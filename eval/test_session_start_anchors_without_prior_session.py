"""Test that memory_session_start succeeds without prior session id and anchors session correctly."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_surface.mcp_search import memory_session_start
from infra.db import open_db
from infra.db_migrations import run_schema_setup


class TestSessionStartAnchoring(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        with open_db(self.db_path) as db:
            run_schema_setup(db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_session_start_without_prior_session(self):
        with patch.dict(os.environ, {"MEMORY_DB_PATH": str(self.db_path)}):
            # Clean any session state file
            from infra.memory_common import get_sessions_dir
            state_file = get_sessions_dir() / ".current_session.json"
            if state_file.exists():
                state_file.unlink()

            result = memory_session_start()
            self.assertNotIn("Error [", result)
            self.assertNotIn("UnboundLocalError", result)
            self.assertTrue(state_file.exists())
            data = json.loads(state_file.read_text())
            self.assertIn("session_id", data)
            self.assertTrue(bool(data["session_id"]))


if __name__ == "__main__":
    unittest.main()
