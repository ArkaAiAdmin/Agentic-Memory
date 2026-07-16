"""A3 — Query-Time Reasoning Expansion tests.

Verifies:
  1. _reasoning_expand returns expansion terms for entailment-predicate queries
  2. Expansion terms are injected into the FTS query passed to _fts_search
  3. Direct (is_entailed=0) facts score higher than derived (is_entailed=1) facts
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Generator

sys.path.insert(0, str(os.environ.get("MEMORY_INSTALL_ROOT", os.path.expanduser("~/.config/agentic-memory"))))
from infra.memory_config import install_root
sys.path.insert(0, str(install_root()))

import pytest
import unittest.mock as mock
from save_pipeline import save_memory


def _bootstrap_db(p: Path) -> None:
    from infra.db import open_db
    from infra.migration_runner import run_migrations
    from fact.fact_schema import ensure_facts_schema

    with open_db(p, timeout=10.0) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        run_migrations(db)
        ensure_facts_schema(db)
        db.commit()


@pytest.fixture
def db_path() -> Generator[Path, None, None]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    p = Path(tmp.name)
    try:
        _bootstrap_db(p)
        yield p
    finally:
        p.unlink(missing_ok=True)


def _up_fact(p: Path, subject: str, predicate: str, obj: str,
             confidence: float, source_memory: str) -> int:
    """Insert or update a kg_fact. Returns the row id."""
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA foreign_keys=OFF")
    from fact.fact_schema import ensure_facts_schema
    ensure_facts_schema(conn)
    row = conn.execute(
        "SELECT id, locked, confidence FROM kg_facts "
        "WHERE subject = ? AND predicate = ? AND object = ?",
        (subject.lower(), predicate, obj.lower()),
    ).fetchone()
    now = time.time()
    fid: int
    if row and not row[1]:
        new_conf = max(row[2], confidence)
        conn.execute(
            "UPDATE kg_facts SET last_seen = ?, mention_count = mention_count + 1, "
            "confidence = ? WHERE id = ?",
            (now, new_conf, row[0]),
        )
        fid = int(row[0])
    else:
        cur = conn.execute(
            "INSERT INTO kg_facts "
            "(subject, predicate, object, confidence, first_seen, last_seen, "
            "source_memory, belief_status, epistemic_source, fact_type, is_entailed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 'agent', 'observation', 0)",
            (subject.lower(), predicate, obj.lower(), confidence, now, now, source_memory),
        )
        last_rowid = cur.lastrowid
        assert last_rowid is not None
        fid = int(last_rowid)
    conn.commit()
    conn.close()
    return fid


def _log_chain(p: Path, source_fact_ids: list, derived_fact_id: int,
               derivation_type: str) -> None:
    """Log an entailment chain entry."""
    conn = sqlite3.connect(str(p))
    conn.execute(
        "INSERT INTO entailment_chains "
        "(source_fact_ids, derived_fact_id, derivation_type, confidence, derived_at, valid) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (json.dumps(source_fact_ids), derived_fact_id, derivation_type, 0.8, time.time()),
    )
    conn.commit()
    conn.close()


def _save_simple(p: Path, title_slug: str, content: str) -> str:
    nid = save_memory(content=content, title_slug=title_slug,
                      category="lessons", db_path=str(p))
    return nid


class TestReasoningExpandTerms:
    """A3.1 — _reasoning_expand returns expansion terms"""

    def test_expand_returns_entailment_objects_for_is_a_query(self, db_path: Path):
        import search.orchestrator as _orch

        python_fid = _up_fact(db_path, "python", "is_a", "language", 0.9, "mem/py1")
        interp_fid = _up_fact(db_path, "python", "is_a", "interpreted", 0.85, "mem/py2")
        assert python_fid > 0 and interp_fid > 0
        lang_fid = _up_fact(db_path, "language", "is_a", "tool", 0.7, "mem/lang")
        interp_chain_fid = _up_fact(db_path, "interpreted", "is_a", "paradigm", 0.7, "mem/interp")
        _log_chain(db_path, [lang_fid], python_fid, "transitive")
        _log_chain(db_path, [interp_chain_fid], interp_fid, "transitive")
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE kg_facts SET is_entailed = 1 WHERE id IN (?, ?)", (python_fid, interp_fid))
        conn.commit()
        conn.close()

        terms = _orch._reasoning_expand(db_path, "what is python")
        expanded_str = " ".join(terms).lower()
        assert "language" in expanded_str or "interpreted" in expanded_str, (
            f"_reasoning_expand should return expansion terms; got {terms}"
        )

    def test_expand_returns_empty_for_non_entailment_query(self, db_path: Path):
        import search.orchestrator as _orch

        terms = _orch._reasoning_expand(db_path, "how to code in python")
        assert terms == [], f"Expected no expansion; got {terms}"


class TestReasoningExpandInFts:
    """A3.2 — Expansion terms are injected into the FTS query"""

    def test_expansion_terms_appear_in_fts_query(self, db_path: Path):
        import search.orchestrator as _orch

        python_fid = _up_fact(db_path, "python", "is_a", "language", 0.9, "mem/py1")
        interp_fid = _up_fact(db_path, "python", "is_a", "interpreted", 0.85, "mem/py2")
        lang_fid = _up_fact(db_path, "language", "is_a", "tool", 0.7, "mem/lang")
        interp_chain_fid = _up_fact(db_path, "interpreted", "is_a", "paradigm", 0.7, "mem/interp")
        _log_chain(db_path, [lang_fid], python_fid, "transitive")
        _log_chain(db_path, [interp_chain_fid], interp_fid, "transitive")
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE kg_facts SET is_entailed = 1 WHERE id IN (?, ?)", (python_fid, interp_fid))
        conn.commit()
        conn.close()
        _save_simple(db_path, "doc-about-python", "Python is a popular programming language.")

        captured_fts_queries = []

        def _capture_fts_search(db, fts_query, limit, has_fitness, repo_filter="",
                                tag_filter_sql="", tag_filter_params=(), category=None,
                                prefilter_ids=None):
            captured_fts_queries.append(fts_query)
            return []

        original_fts_search = _orch._fts_search
        original_kg_search = _orch._search_kg_facts
        _orch._fts_search = _capture_fts_search
        _orch._search_kg_facts = lambda *a, **kw: []

        mock_conn = mock.MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("language",), ("interpreted",)]
        original_pool_get = None
        try:
            try:
                from infra import _lazy_imports as li
                original_pool_get = li.connection_pool.get
                li.connection_pool.get = lambda *a, **kw: mock_conn
            except Exception:
                pass

            from search.orchestrator import search_memories
            try:
                search_memories(db_path=db_path, query="what is python",
                                limit=5, include_facts=True, rerank=False, light=True)
            except Exception:
                pass
        finally:
            _orch._fts_search = original_fts_search
            _orch._search_kg_facts = original_kg_search
            if original_pool_get is not None:
                try:
                    from infra import _lazy_imports as li2
                    li2.connection_pool.get = original_pool_get
                except Exception:
                    pass

        assert len(captured_fts_queries) >= 1, "_fts_search was never called"
        fts_query = captured_fts_queries[0].lower()
        assert "language" in fts_query or "interpreted" in fts_query, (
            f"FTS query should contain expansion terms; got: {captured_fts_queries[0]}"
        )


class TestIsEntailedScoringDiscount:
    """A3.3 — Direct facts score higher than derived (is_entailed=1) facts"""

    def test_direct_fact_scores_higher_than_derived(self, db_path: Path):
        from infra.db import open_db
        from search.orchestrator import _search_kg_facts

        direct_fid = _up_fact(db_path, "x", "is_a", "mammal", 0.9, "mem/direct")
        derived_fid = _up_fact(db_path, "x", "is_a", "animal", 0.7, "mem/derived")
        assert direct_fid > 0 and derived_fid > 0

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE kg_facts SET is_entailed = 1 WHERE id = ?",
            (derived_fid,),
        )
        conn.execute(
            "INSERT INTO entailment_chains "
            "(source_fact_ids, derived_fact_id, derivation_type, confidence, derived_at, valid) "
            "VALUES (?, ?, 'transitive', 0.7, ?, 1)",
            (json.dumps([1]), derived_fid, time.time()),
        )
        conn.commit()
        conn.close()

        with open_db(db_path, timeout=10.0) as db:
            results = _search_kg_facts(db, "mammal OR animal", 10, True)

        direct_entries = [f for f in results if f.get("subject") == "x" and f.get("object") == "mammal"]
        derived_entries = [f for f in results if f.get("subject") == "x"
                           and f.get("object") == "animal"
                           and f.get("is_entailed") == 1]
        assert len(direct_entries) >= 1, "Expected direct fact (x, is_a, mammal)"
        assert len(derived_entries) >= 1, "Expected derived fact (x, is_a, animal) with is_entailed=1"
        direct_conf = direct_entries[0].get("confidence", 0.0)
        derived_conf = derived_entries[0].get("confidence", 0.0)
        assert direct_conf > derived_conf, (
            f"Direct fact confidence ({direct_conf}) should exceed "
            f"derived fact confidence ({derived_conf})"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
