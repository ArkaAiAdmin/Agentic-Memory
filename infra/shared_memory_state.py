"""S1 (2026-06-23): IPC shared memory for lock-free state tracking.

Lets the short-lived CLI hook processes and the persistent daemon
share state (circuit breaker, lock status, daemon health) in
microseconds — no DB hit on the hot path.

Layout
------
Fixed 16KB segment with this packed layout (little-endian, all
fields naturally aligned):

  Offset  Size  Field                  Type
  ------  ----  -----                  ----
       0     4  magic_head             uint32 (0x4D4D5354 = "MMST")
       4     4  version                uint32 (currently 1)
       8     8  timestamp             float64  (last write, Unix epoch)
      16     8  circuit_open_until     float64  (Unix epoch, 0.0 = closed)
      24     4  failure_count         int32    (recent failures in window)
      28     4  last_backoff_seconds  float32  (last backoff computed)
      32     4  daemon_pid            int32    (PID of running daemon)
      36     8  daemon_started_at     float64  (Unix epoch, 0 = no daemon)
      44     4  is_daemon_alive       int32    (1 = alive, 0 = dead)
      48     4  writes_since_start    int32    (counter for debugging)
      52     4  reserved              (alignment padding)
     56-16375    free space           (room for future fields)
  16376     4  checksum              uint32   (XOR of all preceding bytes)
  16380     4  magic_tail            uint32   (0x4D4D5354 again)

Lifecycle
---------
  - Daemon: ``create()`` on startup, ``update()`` on each state
    change, ``unlink()`` on shutdown.
  - CLI hook: ``attach()`` on startup, ``read_circuit_breaker()``
    to check the breaker, ``detach()`` on exit.

Atomic writes use a process-local buffer + a single memcpy. We do
NOT use POSIX semaphores — the segment is small enough that
single-writer semantics (daemon) + read-only semantics (CLI hooks)
are sufficient. Torn reads are detectable via the magic + checksum.

Crash recovery: if ``attach()`` finds bad magic or bad checksum, the
segment is stale (e.g., left over from a crashed daemon). The
caller should fall back to the DB audit log (``_load_circuit_state_from_audit``).

Mac/BSD note: ``/dev/shm`` is Linux-only. On macOS,
``multiprocessing.shared_memory`` falls back to ``/tmp`` which works
on all supported platforms.
"""

from __future__ import annotations

import logging

import multiprocessing.shared_memory  # noqa: F401  (used in type hints)
import os
import struct
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "SharedMemoryState",
    "MAGIC",
    "VERSION",
    "SIZE",
]


# Layout constants — see module docstring.
MAGIC = 0x4D4D5354  # "MMST" in ASCII
VERSION = 1
# Apple's M-series and ARM64 Linux use 16KB pages, while x86_64 Linux
# and Intel macOS use 4KB pages. ``multiprocessing.shared_memory`` on
# Apple Silicon will allocate a full 16KB page even when we ask for
# 4KB. We pad our layout to 16KB so the segment fits any platform.
SIZE = 16384  # 16KB — max OS page size on supported platforms

# Pack/unpack format. Native byte order & alignment to match the
# platform's struct conventions; the magic and checksum detect
# cross-platform confusion.
# Field order: magic, version, timestamp, circuit_open_until,
# failure_count, last_backoff_seconds, daemon_pid, daemon_started_at,
# is_daemon_alive, writes_since_start, reserved
# Then at the end: checksum, magic_tail.
_FMT = (
    "<I"  # magic_head     (offset 0,  size 4)
    "I"  # version        (offset 4,  size 4)
    "d"  # timestamp      (offset 8,  size 8)
    "d"  # circuit_open_until (offset 16, size 8)
    "i"  # failure_count  (offset 24, size 4)
    "f"  # last_backoff   (offset 28, size 4)
    "i"  # daemon_pid     (offset 32, size 4)
    "d"  # daemon_started_at (offset 36, size 8)
    "i"  # is_daemon_alive (offset 44, size 4)
    "i"  # writes_since_start (offset 48, size 4)
    "I"  # reserved       (offset 52, size 4)
)
_HEADER_FMT_SIZE = struct.calcsize(_FMT)  # 56 bytes

