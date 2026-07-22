#!/usr/bin/env python3
"""Regression tests for patch_memory atomicity, file/DB ordering, and rowcount guards (C2, H1, M4)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from save.pipeline import patch_memory, save_memory
from infra.infrastructure import ErrorCode


class TestPatchMemoryAtomicity(unittest.TestCase):
    def setUp(self):
        from infra.lock_manager import clear_lock_manager_cache
        clear_lock_manager_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"
        self.doc_dir = Path(self.temp_dir.name) / "docs"
        self.doc_dir.mkdir(parents=True, exist_ok=True)
        self.env_patcher = mock.patch.dict(
            os.environ,
            {
                "MEMORY_DB_PATH": str(self.db_path),
                "MEMORY_DOCS_DIR": str(self.doc_dir),
            },
        )
        self.env_patcher.start()

    def tearDown(self):
        from infra.lock_manager import clear_lock_manager_cache
        self.env_patcher.stop()
        clear_lock_manager_cache()
        self.temp_dir.cleanup()

    def test_patch_memory_db_commits_before_file_write(self):
        """Verify DB write happens before file write and file failure doesn't roll back DB."""
        # Create initial note
        note_id = save_memory(
            content="Original memory line 1\nOriginal memory line 2",
            category="lessons",
            title_slug="test-slug",
            tags=["test"],
            tenant_id="tenant_a",
        )
        self.assertTrue(bool(note_id), f"Save failed: {note_id}")

        # Patch with additions and deletions; inject failure in safe_atomic_write
        with mock.patch("infra.memory_common.safe_atomic_write", side_effect=OSError("Disk write simulated failure")):
            patch_res = patch_memory(
                note_id=note_id,
                additions=["New memory line 3"],
                deletions=["Original memory line 2"],
                tenant_id="tenant_a",
            )
            self.assertTrue(patch_res.get("success"), f"Patch failed despite file error: {patch_res}")

        # Check DB state was updated
        from infra.db import open_db
        with open_db(self.db_path) as db:
            row = db.execute("SELECT content FROM tenant_memories WHERE id = ?", (note_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("New memory line 3", row[0])
            self.assertNotIn("Original memory line 2", row[0])

    def test_patch_memory_soft_deleted_note_fails(self):
        """Verify patch_memory fails if note was soft-deleted (deleted_at IS NOT NULL)."""
        note_id = save_memory(
            content="Memory to delete then patch",
            category="lessons",
            title_slug="test-slug-2",
            tags=["test"],
            tenant_id="tenant_a",
        )
        self.assertTrue(bool(note_id))

        from infra.db import open_db
        with open_db(self.db_path) as db:
            db.execute("UPDATE tenant_memories SET deleted_at = '2026-07-23T00:00:00Z' WHERE id = ?", (note_id,))

        patch_res = patch_memory(
            note_id=note_id,
            additions=["Extra line"],
            tenant_id="tenant_a",
        )
        self.assertFalse(patch_res.get("success"))
        self.assertEqual(patch_res.get("error_code"), ErrorCode.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
