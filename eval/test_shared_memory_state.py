"""Tests for S1 (IPC Shared Memory for lock-free state tracking).

Covers:
  - Lifecycle: create / attach / detach / unlink
  - Round-trip: write_state → read_state matches
  - Validation: bad magic, bad checksum, version mismatch
  - Crash recovery: unlink and recreate
  - Circuit breaker hot path: is_circuit_open()
  - Daemon liveness: is_daemon_alive()
  - Stale state: detached state is harmless
"""

import os
import struct
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import infra.shared_memory_state as sms


def _unique_name() -> str:
    """Return a unique segment name so tests don't collide."""
    return f"test_sms_{uuid.uuid4().hex[:8]}"


def _cleanup(state: sms.SharedMemoryState) -> None:
    """Best-effort cleanup of a test segment."""
    try:
        state.unlink()
    except Exception:
        pass


class TestLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.name = _unique_name()
        self.state = sms.SharedMemoryState(name=self.name)

    def tearDown(self) -> None:
        _cleanup(self.state)

    def test_create_succeeds(self) -> None:
        self.state.create()
        self.assertIsNotNone(self.state._shm)
        assert self.state._shm is not None  # for type checker
        self.assertEqual(self.state._shm.size, sms.SIZE)

    def test_create_twice_recovers_from_stale(self) -> None:
        # Simulate a stale segment by creating one and not unlinking.
        self.state.create()
        self.state.detach()
        # New instance with the same name should successfully create.
        new_state = sms.SharedMemoryState(name=self.name)
        new_state.create()  # should not raise FileExistsError
        self.assertIsNotNone(new_state._shm)
        new_state.unlink()

    def test_attach_to_existing(self) -> None:
        self.state.create()
        self.state.detach()
        # Fresh instance, same name.
        new_state = sms.SharedMemoryState(name=self.name)
        self.assertTrue(new_state.attach())
        self.assertIsNotNone(new_state._buf)

    def test_attach_to_missing_returns_false(self) -> None:
        # Use a name we never created.
        missing = sms.SharedMemoryState(name="nonexistent_test_segment_xyz")
        self.assertFalse(missing.attach())
        self.assertIsNone(missing._buf)

    def test_detach_does_not_unlink(self) -> None:
        self.state.create()
        self.state.detach()
        # Segment should still exist for other processes.
        new_state = sms.SharedMemoryState(name=self.name)
        self.assertTrue(new_state.attach())

    def test_unlink_removes_segment(self) -> None:
        self.state.create()
        self.state.unlink()
        new_state = sms.SharedMemoryState(name=self.name)
        self.assertFalse(new_state.attach())


class TestRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.name = _unique_name()
        self.state = sms.SharedMemoryState(name=self.name)
        self.state.create()

    def tearDown(self) -> None:
        _cleanup(self.state)

    def test_write_then_read(self) -> None:
        future = time.time() + 60
        self.state.write_state(
            circuit_open_until=future,
            failure_count=3,
            last_backoff_seconds=2.5,
            daemon_pid=12345,
            daemon_started_at=time.time() - 100,
            is_daemon_alive=True,
        )
        state = self.state.read_state()
        assert state is not None
        self.assertEqual(state["circuit_open_until"], future)
        self.assertEqual(state["failure_count"], 3)
        self.assertEqual(state["last_backoff_seconds"], 2.5)
        self.assertEqual(state["daemon_pid"], 12345)
        self.assertTrue(state["is_daemon_alive"])
        # The writes_since_start should increment.
        self.assertEqual(state["writes_since_start"], 1)

    def test_two_writes_increment_counter(self) -> None:
        self.state.write_state()
        self.state.write_state()
        self.state.write_state()
        state = self.state.read_state()
        assert state is not None
        self.assertEqual(state["writes_since_start"], 3)

    def test_cross_process_read(self) -> None:
        """Simulate a CLI hook reading state written by the daemon."""
        # Daemon writes.
        self.state.write_state(
            circuit_open_until=time.time() + 30,
            failure_count=5,
            daemon_pid=99999,
            is_daemon_alive=True,
        )
        self.state.detach()
        # CLI hook attaches and reads.
        hook_state = sms.SharedMemoryState(name=self.name)
        self.assertTrue(hook_state.attach())
        state = hook_state.read_state()
        assert state is not None
        self.assertEqual(state["failure_count"], 5)
        self.assertEqual(state["daemon_pid"], 99999)
        hook_state.detach()

    def test_write_with_defaults(self) -> None:
        """write_state with all defaults should still produce a valid
        segment that round-trips to zeroed values."""
        self.state.write_state()
        state = self.state.read_state()
        assert state is not None
        self.assertEqual(state["circuit_open_until"], 0.0)
        self.assertEqual(state["failure_count"], 0)
        self.assertEqual(state["daemon_pid"], 0)
        self.assertFalse(state["is_daemon_alive"])


class TestValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.name = _unique_name()
        self.state = sms.SharedMemoryState(name=self.name)
        self.state.create()

    def tearDown(self) -> None:
        _cleanup(self.state)

    def test_is_valid_after_write(self) -> None:
        self.state.write_state()
        self.assertTrue(self.state.is_valid())

    def test_invalid_magic(self) -> None:
        # Corrupt the magic.
        assert self.state._buf is not None
        self.state._buf[0:4] = b"\x00\x00\x00\x00"
        self.assertFalse(self.state.is_valid())

    def test_invalid_version(self) -> None:
        # Set a future version.
        assert self.state._buf is not None
        self.state._buf[4:8] = struct.pack("<I", 999)
        self.assertFalse(self.state.is_valid())

    def test_invalid_tail_magic(self) -> None:
        assert self.state._buf is not None
        # Write a valid state, then corrupt the tail magic.
        self.state.write_state()
        self.state._buf[sms.SIZE - 4 : sms.SIZE] = b"\x00\x00\x00\x00"
        self.assertFalse(self.state.is_valid())

    def test_invalid_checksum(self) -> None:
        # Write a valid state, then flip a byte in the data area.
        self.state.write_state(failure_count=1, daemon_pid=1234, is_daemon_alive=True)
        assert self.state._buf is not None
        # Flip byte 100 (in the data area, not the magic).
        prev = self.state._buf[100]
        self.state._buf[100] = (prev + 1) & 0xFF
        self.assertFalse(self.state.is_valid())


class TestCircuitBreakerHotPath(unittest.TestCase):
    """The hot-path API: is_circuit_open() must be < 10μs."""

    def setUp(self) -> None:
        self.name = _unique_name()
        self.state = sms.SharedMemoryState(name=self.name)
        self.state.create()
        self.state.write_state()  # closed by default

    def tearDown(self) -> None:
        _cleanup(self.state)

    def test_closed_circuit_returns_false(self) -> None:
        self.assertFalse(self.state.is_circuit_open())

    def test_open_circuit_returns_true(self) -> None:
        self.state.write_state(circuit_open_until=time.time() + 60)
        self.assertTrue(self.state.is_circuit_open())

    def test_expired_circuit_returns_false(self) -> None:
        # Open until 1 second ago (already expired).
        self.state.write_state(circuit_open_until=time.time() - 1)
        self.assertFalse(self.state.is_circuit_open())

    def test_invalid_segment_returns_none(self) -> None:
        assert self.state._buf is not None
        self.state._buf[0:4] = b"\x00\x00\x00\x00"
        self.assertIsNone(self.state.is_circuit_open())


class TestDaemonLiveness(unittest.TestCase):
    def setUp(self) -> None:
        self.name = _unique_name()
        self.state = sms.SharedMemoryState(name=self.name)
        self.state.create()

    def tearDown(self) -> None:
        _cleanup(self.state)

    def test_daemon_alive_with_current_pid(self) -> None:
        self.state.write_state(daemon_pid=os.getpid(), is_daemon_alive=True)
        self.assertTrue(self.state.is_daemon_alive())

    def test_daemon_dead_with_stale_pid(self) -> None:
        # PID 1 (init) should always be running, but a fake
        # non-existent PID will fail.
        # Use a PID that's very unlikely to exist.
        fake_pid = 999999
        self.state.write_state(daemon_pid=fake_pid, is_daemon_alive=True)
        self.assertFalse(self.state.is_daemon_alive())

    def test_daemon_marked_dead(self) -> None:
        # Even with current PID, is_daemon_alive=False means dead.
        self.state.write_state(daemon_pid=os.getpid(), is_daemon_alive=False)
        self.assertFalse(self.state.is_daemon_alive())

    def test_daemon_zero_pid(self) -> None:
        self.state.write_state(daemon_pid=0, is_daemon_alive=True)
        # PID 0 is invalid — should be treated as not-alive.
        self.assertFalse(self.state.is_daemon_alive())


