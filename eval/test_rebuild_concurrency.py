#!/usr/bin/env python3
"""Rebuild subprocess graceful-skip tests.

Audit-gap fix (2026-06-22 follow-up): when ``rebuild_vec_index.py``
exits non-zero because its cross-process flock is held by another
rebuild, ``handle_vec_index_rebuild`` should return a graceful "skipped"
message rather than raising ``RuntimeError`` (which would mark the
background task as failed and trigger retries).

We don't actually run rebuild_vec_index.py here — it's expensive and
depends on a built vec index.  Instead we mock ``subprocess.run`` to
simulate the two outcomes:

  1. Successful rebuild (returncode 0)
  2. Contended flock — returncode 1 with "Another vec_index rebuild
     is already running." in stderr (the exact string the script
     logs when the flock blocks)
  3. Genuine failure — returncode 1 with a different stderr

Coverage:
    1. Successful rebuild returns the success string.
    2. Contended flock returns the "skipped" string, NOT a raise.
    3. Genuine failure raises RuntimeError (regression).
    4. The handler accepts a default reason when payload is empty.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))


class _FakeResult:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_conn():
    """A no-op stand-in for the sqlite3.Connection the handler accepts.

    The handler doesn't use the conn for the rebuild itself (the
    subprocess does the heavy lifting), so a bare mock is enough.
    """
    return mock.MagicMock()


class TestVecIndexRebuildHandler(unittest.TestCase):
    """handle_vec_index_rebuild error-path handling."""

    def _patch_subprocess(self, result: _FakeResult):
        """Patch subprocess.run to return *result*."""
        return mock.patch("subprocess.run", return_value=result)

    def test_successful_rebuild(self) -> None:
        from background_worker import handle_vec_index_rebuild

        fake = _FakeResult(returncode=0, stdout="vec_idx: 100 keys")
        with self._patch_subprocess(fake):
            result = handle_vec_index_rebuild(
                payload={"reason": "scheduled"},
                conn=_fake_conn(),
                db_path=Path("/tmp/test.db"),
            )
        self.assertIn("vec_idx rebuilt", result)
        self.assertIn("scheduled", result)

    def test_contended_flock_returns_graceful_skip(self) -> None:
        """When the rebuild script says another rebuild is in progress,
        return a graceful skip message — do NOT raise RuntimeError."""
        from background_worker import handle_vec_index_rebuild

        fake = _FakeResult(
            returncode=1,
            stderr="Another vec_index rebuild is already running.\n",
        )
        with self._patch_subprocess(fake):
            result = handle_vec_index_rebuild(
                payload={"reason": "manual"},
                conn=_fake_conn(),
                db_path=Path("/tmp/test.db"),
            )
        self.assertIn("skipped", result)
        self.assertIn("another rebuild is in progress", result)
        # Reason is included in the message for audit-log readability.
        self.assertIn("manual", result)

    def test_contended_flock_skip_works_with_default_reason(self) -> None:
        """Empty payload falls back to the 'scheduled' default reason."""
        from background_worker import handle_vec_index_rebuild

        fake = _FakeResult(
            returncode=1,
            stderr="Another vec_index rebuild is already running.\n",
        )
        with self._patch_subprocess(fake):
            result = handle_vec_index_rebuild(
                payload={},
                conn=_fake_conn(),
                db_path=Path("/tmp/test.db"),
            )
        self.assertIn("skipped", result)
        self.assertIn("scheduled", result)

    def test_genuine_failure_still_raises(self) -> None:
        """Regression: a real failure must still raise RuntimeError so
        the background task machinery treats it as a retryable error."""
        from background_worker import handle_vec_index_rebuild

        fake = _FakeResult(
            returncode=2,
            stderr="Some other error: disk full",
            stdout="",
        )
        with self._patch_subprocess(fake):
            with self.assertRaises(RuntimeError) as ctx:
                handle_vec_index_rebuild(
                    payload={"reason": "scheduled"},
                    conn=_fake_conn(),
                    db_path=Path("/tmp/test.db"),
                )
        self.assertIn("exited 2", str(ctx.exception))
        self.assertIn("disk full", str(ctx.exception))

    def test_contention_message_in_stdout_not_stderr(self) -> None:
        """The script may also write the contention message to stdout."""
        from background_worker import handle_vec_index_rebuild

        fake = _FakeResult(
            returncode=1,
            stdout="Another vec_index rebuild is already running.",
            stderr="",
        )
        with self._patch_subprocess(fake):
            result = handle_vec_index_rebuild(
                payload={"reason": "manual"},
                conn=_fake_conn(),
                db_path=Path("/tmp/test.db"),
            )
        self.assertIn("skipped", result)

    def test_missing_script_still_raises(self) -> None:
        """The script-not-found path is a genuine failure, not a contention."""
        from background_worker import handle_vec_index_rebuild

        # Point at a path that does not exist.
        bad_db = Path("/nonexistent/path/memory.db")
        with self.assertRaises(RuntimeError) as ctx:
            handle_vec_index_rebuild(payload={}, conn=_fake_conn(), db_path=bad_db)
        self.assertIn("not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
