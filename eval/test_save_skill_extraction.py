"""Test post-save skill extraction in save_pipeline.save_memory and auto_save._upsert_memory (Phase 3)."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

from save_pipeline import save_memory
from background.auto_save import _upsert_memory


_PROC = """\
# Install PostgreSQL
## Step 1: Install
$ sudo apt install -y postgresql
## Step 2: Start service
$ sudo systemctl start postgresql
"""

_FACT = """\
# Note about PostgreSQL
PostgreSQL is an open-source relational database.
"""


def _skill_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM memory_skills").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


class TestSavePipelineSkillExtraction(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="save_skill_"))
        self.db_path = self.tmpdir / "memory.db"
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)
        from eval._fixtures import bootstrap_temp_db_clean
        bootstrap_temp_db_clean(self.db_path)
        # Create the category dir so save_memory can write the file
        (self.tmpdir / "memory" / "lessons").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if "MEMORY_DB_PATH" in os.environ:
            del os.environ["MEMORY_DB_PATH"]

    def test_save_memory_extracts_skill_for_procedural_content(self):
        """save_memory extracts skills for procedural content with numbered steps and code blocks."""
        note_id = save_memory(
            content=_PROC,
            category="lessons",
            title_slug="install-postgresql",
            tags=["postgres"],
            safety_wiring=False,
        )
        self.assertFalse(note_id.startswith("Error"))
        db_path = self.tmpdir / "memory.db"
        self.assertEqual(_skill_count(db_path), 1)

    def test_save_memory_does_not_extract_skill_for_fact(self):
        note_id = save_memory(
            content=_FACT,
            category="lessons",
            title_slug="postgresql-fact",
            tags=["postgres"],
            safety_wiring=False,
        )
        self.assertFalse(note_id.startswith("Error"))
        db_path = self.tmpdir / "memory.db"
        self.assertEqual(_skill_count(db_path), 0)

    def test_save_memory_is_idempotent_for_same_content(self):
        """Skill extraction is idempotent via content_hash dedup."""
        save_memory(
            content=_PROC,
            category="lessons",
            title_slug="install-postgresql",
            tags=["postgres"],
            safety_wiring=False,
        )
        db_path = self.tmpdir / "memory.db"
        self.assertEqual(_skill_count(db_path), 1)
        save_memory(
            content=_PROC,
            category="lessons",
            title_slug="install-postgresql-copy",
            tags=["postgres"],
            safety_wiring=False,
        )
        self.assertEqual(_skill_count(db_path), 1)


class TestAutoSaveSkillExtraction(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="autosave_skill_"))
        self.db_path = self.tmpdir / "memory.db"
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)
        from eval._fixtures import bootstrap_temp_db_clean
        bootstrap_temp_db_clean(self.db_path)
        (self.tmpdir / "memory" / "sessions").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if "MEMORY_DB_PATH" in os.environ:
            del os.environ["MEMORY_DB_PATH"]

    def test_upsert_memory_extracts_skill(self):
        # _upsert_memory needs a DB with the full schema. We can let it create
        # the table, but it expects `repo_id` and possibly temporal columns.
        # We bootstrap by calling save_memory once to create the schema, then
        # call _upsert_memory for the procedural note.
        db_path = self.tmpdir / "memory.db"
        save_memory(
            content="bootstrap",
            category="sessions",
            title_slug="bootstrap",
            safety_wiring=False,
        )
        # _upsert_memory uses get_db_path() which resolves MEMORY_DB_PATH
        ok = _upsert_memory(
            note_id="sessions/install-postgresql",
            source_file="sessions/install-postgresql.md",
            content=_PROC,
            tags_json='["postgres"]',
            now_iso="2026-06-17T00:00:00",
        )
        self.assertTrue(ok)
        for _ in range(5):
            if _skill_count(db_path) > 0:
                break
        self.assertEqual(_skill_count(db_path), 1)

    def test_upsert_memory_does_not_extract_fact(self):
        db_path = self.tmpdir / "memory.db"
        save_memory(
            content="bootstrap",
            category="sessions",
            title_slug="bootstrap",
            safety_wiring=False,
        )
        ok = _upsert_memory(
            note_id="sessions/postgresql-fact",
            source_file="sessions/postgresql-fact.md",
            content=_FACT,
            tags_json='["postgres"]',
            now_iso="2026-06-17T00:00:00",
        )
        self.assertTrue(ok)
        self.assertEqual(_skill_count(db_path), 0)


if __name__ == "__main__":
    unittest.main()
