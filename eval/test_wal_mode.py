"""Sprint 2 Task 1: PRAGMA journal_mode = WAL.

Verifies the open_db() context manager:
  - Sets journal_mode to WAL on a writable file
  - Does not crash on a read-only file (degraded mode acceptable)
  - Allows concurrent readers + a single writer without SQLITE_BUSY

The 3rd test is the actual reason WAL was added: under bursty save
load, the default rollback journal serializes reads against writes
and the 30s busy_timeout is not enough. With WAL, readers and
writers proceed concurrently.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

AGENTIC_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(AGENTIC_DIR))
sys.path.insert(0, str(AGENTIC_DIR / "eval"))

from memory_common import open_db


class TestWalMode(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="wal_test_"))
        self.db_path = self.tmpdir / "memory.db"
        from memory_common import connection_pool

        connection_pool.clear()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path.with_name(self.db_path.name + suffix)
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except PermissionError:
                pass
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_open_db_enables_wal(self):
        """Fresh open_db() must set journal_mode to WAL on a writable path."""
        with open_db(self.db_path) as db:
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(
                mode.lower(), "wal", f"expected journal_mode=wal, got {mode!r}"
            )
        # The -wal and -shm sidecars may or may not exist after close
        # depending on platform; what matters is the mode returned.

    def test_open_db_on_readonly_path_does_not_raise(self):
        """Read-only path: PRAGMA silently no-ops, open_db still works.

        We simulate read-only by removing write permission. The
        connection still opens (SQLite can read), and our try/except
        around the WAL PRAGMA keeps the context manager from
        propagating the OperationalError.
        """
        # First create a normal DB and close it
        with open_db(self.db_path):
            pass
        # Then mark it read-only and re-open
        os.chmod(self.db_path, 0o444)
        try:
            with open_db(self.db_path) as db:
                # Should be readable
                n = db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
                self.assertGreaterEqual(n, 0)
                # journal_mode may not be 'wal' (read-only refuses the
                # PRAGMA), but we must not have raised. The test passes
                # as long as we got here.
                mode = db.execute("PRAGMA journal_mode").fetchone()[0]
                # Acceptable outcomes: 'wal' (some platforms allow it
                # on read-only), or any other mode. Just don't crash.
                self.assertIsNotNone(mode)
        finally:
            os.chmod(self.db_path, 0o644)

    def test_concurrent_readers_and_writers_no_busy(self):
        """1 writer + 5 readers must all complete without SQLITE_BUSY.

        This is the actual reason for WAL: under the default rollback
        journal, readers block on writers; with WAL, they don't.
        Without the WAL PRAGMA, this test would either take many
        seconds (busy_timeout kicking in) or raise SQLITE_BUSY.
        """
        # Seed a small table so the readers have something to read.
        # Explicit commit() after the inserts to eliminate any
        # ambiguity about transaction visibility — open_db closes
        # the connection on exit which *should* commit, but we
        # don't want this test to depend on that timing.
        with open_db(self.db_path) as db:
            db.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v INTEGER)")
            for i in range(100):
                db.execute(
                    "INSERT OR REPLACE INTO kv VALUES (?, ?)",
                    (f"key{i}", i),
                )
            db.commit()
            seed_count = db.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
            self.assertEqual(seed_count, 100, "seed failed")

        errors: list = []
        read_counts: list = []

        def writer():
            try:
                end = time.time() + 1.0
                with open_db(self.db_path) as db:
                    i = 0
                    while time.time() < end:
                        db.execute(
                            "INSERT OR REPLACE INTO kv VALUES (?, ?)",
                            (f"w{i}", i),
                        )
                        db.commit()
                        i += 1
                        # Anti-thundering-herd: tiny yield between rapid
                        # writer commits so the reader thread sees WAL frames
                        # at a realistic cadence and exercises the WAL->reader
                        # notification path (not just one big batch).
                        time.sleep(0.01)
            except Exception as e:  # noqa: BLE001
                errors.append(("writer", e))

        def reader(idx: int):
            try:
                # Use pooled=True to test WAL mode correctly (readers
                # should not go through the write queue).
                with open_db(self.db_path, pooled=True) as db:
                    seen = 0
                    end = time.time() + 1.0
                    while time.time() < end:
                        n = db.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
                        seen += 1
                        self.assertGreater(n, 0)
                read_counts.append(seen)
            except Exception as e:  # noqa: BLE001
                errors.append((f"reader{idx}", e))

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(5)]
        threads.append(threading.Thread(target=writer))
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.time() - t0

        if errors:
            # If any thread raised, surface the first one
            name, exc = errors[0]
            self.fail(
                f"{name} raised {type(exc).__name__}: {exc} (elapsed={elapsed:.2f}s)"
            )

        # Sanity: every reader ran for the full second and got plenty
        # of successful reads. Without WAL, busy_timeout would have
        # stalled at least some of them.
        self.assertEqual(len(read_counts), 5)
        for c in read_counts:
            self.assertGreater(
                c,
                5,
                f"reader only completed {c} reads in 1s — "
                f"reads are likely blocking on the writer",
            )


if __name__ == "__main__":
    unittest.main()
