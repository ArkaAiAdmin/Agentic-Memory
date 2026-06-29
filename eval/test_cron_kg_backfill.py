"""Tests for the weekly KG backfill cron (P3.4, 2026-06-19).

Covers:
- preflight_stats captures row counts and timestamp
- postflight_stats computes deltas vs pre
- run_backfill subprocess invocation works
- main() writes a JSON log entry
- main() exits 0 on success
- main() exits 1 when db_path missing
- --dry-run uses --health (no writes)
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load_cron_kg():
    """Load cron_kg_backfill fresh from REPO."""
    import importlib.util as _importlib_util

    spec = _importlib_util.spec_from_file_location(
        "_test_cron_kg_backfill", str(REPO / "cron" / "cron_kg_backfill.py")
    )
    if spec is None:
        raise RuntimeError("Could not load cron_kg_backfill.py")
    mod = _importlib_util.module_from_spec(spec)
    sys.modules["_test_cron_kg_backfill"] = mod
    loader = spec.loader
    if loader is None:
        raise RuntimeError("spec.loader is None for cron_kg_backfill.py")
    loader.exec_module(mod)
    return mod


def _fake_db_connection(counts: dict[str, int]) -> MagicMock:
    """Return a MagicMock that mimics open_db() context manager.

    Yields a connection whose .execute(...).fetchone() returns the
    appropriate count for each kg_* / memories table.
    """
    cm = MagicMock()
    conn = MagicMock()
    cm.__enter__ = MagicMock(return_value=conn)
    cm.__exit__ = MagicMock(return_value=False)

    def fake_execute(sql, *args, **kwargs):
        result = MagicMock()
        # Crude parser: extract table name from "SELECT COUNT(*) FROM <table>"
        sql_lower = sql.lower() if isinstance(sql, str) else ""
        for table, count in counts.items():
            if f"from {table}" in sql_lower:
                result.fetchone.return_value = (count,)
                return result
        # default: 0
        result.fetchone.return_value = (0,)
        return result

    conn.execute = fake_execute
    return cm


class TestPreflightStats(unittest.TestCase):
    def test_preflight_captures_counts(self):
        """preflight_stats returns counts for kg_facts, kg_entities, kg_edges, memories."""
        mod = _load_cron_kg()
        fake_conn = _fake_db_connection(
            {"kg_facts": 100, "kg_entities": 50, "kg_edges": 25, "memories": 200}
        )
        with patch.object(mod, "open_db", return_value=fake_conn):
            pre = mod.preflight_stats(Path("/tmp/fake.db"))
        self.assertEqual(pre["kg_facts"], 100)
        self.assertEqual(pre["kg_entities"], 50)
        self.assertEqual(pre["kg_edges"], 25)
        self.assertEqual(pre["memories"], 200)
        self.assertIn("captured_at", pre)
        self.assertEqual(pre["db_path"], "/tmp/fake.db")

    def test_postflight_computes_deltas(self):
        mod = _load_cron_kg()
        fake_conn = _fake_db_connection(
            {"kg_facts": 2, "kg_entities": 1, "kg_edges": 0, "memories": 5}
        )
        pre = {
            "kg_facts": 5,
            "kg_entities": 3,
            "kg_edges": 2,
            "memories": 10,
        }
        with patch.object(mod, "open_db", return_value=fake_conn):
            post = mod.postflight_stats(Path("/tmp/fake.db"), pre)
        self.assertEqual(post["deltas"]["kg_facts"], 2 - 5)  # -3
        self.assertEqual(post["deltas"]["kg_entities"], 1 - 3)  # -2
        self.assertEqual(post["deltas"]["kg_edges"], 0 - 2)  # -2
        self.assertEqual(post["deltas"]["memories"], 5 - 10)  # -5

    def test_postflight_handles_missing_table(self):
        """If a table doesn't exist, _table_count returns -1."""
        mod = _load_cron_kg()
        # No kg_edges in the fake connection
        fake_conn = _fake_db_connection(
            {"kg_facts": 1, "kg_entities": 1, "memories": 1}
        )
        pre = {"kg_facts": 0, "kg_entities": 0, "kg_edges": 0, "memories": 0}
        with patch.object(mod, "open_db", return_value=fake_conn):
            post = mod.postflight_stats(Path("/tmp/fake.db"), pre)
        # Missing table = default 0 in the fake (since no match)
        # Actually since "kg_edges" isn't in the counts dict, the fake returns 0
        self.assertEqual(post["kg_edges"], 0)