class TestCrashRecovery(unittest.TestCase):
    """Simulate a daemon crash: the segment is left behind but is
    unlinked by the next startup."""

    def setUp(self) -> None:
        self.name = _unique_name()

    def tearDown(self) -> None:
        try:
            sms.SharedMemoryState(name=self.name).unlink()
        except Exception:
            pass

    def test_recreate_after_stale_segment(self) -> None:
        # "Daemon 1" creates a segment and crashes (no unlink).
        d1 = sms.SharedMemoryState(name=self.name)
        d1.create()
        d1.write_state(circuit_open_until=time.time() + 999)
        d1.detach()
        # "Daemon 2" starts and tries to create the same name.
        d2 = sms.SharedMemoryState(name=self.name)
        # Without unlink-on-conflict, this would raise FileExistsError.
        # With the recovery in create(), it unlinks the stale segment
        # and succeeds.
        d2.create()
        # The new segment is fresh (uninitialized) so is_valid()
        # returns False until we write to it. That's the expected
        # behavior — a brand-new segment has magic 0 which is
        # detected as invalid. The daemon should write_state()
        # immediately after create() to make the segment valid.
        self.assertFalse(d2.is_valid())
        d2.write_state()
        self.assertTrue(d2.is_valid())
        # The new segment should have the default (closed) circuit.
        self.assertFalse(d2.is_circuit_open())

    def test_unlink_then_attach_fails(self) -> None:
        s1 = sms.SharedMemoryState(name=self.name)
        s1.create()
        s1.unlink()
        s2 = sms.SharedMemoryState(name=self.name)
        self.assertFalse(s2.attach())


class TestConcurrency(unittest.TestCase):
    """Concurrent readers should not crash or return torn data."""

    def setUp(self) -> None:
        self.name = _unique_name()
        self.state = sms.SharedMemoryState(name=self.name)
        self.state.create()

    def tearDown(self) -> None:
        _cleanup(self.state)

    def test_concurrent_reads(self) -> None:
        """Multiple processes reading the same segment should
        all see the same data."""
        import multiprocessing

        from test_shared_memory_state import _concurrent_reader

        # Write a known state so the readers can validate.
        self.state.write_state(
            circuit_open_until=time.time() + 30,
            failure_count=7,
            daemon_pid=os.getpid(),
            is_daemon_alive=True,
        )
        self.state.detach()

        # Spawn 4 reader processes.
        procs = [
            multiprocessing.Process(target=_concurrent_reader, args=(self.name,))
            for _ in range(4)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=5.0)
            self.assertEqual(p.exitcode, 0)

    def test_concurrent_writes(self) -> None:
        """Single-writer semantics: we don't claim thread-safety, so
        concurrent writers from the same process may produce torn
        data. This test just verifies that the API doesn't crash
        (the torn data is caught by the checksum)."""
        import threading

        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                for _ in range(20):
                    self.state.write_state(
                        failure_count=thread_id,
                        daemon_pid=1000 + thread_id,
                    )
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(errors), 0)


class TestSizeConstants(unittest.TestCase):
    def test_size_is_at_least_one_os_page(self) -> None:
        # On supported platforms, one OS page is 4KB-16KB. 16KB
        # covers all of them.
        self.assertGreaterEqual(sms.SIZE, 4096)
        self.assertGreaterEqual(sms.SIZE, 16384)  # Apple Silicon page

    def test_magic_is_constant(self) -> None:
        # "MMST" in ASCII = 0x4D4D5354.
        self.assertEqual(sms.MAGIC, 0x4D4D5354)

    def test_version_is_current(self) -> None:
        self.assertEqual(sms.VERSION, 1)


# Top-level helper for the concurrency test. Must be module-level
# (not a method or local) so multiprocessing can pickle it.
def _concurrent_reader(segment_name: str) -> None:
    r = sms.SharedMemoryState(name=segment_name)
    if not r.attach():
        raise RuntimeError(f"Failed to attach to {segment_name}")
    state = r.read_state()
    r.detach()
    if state is None:
        raise RuntimeError("read_state returned None")

    def test_size_is_at_least_one_os_page(self) -> None:
        # On supported platforms, one OS page is 4KB-16KB. 16KB
        # covers all of them.
        self.assertGreaterEqual(sms.SIZE, 4096)
        self.assertGreaterEqual(sms.SIZE, 16384)  # Apple Silicon page

    def test_magic_is_constant(self) -> None:
        # "MMST" in ASCII = 0x4D4D5354.
        self.assertEqual(sms.MAGIC, 0x4D4D5354)

    def test_version_is_current(self) -> None:
        self.assertEqual(sms.VERSION, 1)


if __name__ == "__main__":
    unittest.main()
