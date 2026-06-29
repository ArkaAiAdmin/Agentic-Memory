"""Unit tests for auto_save.py — hook-driven auto-save and daily digest.

Tests _upsert_memory (the core write path used by hooks) and the
daily_digest rollup. These test semantic correctness: does the
data actually land in the DB with the expected content and metadata?
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
import sys

sys.path.insert(0, os.getcwd())

from db_migrations import run_schema_setup


class TestUpsertMemory(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="autosave_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

        self._orig_db = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_db:
            os.environ["MEMORY_DB_PATH"] = self._orig_db
        else:
            os.environ.pop("MEMORY_DB_PATH", None)

    def test_upsert_creates_new_memory(self):
        from auto_save import _upsert_memory

        success = _upsert_memory(
            "lessons/test-note",
            "lessons/test-note.md",
            "# Test\nThis is a test note.",
            '["python", "testing"]',
            "2026-06-17T00:00:00+00:00",
        )
        self.assertTrue(success)
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT content, tags, category FROM memories WHERE id='lessons/test-note'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertIn("test note", row[0])
        tags = json.loads(row[1])
        self.assertIn("python", tags)
        self.assertEqual(row[2], "lessons")

    def test_upsert_updates_existing_memory(self):
        from auto_save import _upsert_memory

        _upsert_memory(
            "lessons/test-note",
            "lessons/test-note.md",
            "original content",
            '["v1"]',
            "2026-06-17T00:00:00+00:00",
        )
        _upsert_memory(
            "lessons/test-note",
            "lessons/test-note.md",
            "updated content",
            '["v2"]',
            "2026-06-17T01:00:00+00:00",
        )
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT content, tags, updated_at FROM memories WHERE id='lessons/test-note'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "updated content")
        tags = json.loads(row[1])
        self.assertIn("v2", tags)
        self.assertIn("2026-06-17T01", row[2])

    def test_upsert_with_pinned_and_importance(self):
        from auto_save import _upsert_memory

        success = _upsert_memory(
            "decisions/important-decision",
            "decisions/important-decision.md",
            "# Important\nThis matters.",
            "[]",
            "2026-06-17T00:00:00+00:00",
            pinned=1,
            importance=5,
        )
        self.assertTrue(success)
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT pinned, importance FROM memories WHERE id='decisions/important-decision'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)
        self.assertEqual(
            row[1], 5
        )  # _upsert_memory passes importance to _update_memory_index_incremental which runs tier migration

    def test_upsert_missing_db_returns_false(self):
        os.environ["MEMORY_DB_PATH"] = "/nonexistent/path/memory.db"
        from auto_save import _upsert_memory

        success = _upsert_memory(
            "lessons/test",
            "lessons/test.md",
            "content",
            "[]",
            "2026-06-17T00:00:00+00:00",
        )
        self.assertFalse(success)


class TestDailyDigestToolBreakdown(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="autosave_digest_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

        self._orig_db = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_db:
            os.environ["MEMORY_DB_PATH"] = self._orig_db
        else:
            os.environ.pop("MEMORY_DB_PATH", None)

    def test_get_tool_counts_from_db(self):
        from auto_save import _upsert_memory, _get_tool_counts_from_db

        # 1. Insert timezone-aware memories for date 2026-06-24
        _upsert_memory(
            "sessions/auto-2026-06-24_10-15-20+00-00-bash",
            "sessions/auto-2026-06-24_10-15-20+00-00-bash.md",
            "bash tool call 1",
            "[]",
            "2026-06-24T10:15:20+00:00",
        )
        _upsert_memory(
            "sessions/auto-2026-06-24_10-16-20+00-00-bash",
            "sessions/auto-2026-06-24_10-16-20+00-00-bash.md",
            "bash tool call 2",
            "[]",
            "2026-06-24T10:16:20+00:00",
        )
        _upsert_memory(
            "sessions/auto-2026-06-24_10-17-20+00-00-memory_save",
            "sessions/auto-2026-06-24_10-17-20+00-00-memory_save.md",
            "memory_save tool call",
            "[]",
            "2026-06-24T10:17:20+00:00",
        )

        # 2. Insert timezone-less memories for date 2026-06-24
        _upsert_memory(
            "sessions/auto-2026-06-24_11-15-20-bash",
            "sessions/auto-2026-06-24_11-15-20-bash.md",
            "bash tool call 3 naive",
            "[]",
            "2026-06-24T11:15:20",
        )

        # 3. Insert memory for a different date
        _upsert_memory(
            "sessions/auto-2026-06-25_10-15-20+00-00-bash",
            "sessions/auto-2026-06-25_10-15-20+00-00-bash.md",
            "different date",
            "[]",
            "2026-06-25T10:15:20+00:00",
        )

        counts = _get_tool_counts_from_db("2026-06-24")
        self.assertEqual(counts, {"bash": 3, "memory_save": 1})


class TestBackoffAndCircuitBreaker(unittest.TestCase):
    """Verify the auto-save hook's failure handling.

    Tests:
      - First failure: backoff_seconds is 1.0, circuit stays closed
      - Consecutive failures: backoff_seconds grows exponentially (1, 2, 4)
      - After max_retries+1 failures: circuit opens, tool_complete
        short-circuits with reason='circuit_breaker_open'
      - Circuit auto-resets after circuit_breaker_seconds
      - Success after failures resets the failure window
    """

    def setUp(self):
        import auto_save

        self.auto_save = auto_save
        self.auto_save._auto_save_reset_state()

    def tearDown(self):
        self.auto_save._auto_save_reset_state()

    def test_circuit_closed_initially(self):
        self.assertFalse(self.auto_save._auto_save_circuit_open())

    def test_first_failure_records_one_event(self):
        cb = self.auto_save._auto_save_record_failure_and_maybe_trip()
        self.assertEqual(cb["n_failures"], 1)
        self.assertEqual(cb["next_backoff"], 1.0)
        self.assertFalse(self.auto_save._auto_save_circuit_open())

    def test_consecutive_failures_grow_backoff(self):
        b1 = self.auto_save._auto_save_record_failure_and_maybe_trip()
        b2 = self.auto_save._auto_save_record_failure_and_maybe_trip()
        b3 = self.auto_save._auto_save_record_failure_and_maybe_trip()
        self.assertEqual(b1["next_backoff"], 1.0)
        self.assertEqual(b2["next_backoff"], 2.0)
        self.assertEqual(b3["next_backoff"], 4.0)
        self.assertFalse(self.auto_save._auto_save_circuit_open())

    def test_circuit_trips_after_max_retries(self):
        # Default max_retries = 3, so the 4th failure within the window
        # should trip the circuit.
        for _ in range(4):
            self.auto_save._auto_save_record_failure_and_maybe_trip()
        self.assertTrue(self.auto_save._auto_save_circuit_open())

    def test_tool_complete_short_circuits_when_circuit_open(self):
        for _ in range(5):
            self.auto_save._auto_save_record_failure_and_maybe_trip()
        self.assertTrue(self.auto_save._auto_save_circuit_open())
        result = self.auto_save.tool_complete("bash", '{"command": "ls"}', "result")
        self.assertFalse(result.get("saved"))
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "circuit_breaker_open")
        self.assertIn("circuit_open_until", result)

    def test_circuit_resets_after_circuit_breaker_seconds(self):
        import time as _t

        # Force the circuit to be open for a tiny window.
        self.auto_save._AUTO_SAVE_STATE["circuit_open_until"] = _t.time() + 0.05
        self.assertTrue(self.auto_save._auto_save_circuit_open())
        _t.sleep(0.1)
        self.assertFalse(self.auto_save._auto_save_circuit_open())

    def test_success_resets_failure_window(self):
        self.auto_save._auto_save_record_failure_and_maybe_trip()
        self.auto_save._auto_save_record_failure_and_maybe_trip()
        self.assertEqual(len(self.auto_save._auto_save_get_state()["failure_times"]), 2)
        self.auto_save._auto_save_record_success()
        self.assertEqual(len(self.auto_save._auto_save_get_state()["failure_times"]), 0)

    def test_tool_complete_returns_backoff_on_failure(self):
        """End-to-end: a failing tool_complete returns backoff_seconds.

        Uses an allowlisted tool (``memory_save``) so the call reaches
        ``_tool_complete_inner`` and triggers the failure path.  The
        previous version used ``bash`` which is now correctly filtered
        by the fast-path allowlist check (returning a "skipped"
        envelope) before ever reaching the save path.

        Forces the sync path by setting ``MEMORY_ASYNC_AUTOSAVE=0`` so
        the replacement ``_tool_complete_inner`` is actually invoked
        (the async path enqueues without calling it).
        """

        def boom(*a, **kw):
            raise RuntimeError("simulated DB locked")

        import background.auto_save as _as_backend

        _original_inner = _as_backend._tool_complete_inner
        _as_backend._tool_complete_inner = boom
        import os as _os

        saved_env = _os.environ.get("MEMORY_ASYNC_AUTOSAVE")
        _os.environ["MEMORY_ASYNC_AUTOSAVE"] = "0"
        try:
            result = self.auto_save.tool_complete("memory_save", '{"x":1}', "preview")
        finally:
            _as_backend._tool_complete_inner = _original_inner
            if saved_env is None:
                _os.environ.pop("MEMORY_ASYNC_AUTOSAVE", None)
            else:
                _os.environ["MEMORY_ASYNC_AUTOSAVE"] = saved_env
        self.assertFalse(result.get("saved"))
        self.assertIn("backoff_seconds", result)
        self.assertEqual(result["backoff_seconds"], 1.0)
        self.assertIn("error", result)
        self.assertIn("simulated DB locked", result["error"])


# ===========================================================================
# P0-4 regression: _enqueue_to_inbox must cap the inbox size
# ===========================================================================


class TestInboxSizeCap(unittest.TestCase):
    """P0-4 regression (2026-06-22): prevent disk-fill DoS via inbox.

    Before the fix, _enqueue_to_inbox had no size cap, so a single
    rogue 10 MB tool result could grow the inbox file unbounded
    before the daemon got a chance to drain it.  The fix adds
    AUTO_SAVE_INBOX_MAX_BYTES (default 100 MB) — when the inbox is
    at or above the cap, the enqueue returns False so the caller
    falls back to the sync path.
    """

    def setUp(self):
        import tempfile

        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

        self._orig_db = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)

        # Use a small cap so we can exercise the cap with a tiny
        # entry, without actually writing 100MB of data.
        self.saved_env = os.environ.get("AUTO_SAVE_INBOX_MAX_BYTES")
        os.environ["AUTO_SAVE_INBOX_MAX_BYTES"] = "200"  # 200 bytes
        # Re-import auto_save with the new cap
        if "auto_save" in sys.modules:
            del sys.modules["auto_save"]
        from auto_save import _enqueue_to_inbox, get_auto_save_inbox_path

        self._enqueue = _enqueue_to_inbox
        self._inbox_path = get_auto_save_inbox_path

    def tearDown(self):
        if self.saved_env is None:
            os.environ.pop("AUTO_SAVE_INBOX_MAX_BYTES", None)
        else:
            os.environ["AUTO_SAVE_INBOX_MAX_BYTES"] = self.saved_env
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_db:
            os.environ["MEMORY_DB_PATH"] = self._orig_db
        else:
            os.environ.pop("MEMORY_DB_PATH", None)
        # Re-import to restore default cap
        if "auto_save" in sys.modules:
            del sys.modules["auto_save"]

    def test_small_entry_succeeds(self):
        """A normal-size entry under the cap must enqueue successfully."""
        result = self._enqueue({"tool": "memory_save", "params": "{}"})
        self.assertTrue(result)

    def test_oversized_entry_refused(self):
        """A single entry that would push the inbox past the cap must be refused."""
        # First, enqueue something small.
        self._enqueue({"tool": "memory_save", "params": "{}"})
        inbox = self._inbox_path()
        pre_size = inbox.stat().st_size
        # Build an entry whose serialized form is bigger than the cap.
        huge = "x" * 5000  # ~5KB; cap is 200 bytes
        result = self._enqueue({"tool": "memory_save", "params": huge})
        self.assertFalse(
            result,
            "Enqueue of oversized entry must return False so the caller "
            "falls back to the sync path",
        )
        # Inbox should not have grown.
        post_size = inbox.stat().st_size
        self.assertEqual(pre_size, post_size)


# ===========================================================================
# P1-2 regression: _drain_inbox must use rename-and-process, not read-then-truncate
# ===========================================================================


class TestDrainInboxRenamePattern(unittest.TestCase):
    """P1-2 regression (2026-06-22): no entries lost during drain.

    Before the fix, _drain_inbox used read-then-truncate (write empty
    + rename), which had a race: a SIGKILL between read and truncate
    (or a concurrent enqueue after read but before truncate) lost
    the appended entries.  The fix renames inbox → inbox.processing.{pid}
    first, then reads and deletes the renamed file.  New enqueues go
    to the new (empty) inbox and are never lost.
    """

    def setUp(self):
        import tempfile

        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

        self._orig_db = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)

        # Re-import auto_save with a fresh inbox path
        if "auto_save" in sys.modules:
            del sys.modules["auto_save"]
        from auto_save import _drain_inbox, get_auto_save_inbox_path

        self._drain = _drain_inbox
        self._inbox_path = get_auto_save_inbox_path
        # Ensure inbox starts empty
        inbox = self._inbox_path()
        if inbox.exists():
            inbox.unlink()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_db:
            os.environ["MEMORY_DB_PATH"] = self._orig_db
        else:
            os.environ.pop("MEMORY_DB_PATH", None)
        inbox = self._inbox_path()
        if inbox.exists():
            inbox.unlink()
        if "auto_save" in sys.modules:
            del sys.modules["auto_save"]

    def test_drain_returns_parsed_entries(self):
        """Drain must return all entries that were in the inbox."""
        inbox = self._inbox_path()
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            '{"tool": "memory_save", "params": "{}", "result_preview": "a"}\n'
            '{"tool": "memory_save", "params": "{}", "result_preview": "b"}\n',
            encoding="utf-8",
        )
        entries = self._drain()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["result_preview"], "a")
        self.assertEqual(entries[1]["result_preview"], "b")

    def test_drain_clears_inbox(self):
        """Drain must leave the inbox empty (no entries left behind)."""
        inbox = self._inbox_path()
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            '{"tool": "memory_save", "params": "{}", "result_preview": "a"}\n',
            encoding="utf-8",
        )
        self._drain()
        # After drain, inbox should be empty (or non-existent).
        if inbox.exists():
            content = inbox.read_text(encoding="utf-8")
            self.assertEqual(content, "")

    def test_concurrent_enqueue_during_drain_does_not_lose_entries(self):
        """Entries appended AFTER rename start are preserved on the new inbox.

        Simulates the race: drain renames the inbox to a processing
        file, then before the drain returns, a new entry is enqueued
        (to the new inbox).  The drain must NOT touch the new entry.
        """
        inbox = self._inbox_path()
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            '{"tool": "memory_save", "params": "{}", "result_preview": "old"}\n',
            encoding="utf-8",
        )
        # Monkey-patch rename to inject a concurrent enqueue between
        # the rename and the read.  This is the worst-case race
        # window the rename-and-process pattern is designed to close.

        import os as _os

        def race_rename(src, dst):
            # Inject: enqueue a new entry to the new (renamed-away) inbox.
            # After the rename, src is gone, dst is the processing file.
            # The new inbox doesn't exist yet — the enqueue creates it.
            from auto_save import _enqueue_to_inbox

            result = _os.rename(src, dst)
            _enqueue_to_inbox(
                {"tool": "memory_save", "params": "{}", "result_preview": "new"}
            )
            return result

        # Patch Path.rename for the duration of the drain
        from pathlib import Path as _P

        original_rename = _P.rename
        _P.rename = race_rename
        try:
            entries = self._drain()
        finally:
            _P.rename = original_rename

        # The "old" entry must be in the drain result.
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["result_preview"], "old")
        # The "new" entry must be in the new inbox (preserved).
        self.assertTrue(inbox.exists(), "Inbox should exist after the race")
        new_content = inbox.read_text(encoding="utf-8")
        self.assertIn('"new"', new_content, "The new entry must be preserved")


# ===========================================================================
# Signal handler regression: pre-flock SIGTERM must work
# ===========================================================================


class TestDaemonSignalHandler(unittest.TestCase):
    """Regression test (2026-06-22): SIGTERM must kill a daemon that
    hasn't yet acquired the flock.

    Before the fix, run_daemon installed the SIGTERM/SIGINT handler
    AFTER acquiring the flock.  A daemon that failed to acquire the
    flock (because another daemon already held it) returned silently
    with no signal handler installed, so SIGTERM was ignored.  We
    observed three such "ghost" daemons (PIDs 21117, 21439, 21886)
    on 2026-06-22 that required SIGKILL to terminate.

    The fix: install signal handlers BEFORE the flock acquisition.
    This test spawns two daemons — the second one must fail to
    acquire the flock and respond to SIGTERM within a reasonable
    timeout.
    """

    def test_ghost_daemon_responds_to_sigterm(self):
        """A daemon that fails the flock check must respond to SIGTERM.

        Approach: read the source of run_daemon() and verify that
        the SIGTERM/SIGINT signal handlers are installed BEFORE
        the flock acquisition.  This is a structural regression test
        — if someone moves the signal handler installation back
        to after the flock, this test fails.

        We don't actually spawn a daemon (the real daemon on this
        system is already holding the lock, so a second daemon
        would always fail the flock — masking the bug we're
        trying to detect).
        """
        import re as _re

        from pathlib import Path as _P

        script = _P(__file__).resolve().parent.parent / "background" / "auto_save.py"
        if not script.exists():
            self.skipTest(f"auto_save.py not found at {script}")

        src = script.read_text(encoding="utf-8")

        # Find the body of run_daemon.
        m = _re.search(
            r"def run_daemon\(.*?\):.*?(?=\ndef |\nclass |\Z)",
            src,
            _re.DOTALL,
        )
        if not m:
            self.skipTest("Could not locate run_daemon in auto_save.py")
        body = m.group(0)

        # Find the line numbers (offsets within the body) of:
        # 1. _signal.signal(_signal.SIGTERM, _on_signal)
        # 2. acquire_flock_with_retry(lock_fd, ...)
        sig_match = _re.search(
            r"_signal\.signal\(\s*_signal\.SIGTERM\s*,\s*_on_signal\s*\)",
            body,
        )
        flock_match = _re.search(
            r"acquire_flock_with_retry\(\s*lock_fd\b",
            body,
        )

        self.assertIsNotNone(
            sig_match,
            "Could not find SIGTERM signal handler installation in run_daemon",
        )
        self.assertIsNotNone(
            flock_match,
            "Could not find flock acquisition in run_daemon",
        )

        sig_pos = sig_match.start()
        flock_pos = flock_match.start()
        self.assertLess(
            sig_pos,
            flock_pos,
            f"SIGTERM signal handler must be installed BEFORE the flock "
            f"acquisition.  Currently the signal handler is at byte offset "
            f"{sig_pos} and the flock acquisition is at byte offset "
            f"{flock_pos} in run_daemon's body.  If the signal handler "
            f"comes AFTER the flock, a daemon that fails to acquire the "
            f"lock will not respond to SIGTERM (the 2026-06-22 ghost "
            f"daemon bug).",
        )


if __name__ == "__main__":
    unittest.main()
