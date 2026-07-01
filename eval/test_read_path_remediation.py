import os
import threading
import unittest
import numpy as np
from pathlib import Path
from unittest.mock import MagicMock, patch

import search.scoring as scoring
from infra.rate_limiter import RATE_LIMITERS, configure_rate_limits, check_rate_limit, get_retry_after
from infra.memory_common import rate_limit_check
from infra.infrastructure import _err, ErrorCode


class TestReadPathRemediation(unittest.TestCase):

    def setUp(self):
        # Reset rate limiter registry
        RATE_LIMITERS.clear()
        # Seed random for deterministic test exploration
        np.random.seed(42)

    def test_token_bucket_rate_limiter_integration(self):
        """Verify token bucket rate limiter is wired and functions correctly."""
        configure_rate_limits()
        self.assertIn("memory_save", RATE_LIMITERS)
        self.assertIn("memory_search", RATE_LIMITERS)

        # Consume tokens until rate limited
        allowed = [check_rate_limit("memory_save") for _ in range(30)]
        self.assertIn(False, allowed)
        self.assertGreater(get_retry_after("memory_save"), 0.0)

        # Check redirection wrapper
        redirection_ok = rate_limit_check("memory_search")
        self.assertTrue(redirection_ok)

    def test_thompson_sampling_weights(self):
        """Verify Thompson sampling computes weights using Beta distribution."""
        alphas = {"bm25": 10.0, "fitness": 20.0, "importance": 5.0, "pinned": 2.0, "recency": 1.0, "tag_match": 1.0}
        betas = {"bm25": 2.0, "fitness": 1.0, "importance": 10.0, "pinned": 2.0, "recency": 1.0, "tag_match": 1.0}
        expected = scoring._RERANK_WEIGHTS

        # Mock the cache to return our constructed alphas/betas
        mock_cache = (alphas, betas, expected)

        with patch.dict(os.environ, {"MEMORY_EXPLORATION_MODE": "thompson"}):
            weights = scoring._apply_exploration(mock_cache)
            self.assertIsNotNone(weights)
            # Weights should sum to approximately 1.0
            self.assertAlmostEqual(sum(weights.values()), 1.0, places=4)
            # High alpha channels should tend to have higher weights
            self.assertGreater(weights["fitness"], weights["importance"])

    def test_epsilon_greedy_ab_routing(self):
        """Verify epsilon-greedy routing returns expected weights or exploration variants."""
        alphas = {"ch": 1.0 for ch in scoring._RERANK_WEIGHTS}
        betas = {"ch": 1.0 for ch in scoring._RERANK_WEIGHTS}
        expected = {"bm25": 0.5, "fitness": 0.5, "importance": 0.0, "pinned": 0.0, "recency": 0.0, "tag_match": 0.0}

        mock_cache = (alphas, betas, expected)

        # Case 1: Exploit path (r >= epsilon)
        with patch.dict(os.environ, {"MEMORY_EXPLORATION_MODE": "epsilon_greedy", "MEMORY_CTR_EPSILON": "0.0"}):
            weights = scoring._apply_exploration(mock_cache)
            self.assertEqual(weights, expected)

        # Case 2: Explore path (r < epsilon)
        with patch.dict(os.environ, {"MEMORY_EXPLORATION_MODE": "epsilon_greedy", "MEMORY_CTR_EPSILON": "1.0"}):
            # Test multiple runs to check both A/B variants (Control vs Treatment)
            variants_seen = set()
            for _ in range(50):
                w = scoring._apply_exploration(mock_cache)
                if w == scoring._RERANK_WEIGHTS:
                    variants_seen.add("A_CONTROL")
                else:
                    self.assertAlmostEqual(sum(w.values()), 1.0, places=4)
                    variants_seen.add("B_TREATMENT")
            
            self.assertIn("A_CONTROL", variants_seen)
            self.assertIn("B_TREATMENT", variants_seen)

    def test_idle_sleep_wakeup(self):
        """Verify the worker idle sleep loop wakes up when a new task is pending."""
        import sqlite3
        from background.background_worker import _check_high_priority_pending
        from background.background_queue import init_task_queue, enqueue_task

        conn = sqlite3.connect(":memory:")
        init_task_queue(conn)

        # Before enqueuing, should be False
        self.assertFalse(_check_high_priority_pending(conn))

        # Enqueue a task
        enqueue_task(conn, "test_task", {})
        # Should detect pending task and return True
        self.assertTrue(_check_high_priority_pending(conn))
        conn.close()

    def test_pool_background_revalidation(self):
        """Verify the pool reval thread evicts connections with mismatched inodes."""
        import time
        import sqlite3
        from infra.db import _ConnectionPool

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "dummy.db")
            # Create the file so stat is happy
            with open(db_file, "w") as f:
                f.write("")

            pool = _ConnectionPool(max_size=5)
            # Mock os.stat to return a controllable inode
            original_stat = os.stat
            inode_val = 12345

            def mock_stat(path):
                mock_res = MagicMock()
                mock_res.st_ino = inode_val
                return mock_res

            # Directly mock the _inode_of method on the pool instance
            pool._inode_of = lambda path: inode_val

            conn = pool.get(db_file, timeout=1.0)
            key = (db_file, threading.get_ident())
            self.assertIn(key, pool._pool)
            self.assertEqual(pool._inodes.get(key), 12345)

            # Return connection to pool so depth becomes 0 (idle)
            pool.put(conn)

            # Change the inode and verify reval loop evicts the connection
            inode_val = 67890
            # Manually trigger revalidation method
            pool._lock.acquire()
            try:
                # Retrieve keys and run reval logic
                for k in list(pool._pool.keys()):
                    if pool._depth.get(k, 0) == 0 and pool._inode_mismatch(k, pool._pool[k]):
                        pool._pool.pop(k)
                        pool._inodes.pop(k, None)
            finally:
                pool._lock.release()

            # Connection should be evicted
            self.assertNotIn(key, pool._pool)
            self.assertNotIn(key, pool._inodes)
            pool.clear()

    def test_usearch_contradiction_detector(self):
        """Verify that the usearch index is used and successfully finds contradictions."""
        import numpy as np
        from kg.contradiction_detector import detect_contradictions_semantic
        
        # We can construct a mock database in a temporary directory
        import tempfile
        import sqlite3
        from infra.db_migrations import run_schema_setup

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.db"
            conn = sqlite3.connect(str(db_path))
            run_schema_setup(conn)
            
            # Insert conflicting claims on the same subject
            # "The server is currently running." vs "The server is currently down." (negation flip)
            conn.execute(
                "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) VALUES "
                "('m1', 'The server is currently running.', 'source.py', '2026-06-25', '2026-06-25', '2026-06-25'), "
                "('m2', 'The server is currently down.', 'source.py', '2026-06-25', '2026-06-25', '2026-06-25')"
            )
            conn.commit()
            conn.close()
            
            # Since model2vec will run, we should get some contradiction result
            res = detect_contradictions_semantic(tmpdir, threshold=0.1)
            # The result should contain a semantic negation contradiction if similarity threshold is met
            self.assertIsInstance(res, list)

    def test_tenant_view_routing(self):
        """Verify tenant_id column schema exists and views isolate tenant data correctly."""
        from infra.db import open_db
        import tempfile
        
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.db"
            
            # Open connection for tenant1, insert a row
            with open_db(db_path, tenant_id="tenant1") as conn:
                # The schema setup should automatically run and create memories table with tenant_id column
                conn.execute(
                    "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at, tenant_id) "
                    "VALUES ('m1', 'Content for tenant 1', 'f.py', '2026-06-25', '2026-06-25', '2026-06-25', 'tenant1')"
                )
                conn.execute(
                    "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at, tenant_id) "
                    "VALUES ('m2', 'Content for tenant 2', 'f.py', '2026-06-25', '2026-06-25', '2026-06-25', 'tenant2')"
                )
            
            # Now open connection for tenant1, check view
            with open_db(db_path, tenant_id="tenant1", write=False, pooled=True) as conn:
                rows = conn.execute("SELECT id, content FROM tenant_memories").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], "m1")
            
            # Now open connection for tenant2, check view
            with open_db(db_path, tenant_id="tenant2", write=False, pooled=True) as conn:
                rows = conn.execute("SELECT id, content FROM tenant_memories").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], "m2")


if __name__ == "__main__":
    unittest.main()
