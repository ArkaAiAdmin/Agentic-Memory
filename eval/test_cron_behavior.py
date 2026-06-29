"""Behavior tests for the safety-critical cron wrappers.

Covers:
- cron_heartbeat: error path when DB is missing
- cron_pinned_decay: dry-run path, auto-apply path, no-DB path
- cron_crdt_sync: error path, no-peers path, partial-config path
"""

import datetime
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))

from _fixtures import bootstrap_temp_db_clean


def _fresh_db(name: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix=f"cron_behavior_{name}_")) / "memory.db"
    bootstrap_temp_db_clean(p)
    return p


class TestCronHeartbeatBehavior(unittest.TestCase):
    """cron_heartbeat: must error cleanly when DB is missing, run when present."""

    def test_missing_db_exits_1(self):
        """When MEMORY_DB_PATH points to a non-existent file, main() exits 1."""
        # Set up environment so the cron's import-time setdefault doesn't override us
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["GLOBAL_MEM_DIR"] = tmpdir
            env["MEMORY_SELF_DIRECTED"] = "1"
            env["MEMORY_DB_PATH"] = str(Path(tmpdir) / "no_such_db.db")
            # Run the cron in a subprocess so its setdefault doesn't pollute us
            import subprocess

            result = subprocess.run(
                [sys.executable, str(INSTALL_DIR / "cron" / "cron_heartbeat.py")],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("ERROR: no memory.db", result.stdout)

    def test_present_db_runs_heartbeat(self):
        """When DB exists, main() runs the heartbeat and prints a summary."""
        db = _fresh_db("heartbeat_present")
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["GLOBAL_MEM_DIR"] = tmpdir
            env["MEMORY_SELF_DIRECTED"] = "1"
            env["MEMORY_DB_PATH"] = str(db)
            import subprocess

            result = subprocess.run(
                [sys.executable, str(INSTALL_DIR / "cron" / "cron_heartbeat.py")],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
            self.assertIn("Heartbeat complete", result.stdout)


class TestCronPinnedDecayBehavior(unittest.TestCase):
    """cron_pinned_decay: dry-run, auto-apply, and missing-DB paths."""

    def test_missing_db_exits_with_error(self):
        """pinned_decay.check returns an error dict when DB is missing."""
        from pinned_decay import check

        env = os.environ.copy()
        env["MEMORY_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "no_such.db")
        os.environ.update(env)
        result = check()
        self.assertIn("error", result)
        self.assertIn("no DB at", result["error"])

    def test_dry_run_finds_candidates(self):
        """With a stale pinned note, dry_run returns the candidate but doesn't unpin."""
        from pinned_decay import check

        db = _fresh_db("pinned_decay_dry")
        # Insert a pinned note that should be flagged: high psi, stale access
        old_date = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
        ).isoformat()
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                """INSERT INTO memories
                   (id, content, source_file, version_vector, logical_clock,
                    created_at, updated_at, observed_at, last_accessed,
                    pinned, access_count, fitness_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 0.3)""",
                (
                    "lessons/stale_pinned",
                    "old",
                    "lessons/stale_pinned.md",
                    json.dumps({"a": 1}),
                    1,
                    old_date,
                    old_date,
                    old_date,
                    old_date,
                ),
            )
            conn.commit()

        os.environ["MEMORY_DB_PATH"] = str(db)
        result = check(dry_run=True, db_path=db)
        self.assertTrue(result["dry_run"])
        # At least one candidate should appear (high psi, days > 180)
        self.assertGreaterEqual(result["summary"]["auto_unpin_candidates"], 1)
        # Dry run: nothing actually unpinned
        self.assertEqual(result["summary"]["unpinned"], [])

        # Verify the row is still pinned
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute(
                "SELECT pinned FROM memories WHERE id=?",
                ("lessons/stale_pinned",),
            ).fetchone()
        self.assertEqual(row[0], 1)

    def test_auto_apply_unpins_candidates(self):
        """With dry_run=False, stale pinned notes are actually unpinned."""
        from pinned_decay import check

        db = _fresh_db("pinned_decay_apply")
        old_date = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
        ).isoformat()
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                """INSERT INTO memories
                   (id, content, source_file, version_vector, logical_clock,
                    created_at, updated_at, observed_at, last_accessed,
                    pinned, access_count, fitness_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 0.3)""",
                (
                    "lessons/will_unpin",
                    "old",
                    "lessons/will_unpin.md",
                    json.dumps({"a": 1}),
                    1,
                    old_date,
                    old_date,
                    old_date,
                    old_date,
                ),
            )
            conn.commit()

        os.environ["MEMORY_DB_PATH"] = str(db)
        result = check(dry_run=False, db_path=db)
        # At least one note was actually unpinned
        self.assertGreaterEqual(len(result["summary"]["unpinned"]), 1)
        self.assertIn("lessons/will_unpin", result["summary"]["unpinned"])

        # Verify the row is now unpinned
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute(
                "SELECT pinned FROM memories WHERE id=?",
                ("lessons/will_unpin",),
            ).fetchone()
        self.assertEqual(row[0], 0)

    def test_cron_main_exits_2_on_apply(self):
        """cron_pinned_decay.main exits 2 when --auto-apply found and applied candidates."""
        # The cron wrapper just calls pinned_decay.main; subprocess it
        db = _fresh_db("pinned_decay_cron_apply")
        old_date = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
        ).isoformat()
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                """INSERT INTO memories
                   (id, content, source_file, version_vector, logical_clock,
                    created_at, updated_at, observed_at, last_accessed,
                    pinned, access_count, fitness_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 0.3)""",
                (
                    "lessons/cron_target",
                    "old",
                    "lessons/cron_target.md",
                    json.dumps({"a": 1}),
                    1,
                    old_date,
                    old_date,
                    old_date,
                    old_date,
                ),
            )
            conn.commit()

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["GLOBAL_MEM_DIR"] = tmpdir
            env["MEMORY_DB_PATH"] = str(db)
            env["MEMORY_KNOWLEDGE_GRAPH"] = "1"
            import subprocess

            result = subprocess.run(
                [sys.executable, str(INSTALL_DIR / "cron" / "cron_pinned_decay.py"), "--auto-apply"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            # Exit code 2 = applied auto-decay
            self.assertEqual(
                result.returncode, 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )


class TestCronCrdtSyncBehavior(unittest.TestCase):
    """cron_crdt_sync: error paths and no-peers path."""

    def test_missing_db_exits_1(self):
        """No DB at MEMORY_DB_PATH returns exit code 1 with error message."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["GLOBAL_MEM_DIR"] = tmpdir
            env["MEMORY_MULTI_AGENT"] = "1"
            env["MEMORY_CRDT_ENABLED"] = "1"
            env["MEMORY_DB_PATH"] = str(Path(tmpdir) / "no_such.db")
            import subprocess

            result = subprocess.run(
                [sys.executable, str(INSTALL_DIR / "cron" / "cron_crdt_sync.py")],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            # cron_crdt_sync returns 1 when DB is missing, not sys.exit(1)
            self.assertEqual(result.returncode, 1)
            self.assertIn("memory.db not found", result.stdout)

    def test_no_peers_exits_0(self):
        """With no sync peers configured, exits 0 and prints a helpful message."""
        db = _fresh_db("crdt_sync_no_peers")
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["GLOBAL_MEM_DIR"] = tmpdir
            env["MEMORY_MULTI_AGENT"] = "1"
            env["MEMORY_CRDT_ENABLED"] = "1"
            env["MEMORY_DB_PATH"] = str(db)
            # Force no peers by setting an empty memory.toml path that doesn't exist,
            # which the config resolver treats as empty.
            env["MEMORY_CONFIG_PATH"] = str(Path(tmpdir) / "no_config.toml")
            import subprocess

            result = subprocess.run(
                [sys.executable, str(INSTALL_DIR / "cron" / "cron_crdt_sync.py")],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("No sync peers", result.stdout)


if __name__ == "__main__":
    unittest.main()
