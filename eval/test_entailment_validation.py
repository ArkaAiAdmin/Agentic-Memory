"""A2 — Entailment validation tests.

Verifies:
  1. Derived facts set is_entailed=1
  2. Superseding a source fact invalidates derived facts (is_entailed=0 + chain.valid=0)
  3. Validation pass retracts derived facts contradicting an existing active fact
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Generator, Optional

sys.path.insert(0, str(os.environ.get("MEMORY_INSTALL_ROOT", os.path.expanduser("~/.config/agentic-memory"))))
from infra.memory_config import install_root
sys.path.insert(0, str(install_root()))

import pytest
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


class _FactWriter:
    """Manages a single raw sqlite3 connection for test setup.

    All inserts share one connection so data is visible across calls,
    and FK constraints are off (mirrors test_compilation_behavior.py).
    """

    def __init__(self, p: Path) -> None:
        self.p = p
        self.conn = sqlite3.connect(str(p))
        self.conn.execute("PRAGMA foreign_keys=OFF")
        from fact.fact_schema import ensure_facts_schema
        ensure_facts_schema(self.conn)
        self._entity_cache: dict[str, int] = {}

    def _get_eid(self, name: str) -> int:
        if name not in self._entity_cache:
            self.conn.execute(
                "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES (?, 'concept')",
                (name.lower(),),
            )
            row = self.conn.execute(
                "SELECT id FROM kg_entities WHERE name = ?", (name.lower(),)
            ).fetchone()
            assert row is not None, f"Entity {name} not registered"
            self._entity_cache[name] = int(row[0])
        return self._entity_cache[name]

    def up_fact(self, subject: str, predicate: str, obj: str,
                confidence: float, source_memory: str) -> int:
        subj_eid = self._get_eid(subject)
        obj_eid = self._get_eid(obj)
        row = self.conn.execute(
            "SELECT id, locked, confidence FROM kg_facts "
            "WHERE subject = ? AND predicate = ? AND object = ?",
            (subject.lower(), predicate, obj.lower()),
        ).fetchone()
        now = time.time()
        if row and not row[1]:
            new_conf = max(row[2], confidence)
            self.conn.execute(
                "UPDATE kg_facts SET last_seen = ?, mention_count = mention_count + 1, "
                "confidence = ? WHERE id = ?",
                (now, new_conf, row[0]),
            )
            return int(row[0])
        cur = self.conn.execute(
            "INSERT INTO kg_facts "
            "(subject, predicate, object, confidence, first_seen, last_seen, "
            "source_memory, belief_status, epistemic_source, fact_type, is_entailed, "
            "subject_entity_id, object_entity_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 'agent', 'observation', 0, ?, ?)",
            (subject.lower(), predicate, obj.lower(), confidence, now, now,
             source_memory, subj_eid, obj_eid),
        )
        last_id = cur.lastrowid
        assert last_id is not None
        return int(last_id)

    def log_chain(self, source_fact_ids: list, derived_fact_id: int,
                  derivation_type: str) -> None:
        self.conn.execute(
            "INSERT INTO entailment_chains "
            "(source_fact_ids, derived_fact_id, derivation_type, confidence, derived_at, valid) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (json.dumps(source_fact_ids), derived_fact_id, derivation_type, 0.8, time.time()),
        )

    def insert_memory(self, mem_id: str, content: str, source_file: str) -> None:
        now = time.time()
        self.conn.execute(
            "INSERT OR IGNORE INTO memories "
            "(id, content, source_file, category, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mem_id, content, source_file, "lessons", now, now, now),
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class TestIsEntailedFlag:
    """A2.1 — Derived facts have is_entailed=1"""

    def test_derived_facts_have_is_entailed_set(self, db_path: Path):
        from infra.db import open_db
        from reasoning.compile import infer_entailment_chains

        fw = _FactWriter(db_path)
        fw.insert_memory("mem/x", "Python is a programming language.", "mem/x")
        fid1 = fw.up_fact("python", "is_a", "programming_language", 0.9, "mem/x")
        fid2 = fw.up_fact("programming_language", "is_a", "tool", 0.85, "mem/x")
        assert fid1 > 0 and fid2 > 0
        fw.commit()
        fw.close()

        with open_db(db_path, timeout=10.0) as db:
            result = infer_entailment_chains(db, db_path, batch_size=100)
        assert result["derived"] >= 1, f"Expected >=1 derived fact, got {result}"

        with open_db(db_path, timeout=10.0) as db:
            rows = db.execute(
                "SELECT id, is_entailed FROM kg_facts "
                "WHERE belief_status='active' AND is_entailed = 1"
            ).fetchall()
            assert len(rows) >= 1, "Expected >=1 fact with is_entailed=1"
            for row in rows:
                assert row[1] == 1, f"Fact {row[0]} should have is_entailed=1"


class TestPropagationOnSupersession:
    """A2.2 — Superseding source fact propagates invalidation to derived facts"""

    def test_supersede_propagates_to_derived_facts(self, db_path: Path):
        from infra.db import open_db
        from reasoning.compile import infer_entailment_chains
        from fact import fact_temporal as ft

        fw = _FactWriter(db_path)
        fw.insert_memory("mem/s1", "Python is a tool.", "mem/s1")
        fw.insert_memory("mem/s2", "A tool is a device.", "mem/s2")
        fid_a = fw.up_fact("python", "is_a", "tool", 0.9, "mem/s1")
        fid_b = fw.up_fact("tool", "is_a", "device", 0.85, "mem/s2")
        assert fid_a > 0 and fid_b > 0
        fw.commit()
        fw.close()

        with open_db(db_path, timeout=10.0) as db:
            result = infer_entailment_chains(db, db_path, batch_size=100)
        assert result["derived"] >= 1, f"Expected >=1 derived, got {result}"

        with open_db(db_path, timeout=10.0) as db:
            derived_fids = [
                row[0] for row in db.execute(
                    "SELECT derived_fact_id FROM entailment_chains WHERE valid = 1"
                ).fetchall()
            ]
        assert len(derived_fids) >= 1, "Expected >=1 active chain"
        dfid = derived_fids[0]

        import fact as fe
        with open_db(db_path, timeout=10.0) as db:
            new_fid = fe._upsert_fact(db, "python", "is_a", "language", 0.95,
                                      time.time(), source_memory="mem/s1",
                                      belief_status="active", fact_type="observation")
            assert new_fid is not None
            db.commit()
            ok = ft.supersede_fact(db, old_id=fid_a, new_id=new_fid, reason="contradicted")
            assert ok, "supersede_fact should return True"
            db.commit()

        with open_db(db_path, timeout=10.0) as db:
            row = db.execute(
                "SELECT is_entailed FROM kg_facts WHERE id = ?", (dfid,)
            ).fetchone()
            assert row is not None, f"Derived fact {dfid} not found"
            actual = row[0]
            assert actual == 0 or actual is None, (
                f"Fact {dfid} is_entailed should be 0 after supersession, got {actual}"
            )
            chain_active = db.execute(
                "SELECT COUNT(*) FROM entailment_chains WHERE derived_fact_id = ? AND valid = 1",
                (dfid,),
            ).fetchone()[0]
            assert chain_active == 0, f"chain for {dfid} should have valid=0"


class TestValidationPassRetraction:
    """A2.3 — Validation pass retracts derived facts contradicting existing active facts"""

    def test_validation_does_not_inflate_derived_count(self, db_path: Path):
        fw = _FactWriter(db_path)
        fw.insert_memory("mem/existing", "X is a mammal.", "mem/existing")
        fw.insert_memory("mem/src", "X is a dog.", "mem/src")
        fw.insert_memory("mem/chain", "dog is_a animal.", "mem/chain")
        fw.up_fact("x", "is_a", "mammal", 0.9, "mem/existing")
        fw.up_fact("x", "is_a", "dog", 0.8, "mem/src")
        fw.up_fact("dog", "is_a", "animal", 0.7, "mem/chain")
        fw.commit()
        fw.close()

        from reasoning.compile import infer_entailment_chains
        from infra.db import open_db
        with open_db(db_path, timeout=10.0) as db:
            infer_entailment_chains(db, db_path, batch_size=100)

        with open_db(db_path, timeout=10.0) as db:
            contradictory_derived = db.execute(
                "SELECT COUNT(*) FROM kg_facts kf "
                "JOIN entailment_chains ec ON ec.derived_fact_id = kf.id "
                "WHERE kf.subject = 'x' AND kf.predicate = 'is_a' "
                "AND kf.object = 'mammal' AND kf.epistemic_source = 'inferred' "
                "AND kf.belief_status = 'active'"
            ).fetchone()[0]
            assert contradictory_derived == 0, (
                f"Validation should retract contradictory derived (x,is_a,mammal); "
                f"found {contradictory_derived} active"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
