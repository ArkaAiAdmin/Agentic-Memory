"""Unit and behavioral tests for the Distributed Lock Manager and Skill Extractor upgrades.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

# Make project importable
import sys
INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

from infra.lock_manager import (
    get_lock_manager,
    SQLiteLockManager,
    clear_lock_manager_cache,
)
from skill_extractor import is_skill_worthy


class TestDistributedLockManager(unittest.TestCase):
    def setUp(self):
        clear_lock_manager_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_memory.db"
        # Seed locks table
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE system_locks ("
            "  lock_key TEXT PRIMARY KEY,"
            "  holder_id TEXT NOT NULL,"
            "  acquired_at TEXT NOT NULL,"
            "  expires_at TEXT NOT NULL,"
            "  lease_token TEXT NOT NULL"
            ")"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()
        clear_lock_manager_cache()

    def test_sqlite_lock_lifecycle(self):
        lm = SQLiteLockManager(self.db_path)
        
        # 1. Acquire succeeds
        success, token = lm.acquire_lock("rebuild_lock", "agent-1", ttl_seconds=5)
        self.assertTrue(success)
        self.assertIsNotNone(token)
        
        # 2. Duplicate acquisition fails
        success2, token2 = lm.acquire_lock("rebuild_lock", "agent-2", ttl_seconds=5)
        self.assertFalse(success2)
        self.assertEqual(token2, "")
        
        # 3. Locked state is correct
        self.assertTrue(lm.is_locked("rebuild_lock"))
        
        # 4. Renew lock lease
        renewed = lm.renew_lock("rebuild_lock", token, ttl_seconds=10)
        self.assertTrue(renewed)
        
        # 5. Release lock
        released = lm.release_lock("rebuild_lock", token)
        self.assertTrue(released)
        
        # 6. Locked state after release is correct
        self.assertFalse(lm.is_locked("rebuild_lock"))

    def test_sqlite_lock_timeout_expiry(self):
        lm = SQLiteLockManager(self.db_path)
        
        # Acquire with 1 sec TTL
        success, token = lm.acquire_lock("short_lock", "agent-1", ttl_seconds=1)
        self.assertTrue(success)
        
        # Second acquire fails immediately
        success2, _ = lm.acquire_lock("short_lock", "agent-2", ttl_seconds=1)
        self.assertFalse(success2)
        
        # Sleep for expiration
        time.sleep(1.2)
        
        # Second acquire now succeeds after expiration
        success3, token3 = lm.acquire_lock("short_lock", "agent-2", ttl_seconds=1)
        self.assertTrue(success3)
        self.assertNotEqual(token3, "")

    def test_context_manager_mutual_exclusion(self):
        lm = SQLiteLockManager(self.db_path)
        
        # Acquire via context manager
        with lm.acquire_context("context_lock", "agent-1", ttl_seconds=5) as token:
            self.assertIsNotNone(token)
            # Try to acquire concurrently (should fail)
            success, _ = lm.acquire_lock("context_lock", "agent-2")
            self.assertFalse(success)
            
        # Released after exit, should be acquirable now
        success2, _ = lm.acquire_lock("context_lock", "agent-2")
        self.assertTrue(success2)


class TestSkillExtractorUpgrades(unittest.TestCase):
    def test_tree_sitter_markdown_ast_strong_signals(self):
        # 1. Code blocks are strong AST signals
        code_markdown = (
            "Here is the code block:\n"
            "```bash\n"
            "git status\n"
            "```"
        )
        self.assertTrue(is_skill_worthy(code_markdown))

        # 2. Numbered list items are strong AST signals
        list_markdown = (
            "Follow these steps:\n"
            "1. First step\n"
            "2. Second step"
        )
        self.assertTrue(is_skill_worthy(list_markdown))

    def test_onnx_classifier_fallback(self):
        # Text containing procedural words without code/list structure
        # (should trigger ONNX fallback successfully based on weights)
        procedural_prose = "I went ahead and did the setup of the application, then run the docker container and deploy it to production."
        self.assertTrue(is_skill_worthy(procedural_prose))

        # Casual conversational text (should NOT qualify)
        conversational_prose = "I had a wonderful conversation with the team about photography and visual designs today."
        self.assertFalse(is_skill_worthy(conversational_prose))

    def test_regex_fallback_on_missing_model(self):
        # Override file path in is_skill_worthy_onnx temporarily or rename the file
        # to force the regex fallback
        model_path = INSTALL / "models" / "skill_classifier_v1.onnx"
        backup_path = INSTALL / "models" / "skill_classifier_v1.onnx.bak"
        
        if model_path.exists():
            os.rename(model_path, backup_path)
        try:
            # When model is missing, regex fallback still handles code block correctly
            code_markdown = "```sh\npip install onnx\n```"
            self.assertTrue(is_skill_worthy(code_markdown))
        finally:
            if backup_path.exists():
                os.rename(backup_path, model_path)


if __name__ == "__main__":
    unittest.main()
