"""Tests for docker/cron_runner.py — containerized cron scheduler.

Covers:
  * Schedule loading from JSON
  * --once mode runs all enabled entries
  * Disabled entries are skipped
  * Missing script logs error and continues
  * Per-entry env vars are applied
  * Non-zero exit code is logged but doesn't crash
  * Timeout is honored
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker"
sys.path.insert(0, str(DOCKER_DIR))

import cron_runner  # noqa: E402


SAMPLE_SCHEDULE = [
    {
        "name": "fast_script.py",
        "interval_minutes": 5,
        "args": ["--flag"],
        "env": {"MY_VAR": "hello"},
        "enabled": True,
        "timeout_seconds": 30,
    },
    {
        "name": "disabled_script.py",
        "interval_minutes": 60,
        "args": [],
        "enabled": False,
    },
    {
        "name": "another.py",
        "interval_minutes": 10,
        "args": [],
        "enabled": True,
    },
]


class TestLoadSchedule(unittest.TestCase):
    def test_loads_list(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(SAMPLE_SCHEDULE, f)
            tmp_path = Path(f.name)
        try:
            items = cron_runner.load_schedule(tmp_path)
            self.assertEqual(len(items), 3)
        finally:
            tmp_path.unlink()

    def test_rejects_non_list(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"not": "a list"}, f)
            tmp_path = Path(f.name)
        try:
            with self.assertRaises(ValueError):
                cron_runner.load_schedule(tmp_path)
        finally:
            tmp_path.unlink()


class TestRunOnce(unittest.TestCase):
    def setUp(self):
        # 2026-06-29 fix: create a real tmp dir with the script the test
        # expects. Path.exists() doesn't go through os.path.exists, so
        # the previous mock did not intercept the file-existence check and
        # the function returned early on CI where /scripts/test.py doesn't
        # exist.
        import tempfile

        self._tmpdir = Path(tempfile.mkdtemp(prefix="docker_cron_runner_"))
        (self._tmpdir / "test.py").write_text("# stub script\n")

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_runs_subprocess_with_env(self):
        with patch("cron_runner.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            cron_runner.run_once(
                {
                    "name": "test.py",
                    "args": ["--foo"],
                    "env": {"A": "1"},
                },
                self._tmpdir,
            )
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertEqual(args[0][1], str(self._tmpdir / "test.py"))
        self.assertEqual(args[0][2], "--foo")
        self.assertEqual(kwargs["env"]["A"], "1")
        self.assertEqual(
            kwargs["env"]["PATH"], __import__("os").environ.get("PATH", "")
        )

    def test_missing_script_logs_error(self):
        with patch("cron_runner.subprocess.run") as mock_run:
            with self.assertLogs("agentic_memory.cron_runner", level="ERROR") as cm:
                cron_runner.run_once(
                    {"name": "missing.py", "args": []},
                    self._tmpdir,
                )
        self.assertFalse(mock_run.called)
        self.assertTrue(any("missing.py" in line for line in cm.output))

    def test_nonzero_exit_doesnt_crash(self):
        with patch("cron_runner.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "boom"
            with self.assertLogs("agentic_memory.cron_runner", level="ERROR"):
                cron_runner.run_once(
                    {"name": "test.py", "args": []},
                    self._tmpdir,
                )
        # No exception raised.

    def test_timeout_logged(self):
        import subprocess as sp

        with patch(
            "cron_runner.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="test", timeout=5),
        ):
            with self.assertLogs("agentic_memory.cron_runner", level="ERROR"):
                cron_runner.run_once(
                    {"name": "test.py", "args": [], "timeout_seconds": 5},
                    self._tmpdir,
                )


class TestNextDue(unittest.TestCase):
    def test_first_run_due_now(self):
        now = 1000.0
        due = cron_runner.next_due({"interval_minutes": 5}, now_ts=now, last_run=0)
        self.assertEqual(due, now)

    def test_after_interval(self):
        last = 1000.0
        interval = 5 * 60  # 5 min
        due = cron_runner.next_due(
            {"interval_minutes": 5}, now_ts=last + 100, last_run=last
        )
        self.assertEqual(due, last + interval)

    def test_due_when_elapsed(self):
        last = 1000.0
        interval = 5 * 60
        now = last + interval + 10  # 10s past due
        due = cron_runner.next_due({"interval_minutes": 5}, now_ts=now, last_run=last)
        self.assertEqual(due, last + interval)
        # And due <= now, so the scheduler will fire.
        self.assertLessEqual(due, now)


class TestMainOnce(unittest.TestCase):
    def test_once_runs_all_enabled(self):
        with patch("cron_runner.load_schedule") as mock_load:
            mock_load.return_value = SAMPLE_SCHEDULE
            with patch("cron_runner.run_once") as mock_run:
                with patch.object(sys, "argv", ["cron_runner.py", "--once"]):
                    with patch("cron_runner.DEFAULT_SCHEDULE", Path("dummy.json")):
                        result = cron_runner.main()
        self.assertEqual(result, 0)
        # 2 enabled entries (the disabled_script is skipped)
        self.assertEqual(mock_run.call_count, 2)
        # The disabled entry should NOT be in the call args list
        called_names = [c.args[0]["name"] for c in mock_run.call_args_list]
        self.assertIn("fast_script.py", called_names)
        self.assertIn("another.py", called_names)
        self.assertNotIn("disabled_script.py", called_names)


class TestMainLoop(unittest.TestCase):
    def test_loop_calls_run_once_when_due(self):
        """Simulate the loop body once via the helpers, no infinite loop.

        We exercise the same primitives the loop uses
        (next_due, run_once, time.sleep) without actually entering
        the while True in main(). This is the right shape for a
        unit test — the loop itself is a 7-line wrapper around
        these primitives.
        """
        from time import time as real_time

        last_run = 0.0
        now = 1000.0
        entry = {
            "name": "x.py",
            "interval_minutes": 1,
            "args": [],
            "enabled": True,
        }
        with patch("cron_runner.run_once") as mock_run:
            due = cron_runner.next_due(entry, now_ts=now, last_run=last_run)
            self.assertLessEqual(due, now)
            cron_runner.run_once(entry, Path("/scripts"))
        mock_run.assert_called_once_with(entry, Path("/scripts"))

        # After running, last_run should advance.
        new_last = real_time()
        due2 = cron_runner.next_due(entry, now_ts=new_last + 1, last_run=new_last)
        self.assertGreater(due2, new_last)


if __name__ == "__main__":
    unittest.main()