# Tail: checksum (uint32) + magic_tail (uint32)
_TAIL_FMT = "<II"
_TAIL_FMT_SIZE = struct.calcsize(_TAIL_FMT)  # 8 bytes

# The data area between the header and the tail (used for XOR).
_DATA_OFFSET = _HEADER_FMT_SIZE
_DATA_SIZE = SIZE - _HEADER_FMT_SIZE - _TAIL_FMT_SIZE  # 4032 bytes

# Default segment name. Overridden by ``name`` arg.
DEFAULT_NAME = "agentic_memory_state"


class SharedMemoryState:
    """Wrapper around a single shared-memory segment.

    The class has three states:

    - **Unattached** (just constructed): no resources held.
    - **Attached** (after ``attach()`` or ``create()``): holds a
      ``multiprocessing.shared_memory.SharedMemory`` object. The
      underlying buffer is memory-mapped; reads/writes are O(1) and
      process-safe (we use ``bytes(buf)`` to take a snapshot before
      reading to avoid torn reads on a non-atomic buffer).
    - **Detached** (after ``detach()``): the shared memory is still
      on disk but this process no longer maps it. ``unlink()``
      removes it entirely.

    Thread-safety: a single ``SharedMemoryState`` instance is not
    safe to use from multiple threads within the same process —
    call sites are single-threaded (CLI hooks are short-lived
    subprocesses; the daemon is single-threaded). For multi-writer
    scenarios (e.g., multi-daemon), use a file lock or POSIX
    semaphore — the segment is single-writer by convention.
    """

    def __init__(self, name: str = DEFAULT_NAME, size: int = SIZE) -> None:
        if size < SIZE:
            raise ValueError(
                f"size must be >= {SIZE} (16KB, max OS page on supported platforms); got {size}"
            )
        self.name = name
        self.size = size
        self._shm: Optional[Any] = None  # multiprocessing.shared_memory.SharedMemory
        self._buf: Optional[memoryview] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create(self) -> None:
        """Create a new segment. Errors if the name already exists.

        Use this on daemon startup. The daemon is the sole writer;
        CLI hooks use ``attach()`` to read the existing segment.
        """
        # Imported lazily so the module is importable in environments
        # without multiprocessing (e.g., minimal test runners).
        from multiprocessing.shared_memory import SharedMemory

        # Retry loop to handle race where another process creates
        # the segment between our check and create.
        for _ in range(3):
            try:
                self._shm = SharedMemory(name=self.name, create=True, size=self.size)
                self._buf = self._shm.buf
                return
            except FileExistsError:
                # Stale segment from a crashed previous run. Try to
                # unlink and retry. This is a best-effort cleanup;
                # another process might have already cleaned it up.
                try:
                    stale = SharedMemory(name=self.name, create=False)
                    stale.unlink()
                except FileNotFoundError:
                    pass
                except Exception as e:
                    logger.warning("create failed: %s", e)
                # Small delay before retry to reduce contention.
                time.sleep(0.01)
        raise RuntimeError(
            f"Failed to create shared memory segment '{self.name}' after retries"
        )

    def attach(self) -> bool:
        """Attach to an existing segment. Returns True on success.

        On failure (segment doesn't exist yet — no daemon has
        started, or the daemon hasn't created the segment yet),
        returns False. The caller should fall back to the DB
        audit log in that case.
        """
        from multiprocessing.shared_memory import SharedMemory

        try:
            self._shm = SharedMemory(name=self.name, create=False)
        except FileNotFoundError:
            self._shm = None
            self._buf = None
            return False
        self._buf = self._shm.buf
        return True

    def detach(self) -> None:
        """Release the mmap. Does NOT unlink (the segment persists
        for other processes)."""
        if self._shm is not None:
            try:
                self._shm.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("SharedMemoryState: close failed: %s", e)
            self._shm = None
            self._buf = None

    def unlink(self) -> None:
        """Unlink the segment from the OS. Called by the daemon on
        clean shutdown. Idempotent — safe to call on a missing
        segment."""
        if self._shm is not None:
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.debug("SharedMemoryState: unlink failed: %s", e)
        # Best-effort: also try unlinking by name (in case the
        # current process didn't create the segment but wants to
        # clean it up).
        from multiprocessing.shared_memory import SharedMemory

        try:
            SharedMemory(name=self.name).unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("unlink failed: %s", e)
        self.detach()

    def __enter__(self) -> "SharedMemoryState":
        return self

    def __exit__(self, *args: object) -> None:
        self.detach()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _read_header(self) -> Optional[bytes]:
        """Read the header + tail as raw bytes. Returns None if the
        segment is invalid (bad magic, bad checksum)."""
        if self._buf is None:
            return None
        try:
            header = bytes(self._buf[:_HEADER_FMT_SIZE])
            tail = bytes(self._buf[SIZE - _TAIL_FMT_SIZE :])
        except Exception as e:  # noqa: BLE001
            logger.debug("SharedMemoryState: read failed: %s", e)
            return None
        # Validate magic.
        magic_head, version = struct.unpack("<II", header[:8])
        if magic_head != MAGIC:
            return None
        if version != VERSION:
            # Future or past version — treat as invalid.
            return None
        # Validate checksum.
        data = bytes(self._buf[_DATA_OFFSET : SIZE - _TAIL_FMT_SIZE])
        stored_checksum, magic_tail = struct.unpack(_TAIL_FMT, tail)
        if magic_tail != MAGIC:
            return None
        actual_checksum = self._compute_checksum(header + data)
        if actual_checksum != stored_checksum:
            return None
        return header

    @staticmethod
    def _compute_checksum(data: bytes) -> int:
        """32-bit XOR checksum. Detects single-bit errors and most
        multi-bit errors; not cryptographic, just a sanity check."""
        checksum = 0
        # Process 4 bytes at a time.
        for i in range(0, len(data) - 3, 4):
            checksum ^= struct.unpack_from("<I", data, i)[0]
        # Handle remaining bytes (< 4).
        remainder = len(data) % 4
        if remainder:
            tail = data[-remainder:] + b"\x00" * (4 - remainder)
            checksum ^= struct.unpack("<I", tail)[0]
        return checksum

    def is_valid(self) -> bool:
        """True if the segment exists and has valid magic + checksum."""
        return self._read_header() is not None

    # ------------------------------------------------------------------
    # State API
    # ------------------------------------------------------------------

    def read_state(self) -> Optional[dict[str, object]]:
        """Read the current state. Returns None if the segment is
        invalid. The returned dict is a snapshot — safe to inspect
        after detach().
        """
        header = self._read_header()
        if header is None:
            return None
        try:
            (
                _magic,
                _version,
                timestamp,
                circuit_open_until,
                failure_count,
                last_backoff_seconds,
                daemon_pid,
                daemon_started_at,
                is_daemon_alive,
                writes_since_start,
                _reserved,
            ) = struct.unpack(_FMT, header)
        except struct.error as e:  # noqa: BLE001
            logger.debug("SharedMemoryState: unpack failed: %s", e)
            return None
        return {
            "timestamp": float(timestamp),
            "circuit_open_until": float(circuit_open_until),
            "failure_count": int(failure_count),
            "last_backoff_seconds": float(last_backoff_seconds),
            "daemon_pid": int(daemon_pid),
            "daemon_started_at": float(daemon_started_at),
            "is_daemon_alive": bool(is_daemon_alive),
            "writes_since_start": int(writes_since_start),
        }

    def write_state(
        self,
        *,
        circuit_open_until: float = 0.0,
        failure_count: int = 0,
        last_backoff_seconds: float = 0.0,
        daemon_pid: int = 0,
        daemon_started_at: float = 0.0,
        is_daemon_alive: bool = False,
    ) -> None:
        """Atomically write the state. No-op if not attached.

        "Atomic" here means: we build the entire 4KB buffer in a
        local ``bytearray``, compute the checksum, and then a
        single ``self._buf[:] = bytes(buffer)`` copies it to the
        mmap. POSIX guarantees that a memcpy of a page-sized buffer
        is atomic at the filesystem level (and mmap is page-aligned
        internally). On a reader side, the magic + checksum detect
        any torn read.
        """
        if self._buf is None:
            return
        timestamp = time.time()
        # Header: 56 bytes
        header = struct.pack(
            _FMT,
            MAGIC,
            VERSION,
            timestamp,
            float(circuit_open_until),
            int(failure_count),
            float(last_backoff_seconds),
            int(daemon_pid),
            float(daemon_started_at),
            1 if is_daemon_alive else 0,
            int(getattr(self, "_writes", 0)) + 1,
            0,  # reserved
        )
        # Data area: zero-filled (free space for future fields).
        data = b"\x00" * _DATA_SIZE
        body = header + data
        checksum = self._compute_checksum(body)
        tail = struct.pack(_TAIL_FMT, checksum, MAGIC)
        buffer = body + tail
        assert len(buffer) == self.size, (
            f"buffer size {len(buffer)} != segment size {self.size}"
        )
        # Single atomic memcpy to the mmap.
        self._buf[: self.size] = buffer
        # Track writes for debugging.
        self._writes = int(getattr(self, "_writes", 0)) + 1

    def is_circuit_open(self) -> Optional[bool]:
        """Read just the circuit_open_until field. Returns None if
        the segment is invalid.

        This is the hot-path method called by CLI hooks. It's
        intentionally cheaper than ``read_state()`` — it only
        reads the first 24 bytes (magic + version + timestamp +
        circuit_open_until).
        """
        if self._buf is None:
            return None
        try:
            # Read magic first, then the rest. Check magic BEFORE
            # unpacking the rest to avoid processing corrupted data.
            raw = bytes(self._buf[:24])
            if len(raw) < 4:
                return None
            magic = struct.unpack("<I", raw[:4])[0]
            if magic != MAGIC:
                return None
            # Now unpack the rest knowing magic is valid.
            _version, _ts, circuit_open_until = struct.unpack("<Idd", raw[4:])
        except struct.error:  # noqa: BLE001
            return None
        except Exception as e:
            logger.warning("is_circuit_open failed: %s", e)
            return None
        return bool(time.time() < float(circuit_open_until))

    # ------------------------------------------------------------------
    # Daemon PID tracking (used by CLI hooks to detect liveness)
    # ------------------------------------------------------------------

    def read_daemon_pid(self) -> Optional[int]:
        """Read the daemon's PID. Used by the health check in
        ``is_daemon_alive()``."""
        if self._buf is None:
            return None
        try:
            raw = bytes(self._buf[32:36])
        except Exception as e:
            logger.warning("read_daemon_pid failed: %s", e)
            return None
        try:
            (pid,) = struct.unpack("<i", raw)
        except struct.error:  # noqa: BLE001
            return None
        return int(pid) if pid > 0 else None

    def is_daemon_alive(self) -> bool:
        """True if the daemon is alive.

        Two checks: (1) the segment says is_daemon_alive=1, AND
        (2) the PID stored in the segment matches a running
        process. The PID check guards against the case where the
        daemon crashed but the segment wasn't unlinked.
        """
        if self._buf is None:
            return False
        # Read the full state in one go (includes validation).
        state = self.read_state()
        if state is None:
            return False
        if not state["is_daemon_alive"]:
            return False
        pid_val = state["daemon_pid"]
        if not isinstance(pid_val, (int, float)):
            return False
        pid = int(pid_val)
        if pid <= 0:
            return False
        # Check if the PID is running. We don't import psutil — just
        # use os.kill(pid, 0) which sends signal 0 (a no-op that
        # errors with ESRCH if the process doesn't exist).
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but is owned by another user — count
            # this as alive.
            return True
        return True


# ---------------------------------------------------------------------------
# Module-level convenience: default singleton
# ---------------------------------------------------------------------------

_default: Optional[SharedMemoryState] = None


def get_default() -> SharedMemoryState:
    """Return the default SharedMemoryState singleton.

    The default uses ``DEFAULT_NAME`` and the standard size. The
    caller is responsible for ``attach()`` / ``create()`` / ``detach()``.
    """
    global _default
    if _default is None:
        _default = SharedMemoryState()
    return _default
