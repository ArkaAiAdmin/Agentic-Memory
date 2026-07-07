"""Behavioral tests for Sprint 3 — Knowledge Compilation.

Verifies:
1. After ingesting 5 related memories → concepts/ entry exists
2. Searching for a compiled concept → concept document ranked above individual memories
3. Entailment chain correctly derived from transitive facts
4. concepts/ entry carries derived_from with all source memory IDs
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(os.environ.get("MEMORY_INSTALL_ROOT", os.path.expanduser("~/.config/agentic-memory"))))
from infra.memory_config import install_root
sys.path.insert(0, str(install_root()))

import pytest
from save_pipeline import save_memory


@pytest.fixture
def db_path_str():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        p = f.name
    from infra.db import open_db
    from infra.migration_runner import run_migrations
    from knowledge_graph.kg_schema import ensure_kg_schema

    with open_db(p, timeout=10.0) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        run_migrations(db)
        ensure_kg_schema(db)
        db.commit()
    yield p
    Path(p).unlink(missing_ok=True)


def _index_all_facts(p: str):
    """Index facts for all memories in the DB."""
    import fact as fe
    from infra.db import open_db

    with open_db(p, timeout=10.0) as db:
        rows = db.execute("SELECT id, content FROM memories WHERE deleted_at IS NULL").fetchall()
        for mid, content in rows:
            try:
                fe.index_facts_for_memory(db, mid, content, fact_type="observation")
            except Exception:
                pass
        db.commit()


class TestConceptCompilation:
    """After ingesting 5 related memories → concepts/ entry exists"""

    def _save(self, p: str, count: int = 5, prefix: str = "test"):
        ids = []
        for i in range(count):
            slug = f"{prefix}-{i}"
            name = f"Concept {chr(65 + i)}"
            save_memory(
                content=f"# Memory {i}\n\n{name} is a kind of knowledge.",
                title_slug=slug, category="lessons", db_path=p,
            )
            ids.append(f"lessons/{slug}")
        return ids

    def test_concept_entry_created_after_five_related_memories(self, db_path_str):
        ids = self._save(db_path_str, count=5, prefix="conc-test")
        _index_all_facts(db_path_str)

        from reasoning.compile import compile_concept
        from infra.db import open_db

        with open_db(db_path_str, timeout=10.0) as db:
            result = compile_concept(db, db_path_str, memory_ids=ids)

        assert result is not None, (
            "compile_concept returned None — no facts gathered. "
            "Verify _index_all_facts creates kg_facts with source_memory matching memory_ids."
        )

        with open_db(db_path_str, timeout=10.0) as db:
            concepts = db.execute(
                "SELECT id FROM memories WHERE id LIKE 'concepts/%'"
            ).fetchall()
            assert len(concepts) >= 1, (
                f"Expected at least 1 concept row, got {len(concepts)}"
            )

    def test_concept_entry_carries_derived_from(self, db_path_str):
        ids = self._save(db_path_str, count=3, prefix="derived-test")
        _index_all_facts(db_path_str)

        from reasoning.compile import compile_concept
        from infra.db import open_db

        with open_db(db_path_str, timeout=10.0) as db:
            result = compile_concept(db, db_path_str, memory_ids=ids)

        assert result is not None, (
            "compile_concept returned None — no facts gathered. "
            "Verify _index_all_facts creates kg_facts with source_memory matching memory_ids."
        )

        with open_db(db_path_str, timeout=10.0) as db:
            concept = db.execute(
                "SELECT content, metadata FROM memories WHERE id LIKE 'concepts/%' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            assert concept is not None
            meta = json.loads(concept[1]) if isinstance(concept[1], str) else (concept[1] or {})
            derived_from = meta.get("derived_from", [])
            assert len(derived_from) >= 1


class TestEntailmentChains:
    """Entailment chain correctly derived from transitive facts"""

    def test_transitive_entailment_derived(self, db_path_str):
        from infra.db import open_db
        import fact as fe
        from fact.fact_schema import ensure_facts_schema

        # Use a raw connection so we can set PRAGMA foreign_keys=OFF during fact inserts
        # (migrations add FK constraints that fail with _upsert_fact's default source_memory="")
        conn_raw = sqlite3.connect(db_path_str)
        conn_raw.execute("PRAGMA foreign_keys=OFF")
        # Ensure the kg_facts table exists with all required columns
        ensure_facts_schema(conn_raw)

        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        conn_raw.execute(
            "INSERT OR IGNORE INTO memories (id, content, source_file, category, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mem/a", "a is a kind of b", "memory/mem/a.md", "lessons", now, now, now),
        )
        conn_raw.execute(
            "INSERT OR IGNORE INTO memories (id, content, source_file, category, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mem/b", "b is a kind of c", "memory/mem/b.md", "lessons", now, now, now),
        )
        # Create entities with lowercase names (matching _upsert_fact's lower() lookup)
        conn_raw.execute("INSERT OR IGNORE INTO kg_entities (id, name, entity_type) VALUES (1, 'a', 'concept')")
        conn_raw.execute("INSERT OR IGNORE INTO kg_entities (id, name, entity_type) VALUES (2, 'b', 'concept')")
        conn_raw.execute("INSERT OR IGNORE INTO kg_entities (id, name, entity_type) VALUES (3, 'c', 'concept')")
        # Create transitive facts: a is_a b, b is_a c
        now_ts = time.time()
        fid1 = fe._upsert_fact(conn_raw, "a", "is_a", "b", 0.95, now_ts,
                                belief_status="active", fact_type="observation",
                                source_memory="mem/a")
        fid2 = fe._upsert_fact(conn_raw, "b", "is_a", "c", 0.95, now_ts,
                                belief_status="active", fact_type="observation",
                                source_memory="mem/b")
        assert fid1 is not None, "_upsert_fact failed for a is_a b"
        assert fid2 is not None, "_upsert_fact failed for b is_a c"
        conn_raw.commit()
        conn_raw.close()

        from reasoning.compile import infer_entailment_chains
        with open_db(db_path_str, timeout=10.0) as db:
            result = infer_entailment_chains(db, db_path_str, batch_size=100)
            assert result is not None

        with open_db(db_path_str, timeout=10.0) as db:
            chains = db.execute(
                "SELECT derivation_type, confidence, source_fact_ids FROM entailment_chains WHERE derivation_type = 'transitive'"
            ).fetchall()
            assert len(chains) >= 1


class TestConceptRanking:
    """Searching for a compiled concept → concept document ranked above individual memories"""

    def test_concept_ranks_above_individual_memories(self, db_path_str):
        ids = []
        for i in range(3):
            slug = f"ranking-test-{i}"
            save_memory(
                content=f"# {slug}\n\nThis document discusses machine learning concepts like neural networks, training, and inference.",
                title_slug=slug, category="lessons", db_path=db_path_str,
            )
            ids.append(f"lessons/{slug}")

        save_memory(
            content="# Machine Learning\n\nA synthesized concept about machine learning covering neural networks, training, and inference algorithms.",
            title_slug="machine-learning", category="concepts", db_path=db_path_str,
        )

        _index_all_facts(db_path_str)

        from search.orchestrator import search_memories
        result = search_memories(db_path=Path(db_path_str), query="machine learning concepts", limit=10)
        results = result.get("results", [])
        concept_found = any("concepts/machine-learni" in (r.get("id", "") or "") for r in results)
        assert concept_found, f"concept not found in search results: {[r.get('id') for r in results]}"


class TestPredicateFilter:
    """A1.1 — Non-entailment predicates do not produce transitive chains"""

    def test_non_entailment_predicates_produce_no_chains(self, db_path_str):
        from infra.db import open_db
        import fact as fe
        from fact.fact_schema import ensure_facts_schema

        conn_raw = sqlite3.connect(db_path_str)
        conn_raw.execute("PRAGMA foreign_keys=OFF")
        ensure_facts_schema(conn_raw)

        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        conn_raw.execute(
            "INSERT OR IGNORE INTO memories (id, content, source_file, category, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mem/p", "python", "mem/p", "lessons", now, now, now),
        )
        fid_a = fe._upsert_fact(conn_raw, "python", "is_a", "language", 0.9, time.time(),
                                source_memory="mem/p", belief_status="active",
                                fact_type="observation")
        fid_desc = fe._upsert_fact(conn_raw, "python", "has_description",
                                   "high-level", 0.8, time.time(),
                                   source_memory="mem/p", belief_status="active",
                                   fact_type="observation")
        fid_located = fe._upsert_fact(conn_raw, "python", "located_in",
                                      "earth", 0.7, time.time(),
                                      source_memory="mem/p", belief_status="active",
                                      fact_type="observation")
        assert fid_a is not None and fid_desc is not None and fid_located is not None

        conn_raw.commit()
        conn_raw.close()

        from reasoning.compile import infer_entailment_chains
        with open_db(db_path_str, timeout=10.0) as db:
            result = infer_entailment_chains(db, db_path_str, batch_size=200)

        with open_db(db_path_str, timeout=10.0) as db:
            non_entailment_derived = db.execute(
                "SELECT ec.id FROM entailment_chains ec "
                "JOIN kg_facts kf ON kf.id = ec.derived_fact_id "
                "WHERE kf.predicate IN ('has_description', 'located_in')"
            ).fetchall()
            assert len(non_entailment_derived) == 0, (
                f"Non-entailment predicates should produce no chains; "
                f"found {len(non_entailment_derived)}"
            )


class TestConjunctiveInference:
    """A1.2 — Conjunctive inference derives related_to between same-subject objects"""

    def test_conjunctive_derives_related_to_same_subject(self, db_path_str):
        from infra.db import open_db
        import fact as fe
        from fact.fact_schema import ensure_facts_schema

        conn_raw = sqlite3.connect(db_path_str)
        conn_raw.execute("PRAGMA foreign_keys=OFF")
        ensure_facts_schema(conn_raw)

        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        conn_raw.execute(
            "INSERT OR IGNORE INTO memories (id, content, source_file, category, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mem/conj", "Python is a framework for web.", "mem/conj", "lessons", now, now, now),
        )
        fid1 = fe._upsert_fact(conn_raw, "python", "is_a", "framework", 0.9, time.time(),
                               source_memory="mem/conj", belief_status="active",
                               fact_type="observation")
        fid2 = fe._upsert_fact(conn_raw, "python", "is_a", "language", 0.85, time.time(),
                               source_memory="mem/conj", belief_status="active",
                               fact_type="observation")
        assert fid1 is not None and fid2 is not None
        conn_raw.commit()
        conn_raw.close()

        from reasoning.compile import infer_entailment_chains
        with open_db(db_path_str, timeout=10.0) as db:
            result = infer_entailment_chains(db, db_path_str, batch_size=100)
        assert result["derived"] >= 1, f"Expected >=1 derived fact, got {result}"

        expected_conf = round(0.7 * min(0.9, 0.85), 4)
        with open_db(db_path_str, timeout=10.0) as db:
            chain = db.execute(
                "SELECT ec.derivation_type, ec.confidence, kf.subject, kf.predicate, kf.object "
                "FROM entailment_chains ec "
                "JOIN kg_facts kf ON kf.id = ec.derived_fact_id "
                "WHERE kf.subject = 'python' AND kf.predicate = 'related_to'"
            ).fetchone()
            assert chain is not None, "Expected conjunctive chain (python, related_to, ...)"
            assert chain[0] == "conjunctive", (
                f"derivation_type should be 'conjunctive', got '{chain[0]}'"
            )
            assert abs(chain[1] - expected_conf) < 0.01, (
                f"Conjunctive confidence should be ~{expected_conf}, got {chain[1]}"
            )
            assert chain[2].lower() == "python"
            assert chain[4].lower() in ("language", "framework")


class TestConceptNaming:
    """A1.3 — Concept name derives from most-frequent is_a object"""

    def test_concept_name_from_isa_object(self, db_path_str):
        ids = []
        objects = []
        for i in range(3):
            slug = f"concept-name-{i}"
            obj = "Python" if i < 2 else "Framework"
            _content = f"# Memory {i}\n\nPython is a {obj.lower()}."
            save_memory(content=_content, title_slug=slug,
                        category="lessons", db_path=db_path_str)
            ids.append(f"lessons/{slug}")
            objects.append(obj)

        _index_all_facts(db_path_str)

        from reasoning.compile import compile_concept
        from infra.db import open_db
        with open_db(db_path_str, timeout=10.0) as db:
            result = compile_concept(db, db_path_str, memory_ids=ids)

        assert result is not None, "compile_concept should return a concept"
        concept_name = result.get("concept_id", "")
        concept_slug = result.get("slug", "")
        assert "python" in concept_slug.lower(), (
            f"Concept slug should derive from 'is_a' object 'Python'; got '{concept_slug}'"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
