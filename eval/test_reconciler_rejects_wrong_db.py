"""Unit test verifying that journal_reconciler correctly resolves paths and rejects nonexistent DBs."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from background.journal_reconciler import _resolve_paths


class TestReconcilerPathResolution(unittest.TestCase):
    def test_reconciler_rejects_nonexistent_db(self):
        with patch.dict(os.environ, {"MEMORY_DB_PATH": "/tmp/nonexistent_dir_12345/memory.db"}):
            with self.assertRaises(FileNotFoundError):
                _resolve_paths()

    def test_reconciler_resolves_valid_env_db(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.db"
            db_path.touch()
            with patch.dict(os.environ, {"MEMORY_DB_PATH": str(db_path)}):
                target_base, journal_path = _resolve_paths()
                self.assertEqual(target_base, Path(tmpdir).resolve())
                self.assertEqual(journal_path, Path(tmpdir).resolve() / "journal.db")


if __name__ == "__main__":
    unittest.main()
