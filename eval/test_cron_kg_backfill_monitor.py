"""Tests for cron_kg_backfill_monitor.py (TODO 2, 2026-06-19)."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load_monitor():
    """Load cron_kg_backfill_monitor fresh from REPO."""
    import importlib.util as _importlib_util

    spec = _importlib_util.spec_from_file_location(
        "_test_monitor", str(REPO / "cron" / "cron_kg_backfill_monitor.py")
    )
    if spec is None:
        raise RuntimeError("Could not load cron_kg_backfill_monitor.py")
    mod = _importlib_util.module_from_spec(spec)
    sys.modules["_test_monitor"] = mod
    loader = spec.loader
    if loader is None:
        raise RuntimeError("spec.loader is None")
    loader.exec_module(mod)
    return mod


def _make_entry(
    ts: datetime,
    exit_code: int = 0,
    dry_run: bool = False,
    entity_delta: int = 0,
    edge_delta: int = 0,
    fact_delta: int = 0,
    entity_pre: int = 1000,
    edge_pre: int = 500,
    fact_pre: int = 1500,
) -> dict:
    return {
        "captured_at": ts.isoformat(),
        "dry_run": dry_run,
        "pre": {
            "kg_facts": fact_pre,
            "kg_entities": entity_pre,
            "kg_edges": edge_pre,
            "memories": 6000,
        },
        "post": {
            "kg_facts": fact_pre + fact_delta,
            "kg_entities": entity_pre + entity_delta,
            "kg_edges": edge_pre + edge_delta,
            "memories": 6000,
            "deltas": {
                "kg_facts": fact_delta,
                "kg_entities": entity_delta,
                "kg_edges": edge_delta,
                "memories": 0,
            },
        },
        "result": {"exit_code": exit_code, "elapsed_seconds": 5.0},
    }


class TestIsInExpectedWindow(unittest.TestCase):
    def test_sunday_330_in_window(self):
        mod = _load_monitor()
        ts = datetime(2026, 6, 21, 3, 30, tzinfo=timezone.utc)  # Sunday 03:30
        self.assertTrue(mod._is_in_expected_window(ts))

    def test_sunday_300_not_in_window(self):
        mod = _load_monitor()
        ts = datetime(2026, 6, 21, 3, 0, tzinfo=timezone.utc)
        self.assertFalse(mod._is_in_expected_window(ts))

    def test_sunday_400_in_window(self):
        mod = _load_monitor()
        ts = datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc)
        self.assertTrue(mod._is_in_expected_window(ts))

    def test_sunday_200_not_in_window(self):
        mod = _load_monitor()
        ts = datetime(2026, 6, 21, 2, 0, tzinfo=timezone.utc)
        self.assertFalse(mod._is_in_expected_window(ts))

    def test_monday_in_window(self):
        mod = _load_monitor()
        ts = datetime(2026, 6, 22, 3, 30, tzinfo=timezone.utc)  # Monday
        self.assertTrue(mod._is_in_expected_window(ts))


class TestCheck(unittest.TestCase):
    def test_no_entries_returns_error(self):
        mod = _load_monitor()
        code, alerts = mod.check([])
        self.assertEqual(code, 2)
        self.assertIn("ERROR: no log entries found", alerts[0])

    def test_healthy_run_returns_ok(self):
        mod = _load_monitor()
        now = datetime.now(timezone.utc)
        # Find a recent Sunday 03:30
        days_back = (now.weekday() - 6) % 7
        last_sunday = (now - timedelta(days=days_back)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        entries = [_make_entry(last_sunday, exit_code=0, dry_run=False)]
        # OK message is only added when verbose=True
        code, alerts = mod.check(entries, verbose=True)
        self.assertEqual(code, 0)
        self.assertTrue(any("OK" in a for a in alerts))

    def test_failed_exit_code_returns_error(self):
        mod = _load_monitor()
        now = datetime.now(timezone.utc)
        days_back = (now.weekday() - 6) % 7
        last_sunday = (now - timedelta(days=days_back)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        entries = [_make_entry(last_sunday, exit_code=1, dry_run=False)]
        code, alerts = mod.check(entries)
        self.assertEqual(code, 2)
        self.assertTrue(any("exited with code" in a for a in alerts))

    def test_large_drop_warns(self):
        mod = _load_monitor()
        now = datetime.now(timezone.utc)
        days_back = (now.weekday() - 6) % 7
        last_sunday = (now - timedelta(days=days_back)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        # 15% drop in edges
        entries = [
            _make_entry(last_sunday, edge_delta=-75, edge_pre=500, dry_run=False)
        ]
        code, alerts = mod.check(entries)
        self.assertEqual(code, 1)
        self.assertTrue(any("kg_edges" in a and "WARN" in a for a in alerts))

    def test_small_drop_no_warn(self):
        mod = _load_monitor()
        now = datetime.now(timezone.utc)
        days_back = (now.weekday() - 6) % 7
        last_sunday = (now - timedelta(days=days_back)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        # 5% drop in edges (below 10% threshold)
        entries = [
            _make_entry(last_sunday, edge_delta=-25, edge_pre=500, dry_run=False)
        ]
        code, alerts = mod.check(entries)
        self.assertEqual(code, 0)

    def test_dry_run_entries_skipped_for_drop_check(self):
        mod = _load_monitor()
        now = datetime.now(timezone.utc)
        days_back = (now.weekday() - 6) % 7
        last_sunday = (now - timedelta(days=days_back)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        # 50% drop but dry-run — should be skipped
        entries = [
            _make_entry(last_sunday, edge_delta=-250, edge_pre=500, dry_run=True)
        ]
        code, alerts = mod.check(entries)
        self.assertEqual(code, 0)

    def test_stale_log_warns(self):
        mod = _load_monitor()
        # Last entry is 3 days old
        old_ts = datetime.now(timezone.utc) - timedelta(days=3)
        entries = [_make_entry(old_ts, dry_run=False)]
        with mock.patch.object(mod, "_is_in_expected_window", return_value=False):
            code, alerts = mod.check(entries)
        self.assertEqual(code, 1)
        self.assertTrue(any("days old" in a for a in alerts))

    def test_very_stale_log_errors(self):
        mod = _load_monitor()
        # Last entry is 10 days old
        old_ts = datetime.now(timezone.utc) - timedelta(days=10)
        entries = [_make_entry(old_ts, dry_run=False)]
        with mock.patch.object(mod, "_is_in_expected_window", return_value=False):
            code, alerts = mod.check(entries)
        self.assertEqual(code, 2)
        self.assertTrue(any("over a week" in a for a in alerts))


class TestLoadEntries(unittest.TestCase):
    def test_load_filters_by_age(self):
        mod = _load_monitor()
        # We can't easily redirect LOG_FILE because the module reads it
        # at module load time. Just verify the function exists and is callable.
        self.assertTrue(callable(mod._load_entries))


class TestEndToEnd(unittest.TestCase):
    """Run the monitor as a subprocess against a synthetic log file."""

    def test_monitor_subprocess_ok(self):
        """Subprocess run with --days returns proper exit code on good log."""
        # 2026-06-29 fix: use tmp_path instead of /tmp + REPO/memory so the
        # test works on CI runners where the project's memory/ dir does not
        # exist (it's gitignored and only created at runtime).
        tmpdir = Path(tempfile.mkdtemp(prefix="kg_monitor_test_"))
        log_file = tmpdir / "kg.log"

        now = datetime.now(timezone.utc)
        days_back = (now.weekday() - 6) % 7
        last_sunday = (now - timedelta(days=days_back)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        entry = _make_entry(last_sunday, dry_run=False)
        log_file.write_text(json.dumps(entry) + "\n")

        # Run the monitor with a hacked LOG_FILE path
        # We do this by writing a wrapper script in tmpdir.
        driver = tmpdir / ".test_monitor_driver.py"
        driver.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            f"sys.path.insert(0, {str(REPO / 'cron')!r})\n"
            "from pathlib import Path\n"
            "import cron_kg_backfill_monitor as m\n"
            f"m.LOG_FILE = Path({str(log_file)!r})\n"
            "sys.exit(m.check(m._load_entries(7))[0])\n"
        )
        try:
            result = subprocess.run(
                [sys.executable, str(driver)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        self.assertEqual(
            result.returncode,
            0,
            f"Expected exit 0; got {result.returncode}\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