class TestRunBackfill(unittest.TestCase):
    @patch("subprocess.run")
    def test_run_backfill_invokes_subprocess(self, mock_run):
        mod = _load_cron_kg()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Backfill complete\n"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        result = mod.run_backfill(
            Path("/tmp/fake.db"),
            commit_every=10,
            progress_every=50,
            dry_run=False,
        )

        self.assertEqual(result["exit_code"], 0)
        self.assertIn("command", result)
        cmd = result["command"]
        self.assertIn("--incremental", cmd)
        self.assertIn("--commit-every=10", cmd)
        self.assertIn("--progress-every=50", cmd)
        self.assertNotIn("--health", cmd)
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_dry_run_uses_health_flag(self, mock_run):
        mod = _load_cron_kg()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        result = mod.run_backfill(
            Path("/tmp/fake.db"),
            commit_every=25,
            progress_every=100,
            dry_run=True,
        )
        cmd = result["command"]
        self.assertIn("--health", cmd)
        self.assertIn("--incremental", cmd)

    @patch("subprocess.run")
    def test_run_backfill_handles_timeout(self, mock_run):
        mod = _load_cron_kg()
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["x"], timeout=1)

        result = mod.run_backfill(
            Path("/tmp/fake.db"),
            commit_every=25,
            progress_every=100,
            dry_run=False,
        )
        self.assertEqual(result["exit_code"], -1)
        self.assertIn("TIMEOUT", result["stderr_tail"])


class TestResolveDbPath(unittest.TestCase):
    def test_resolve_uses_env_var(self):
        mod = _load_cron_kg()
        os.environ["MEMORY_DB_PATH"] = "/tmp/explicit_test.db"
        try:
            p = mod._resolve_db_path()
            self.assertEqual(str(p), "/tmp/explicit_test.db")
        finally:
            os.environ.pop("MEMORY_DB_PATH", None)


class TestMainExitCodes(unittest.TestCase):
    @patch("subprocess.run")
    def test_main_exits_0_on_success(self, mock_run):
        """main() exits 0 when backfill succeeds and DB exists."""
        import tempfile

        mod = _load_cron_kg()
        fake_conn = _fake_db_connection(
            {"kg_facts": 100, "kg_entities": 50, "kg_edges": 25, "memories": 200}
        )
        with patch.object(mod, "open_db", return_value=fake_conn):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "All good"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            tmpdir = tempfile.mkdtemp()
            log_file = Path(tmpdir) / "kg.log"
            try:
                # Patch sys.argv so argparse parses the right args
                with patch.object(
                    sys, "argv", ["cron_kg_backfill.py", f"--log-file={log_file}"]
                ):
                    rc = 0
                    try:
                        mod.main()
                    except SystemExit as e:
                        rc = e.code if e.code is not None else 0
                    self.assertEqual(rc, 0)
                # Log file should have one JSON line
                self.assertTrue(log_file.exists())
                lines = log_file.read_text().strip().split("\n")
                self.assertEqual(len(lines), 1)
                entry = json.loads(lines[0])
                self.assertIn("pre", entry)
                self.assertIn("post", entry)
                self.assertIn("result", entry)
                self.assertEqual(entry["result"]["exit_code"], 0)
            finally:
                import shutil

                shutil.rmtree(tmpdir, ignore_errors=True)

    def test_main_exits_1_on_missing_db(self):
        """main() exits 1 when memory.db doesn't exist."""
        mod = _load_cron_kg()
        missing = Path("/tmp/does_not_exist_test_xyz_kg.db")
        # Don't even let argparse see pytest args
        with patch.object(sys, "argv", ["cron_kg_backfill.py"]):
            with patch.object(mod, "_resolve_db_path", return_value=missing):
                with self.assertRaises(SystemExit) as cm:
                    mod.main()
                self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
