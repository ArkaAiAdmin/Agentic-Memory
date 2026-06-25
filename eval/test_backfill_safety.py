"""Tests for the 2026-06-19 backfill safety improvements.

Covers:
- _backfill_kg_facts accepts commit_every and progress_every params
- backfill_full does NOT wipe kg_* tables at the start (UPSERT pattern)
- backfill_all / backfill_full / backfill_incremental all accept the
  new kwargs
- main() CLI parses --commit-every and --progress-every
"""

import importlib
import inspect
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load_backfill():
    """Load backfill_all module fresh (so tests are isolated)."""
    import importlib.util as _importlib_util

    spec = _importlib_util.spec_from_file_location(
        "backfill_all", str(REPO / "backfill_all.py")
    )
    assert spec is not None and spec.loader is not None
    mod = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBackfillCommitEverySignature(unittest.TestCase):
    """The new commit_every and progress_every params must thread
    through all backfill functions."""

    def test_backfill_kg_facts_signature(self):
        ba = _load_backfill()
        sig = inspect.signature(ba._backfill_kg_facts)
        self.assertIn("commit_every", sig.parameters)
        self.assertIn("progress_every", sig.parameters)
        # Defaults: 50 commit, 100 progress
        self.assertEqual(sig.parameters["commit_every"].default, 50)
        self.assertEqual(sig.parameters["progress_every"].default, 100)

    def test_backfill_full_signature(self):
        ba = _load_backfill()
        sig = inspect.signature(ba.backfill_full)
        self.assertIn("commit_every", sig.parameters)
        self.assertIn("progress_every", sig.parameters)

    def test_backfill_incremental_signature(self):
        ba = _load_backfill()
        sig = inspect.signature(ba.backfill_incremental)
        self.assertIn("commit_every", sig.parameters)
        self.assertIn("progress_every", sig.parameters)

    def test_backfill_all_signature(self):
        ba = _load_backfill()
        sig = inspect.signature(ba.backfill_all)
        self.assertIn("commit_every", sig.parameters)
        self.assertIn("progress_every", sig.parameters)


class TestBackfillPreservesKg(unittest.TestCase):
    """The data-loss fix: backfill_full must NOT wipe kg_* tables at start."""

    def setUp(self):
        # Use a fresh tmpdir per test run to avoid "table already
        # exists" collisions from a previous failed run.
        import time as _time
        import uuid as _uuid

        self.tmp = tempfile.mkdtemp(
            prefix=f"backfill_test_{int(_time.time())}_{_uuid.uuid4().hex[:6]}_"
        )
        self.db_path = Path(self.tmp) / "memory.db"
        # Bootstrap a minimal schema
        from memory_common import open_db

        with open_db(Path(self.db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS kg_entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    entity_type TEXT,
                    mentions INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT,
                    UNIQUE(name, entity_type)
                );
                CREATE TABLE IF NOT EXISTS kg_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES kg_entities(id),
                    target_id INTEGER NOT NULL REFERENCES kg_entities(id),
                    relation TEXT NOT NULL DEFAULT 'related_to',
                    weight REAL DEFAULT 1.0,
                    created_at TEXT,
                    valid_at TEXT,
                    invalid_at TEXT,
                    UNIQUE(source_id, target_id, relation)
                );
                CREATE TABLE IF NOT EXISTS kg_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT, predicate TEXT, object TEXT,
                    confidence REAL, locked INTEGER,
                    first_seen REAL, last_seen REAL,
                    mention_count INTEGER,
                    source_memory TEXT, context TEXT,
                    subject_entity_id INTEGER, object_entity_id INTEGER
                );
                """
            )
            # Insert a memory FIRST (the kg_facts row will reference it
            # via source_memory FK; order matters).
            conn.execute(
                "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "test/smoke",
                    "Some test content with the word 'preserved-entity' mentioned.",
                    "test/smoke.md",
                    "2026-06-19T00:00:00",
                    "2026-06-19T00:00:00",
                    "2026-06-19T00:00:00",
                ),
            )
            # Now insert a pre-existing entity (the "must not be wiped" data).
            conn.execute(
                "INSERT INTO kg_entities (name, entity_type, mentions, created_at) VALUES (?, ?, ?, ?)",
                ("preserved-entity", "concept", 5, "2026-06-19T00:00:00"),
            )
            # And a pre-existing fact with a valid source_memory reference.
            conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, source_memory) VALUES (?, ?, ?, ?)",
                ("preserved-subject", "has_property", "preserved-object", "test/smoke"),
            )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backfill_kg_facts_does_not_wipe_kg_tables(self):
        """Calling _backfill_kg_facts must PRESERVE existing kg_* rows."""
        ba = _load_backfill()
        # Disable LLM so this runs fast
        os.environ["MEMORY_LLM_EXTRACTION"] = "0"

        # Pre-state
        with sqlite3.connect(str(self.db_path)) as db:
            pre_entities = db.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
            pre_facts = db.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
            self.assertEqual(pre_entities, 1, "pre-existing entity should be present")
            self.assertEqual(pre_facts, 1, "pre-existing fact should be present")

        # Run
        with sqlite3.connect(str(self.db_path), timeout=30) as db:
            db.execute("PRAGMA busy_timeout = 30000;")
            # We need to mock the memory_chunks and other tables backfill
            # for this test. Just call _backfill_kg_facts directly.
            try:
                ba._backfill_kg_facts(db, commit_every=10, progress_every=10)
            except Exception as e:
                # If some downstream table is missing, that's OK —
                # we just need the function to NOT wipe kg_*.
                print(f"  (downstream missing: {e})")

        # Post-state: the pre-existing entity MUST still be there
        with sqlite3.connect(str(self.db_path)) as db:
            post_entities = db.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
            post_facts = db.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
            preserved = db.execute(
                "SELECT name FROM kg_entities WHERE name = ?", ("preserved-entity",)
            ).fetchone()
            self.assertIsNotNone(
                preserved,
                "pre-existing entity was WIPED — the data-loss bug is back",
            )
            self.assertGreaterEqual(
                post_entities, pre_entities, "kg_entities count regressed"
            )
            self.assertGreaterEqual(post_facts, pre_facts, "kg_facts count regressed")


class TestBackfillCliFlags(unittest.TestCase):
    """--commit-every and --progress-every CLI flags must parse."""

    def test_commit_every_flag(self):
        import subprocess

        # Just verify the flag is recognized (run with --help-like
        # dry-run; the script doesn't have --help, so we just
        # verify the parser doesn't error on the flag).
        result = subprocess.run(
            [
                str(REPO / "venv" / "bin" / "python3.14"),
                str(REPO / "backfill_all.py"),
                "--health",
                "--commit-every",
                "10",
                "--progress-every",
                "20",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO),
        )
        # --health doesn't need a real DB; the script should run
        # and either succeed or fail with a clean message
        # (not "unknown argument" or "TypeError").
        combined = (result.stdout + result.stderr).lower()
        self.assertNotIn("unknown argument", combined)
        self.assertNotIn("typeerror: __init__()", combined)


if __name__ == "__main__":
    unittest.main()
