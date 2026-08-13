"""Pluggable Lock Manager for Agentic Memory.

Supports local-first (fcntl.flock file locking), and distributed (Redis, PostgreSQL) locks.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Generator, Tuple

logger = logging.getLogger(__name__)


class LockManager:
    """Abstract base class for pluggable lock management."""

    def acquire_lock(self, lock_name: str, holder_id: str, ttl_seconds: int = 60) -> Tuple[bool, str]:
        """Attempt to acquire a lock.

        Returns (success, lease_token).
        """
        raise NotImplementedError

    def release_lock(self, lock_name: str, lease_token: str) -> bool:
        """Release a lock using its lease token.

        Returns True if released successfully, else False.
        """
        raise NotImplementedError

    def renew_lock(self, lock_name: str, lease_token: str, ttl_seconds: int = 60) -> bool:
        """Extend lock lease TTL.

        Returns True if renewed successfully, else False.
        """
        raise NotImplementedError

    def is_locked(self, lock_name: str) -> bool:
        """Check if lock is currently held and active."""
        raise NotImplementedError

    @contextlib.contextmanager
    def acquire_context(
        self,
        lock_name: str,
        holder_id: str,
        ttl_seconds: int = 60,
        acquire_timeout: float = 10.0,
        poll_interval: float = 0.05,
    ) -> Generator[str, None, None]:
        """Context manager to block until lock is acquired, and release it on block exit."""
        start = time.time()
        lease_token = ""
        while True:
            success, token = self.acquire_lock(lock_name, holder_id, ttl_seconds)
            if success:
                lease_token = token
                break
            if time.time() - start > acquire_timeout:
                raise TimeoutError(
                    f"Lock acquisition timed out for '{lock_name}' after {acquire_timeout} seconds"
                )
            time.sleep(poll_interval)
        try:
            yield lease_token
        finally:
            self.release_lock(lock_name, lease_token)


class SQLiteLockManager(LockManager):
    """Deprecated SQLite-backed lock manager.

    .. deprecated::
        Use :class:`FlockLockManager` instead. SQLiteLockManager opens a
        new connection to the DB it is trying to coordinate, which creates
        a circular dependency under contention and can deadlock.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS system_locks ("
                "lock_key TEXT PRIMARY KEY, holder_id TEXT, "
                "acquired_at TEXT, expires_at TEXT, lease_token TEXT)"
            )
        except Exception:
            pass
        return conn

    def acquire_lock(self, lock_name: str, holder_id: str, ttl_seconds: int = 60) -> Tuple[bool, str]:
        try:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                row = conn.execute(
                    "SELECT holder_id, expires_at, lease_token FROM system_locks WHERE lock_key = ?",
                    (lock_name,),
                ).fetchone()

                if row:
                    _, expires_at, token = row
                    if now <= expires_at:
                        conn.rollback()
                        conn.close()
                        return False, ""  # Lock is active and held by someone else

                token = str(uuid.uuid4())
                expires = (
                    datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=ttl_seconds)
                ).isoformat()

                conn.execute(
                    "INSERT OR REPLACE INTO system_locks (lock_key, holder_id, acquired_at, expires_at, lease_token) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (lock_name, holder_id, now, expires, token),
                )
                conn.commit()
                conn.close()
                return True, token
            except Exception:
                try:
                    conn.rollback()
                    conn.close()
                except Exception:
                    pass
                raise
        except sqlite3.OperationalError as exc:
            logger.warning("SQLiteLockManager.acquire_lock failed: %s", exc)
            return False, ""

    def release_lock(self, lock_name: str, lease_token: str) -> bool:
        if not lease_token:
            return False
        try:
            with self._get_conn() as conn:
                cur = conn.execute(
                    "DELETE FROM system_locks WHERE lock_key = ? AND lease_token = ?",
                    (lock_name, lease_token),
                )
                return cur.rowcount > 0
        except sqlite3.OperationalError as exc:
            logger.warning("SQLiteLockManager.release_lock failed: %s", exc)
            return False

    def renew_lock(self, lock_name: str, lease_token: str, ttl_seconds: int = 60) -> bool:
        if not lease_token:
            return False
        try:
            with self._get_conn() as conn:
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                row = conn.execute(
                    "SELECT expires_at FROM system_locks WHERE lock_key = ? AND lease_token = ?",
                    (lock_name, lease_token),
                ).fetchone()

                if not row:
                    return False

                expires_at = row[0]
                if now > expires_at:
                    return False  # Lock has already expired, cannot renew safely

                new_expires = (
                    datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=ttl_seconds)
                ).isoformat()

                conn.execute(
                    "UPDATE system_locks SET expires_at = ? WHERE lock_key = ? AND lease_token = ?",
                    (new_expires, lock_name, lease_token),
                )
                return True
        except sqlite3.OperationalError as exc:
            logger.warning("SQLiteLockManager.renew_lock failed: %s", exc)
            return False

    def is_locked(self, lock_name: str) -> bool:
        try:
            with self._get_conn() as conn:
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                row = conn.execute(
                    "SELECT expires_at FROM system_locks WHERE lock_key = ?",
                    (lock_name,),
                ).fetchone()
                if row:
                    return bool(now <= row[0])
                return False
        except sqlite3.OperationalError:
            return False


class FlockLockManager(LockManager):
    """Local file-descriptor lock manager using fcntl.flock.

    Replaces SQLiteLockManager to eliminate the circular dependency where
    the lock manager opened SQLite connections to the very DB it was
    coordinating.  flock(2) is an OS-level advisory lock held on an open
    file descriptor; it is automatically released when the fd is closed
    (including on process crash), so there is no risk of stale locks.
    """

    def __init__(self, base_path: str | Path):
        self.base_path = str(base_path)
        self._lock_fds: dict[str, int] = {}
        self._lock_tokens: dict[str, set[str]] = {}
        self._mu = threading.Lock()  # protects _lock_fds / _lock_tokens

    def _lock_file(self, lock_name: str) -> str:
        if "/" in lock_name or os.path.isabs(lock_name):
            return lock_name + ".flock"
        h = hashlib.sha256(lock_name.encode()).hexdigest()[:16]
        return self.base_path + f".flock.{h}"

    def acquire_lock(self, lock_name: str, holder_id: str, ttl_seconds: int = 60) -> Tuple[bool, str]:
        # Cache ONE fd per lock_name per process. flock(2) is per
        # open-file-description: locking the SAME fd again is re-entrant and
        # succeeds immediately, while locking a FRESH fd for the same file
        # self-conflicts (EWOULDBLOCK) — that fresh-fd-per-acquire pattern
        # deadlocked same-process threads (main loop vs. SQLiteWriteQueue
        # thread) while the contradiction-resolver cron held the flock.
        with self._mu:
            fd = self._lock_fds.get(lock_name)
            if fd is not None:
                token = str(uuid.uuid4())
                self._lock_tokens.setdefault(lock_name, set()).add(token)
                return True, token
            path = self._lock_file(lock_name)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                os.close(fd)
                return False, ""
            self._lock_fds[lock_name] = fd
            token = str(uuid.uuid4())
            self._lock_tokens[lock_name] = {token}
            return True, token

    def release_lock(self, lock_name: str, lease_token: str) -> bool:
        with self._mu:
            fd = self._lock_fds.get(lock_name)
            tokens = self._lock_tokens.get(lock_name)
            if fd is None or tokens is None or lease_token not in tokens:
                return False
            tokens.discard(lease_token)
            if tokens:
                return True
            self._lock_tokens.pop(lock_name, None)
            self._lock_fds.pop(lock_name, None)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
        return True

    def renew_lock(self, lock_name: str, lease_token: str, ttl_seconds: int = 60) -> bool:
        return lock_name in self._lock_fds and lease_token in self._lock_tokens.get(lock_name, ())

    def is_locked(self, lock_name: str) -> bool:
        path = self._lock_file(lock_name)
        if not os.path.exists(path):
            return False
        fd = os.open(path, os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except (IOError, OSError):
            return True
        finally:
            os.close(fd)


class RedisLockManager(LockManager):
    """Distributed Redis-backed lock manager (using Redlock patterns)."""

    def __init__(self, redis_url: str):
        import redis
        self.client = redis.Redis.from_url(redis_url)

    def acquire_lock(self, lock_name: str, holder_id: str, ttl_seconds: int = 60) -> Tuple[bool, str]:
        token = str(uuid.uuid4())
        lock_key = f"lock:{lock_name}"
        success = self.client.set(lock_key, token, ex=ttl_seconds, nx=True)
        if success:
            return True, token
        return False, ""

    def release_lock(self, lock_name: str, lease_token: str) -> bool:
        if not lease_token:
            return False
        lock_key = f"lock:{lock_name}"
        lua = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        result = self.client.eval(lua, 1, lock_key, lease_token)
        return bool(result)

    def renew_lock(self, lock_name: str, lease_token: str, ttl_seconds: int = 60) -> bool:
        if not lease_token:
            return False
        lock_key = f"lock:{lock_name}"
        lua = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        result = self.client.eval(lua, 1, lock_key, lease_token, ttl_seconds)
        return bool(result)

    def is_locked(self, lock_name: str) -> bool:
        lock_key = f"lock:{lock_name}"
        return self.client.exists(lock_key) > 0


class PostgresLockManager(LockManager):
    """Distributed Postgres-backed lock manager utilizing session advisory locks."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._connections: dict[str, Any] = {}  # lease_token -> active conn

    def _lock_key_to_int(self, lock_key: str) -> int:
        h = hashlib.sha256(lock_key.encode("utf-8")).digest()
        return int.from_bytes(h[:8], byteorder="big", signed=True)

    def acquire_lock(self, lock_name: str, holder_id: str, ttl_seconds: int = 60) -> Tuple[bool, str]:
        try:
            import psycopg2
            conn = psycopg2.connect(self.dsn)
            conn.set_session(autocommit=True)
            lock_id = self._lock_key_to_int(lock_name)
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
                row = cur.fetchone()
                acquired = row[0] if row is not None else False
                if acquired:
                    token = str(uuid.uuid4())
                    self._connections[token] = conn
                    return True, token
                else:
                    conn.close()
                    return False, ""
        except Exception as exc:
            logger.warning("PostgresLockManager.acquire_lock failed: %s", exc)
            return False, ""

    def release_lock(self, lock_name: str, lease_token: str) -> bool:
        conn = self._connections.pop(lease_token, None)
        if not conn:
            return False
        try:
            lock_id = self._lock_key_to_int(lock_name)
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
                released = cur.fetchone()[0]
                return bool(released)
        except Exception as exc:
            logger.warning("PostgresLockManager.release_lock failed: %s", exc)
            return False
        finally:
            conn.close()

    def renew_lock(self, lock_name: str, lease_token: str, ttl_seconds: int = 60) -> bool:
        # Session locks are held until connection closes; TTL renewal is a no-op
        return lease_token in self._connections

    def is_locked(self, lock_name: str) -> bool:
        # Postgres lock inspection requires querying pg_locks
        try:
            import psycopg2
            conn = psycopg2.connect(self.dsn)
            lock_id = self._lock_key_to_int(lock_name)
            # Advisory lock classid/objid maps
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND classid = %s",
                    (lock_id,),
                )
                locked = cur.fetchone() is not None
                conn.close()
                return locked
        except Exception:
            return False


_GLOBAL_LOCK_MANAGER: LockManager | None = None


def get_lock_manager() -> LockManager:
    """Get or initialize the global pluggable lock manager based on configuration."""
    global _GLOBAL_LOCK_MANAGER
    if _GLOBAL_LOCK_MANAGER is not None:
        return _GLOBAL_LOCK_MANAGER

    engine = os.environ.get("MEMORY_LOCK_ENGINE", "local").strip().lower()
    if engine == "redis":
        url = os.environ.get("MEMORY_REDIS_URL", "redis://localhost:6379/0")
        _GLOBAL_LOCK_MANAGER = RedisLockManager(url)
    elif engine == "postgres":
        dsn = os.environ.get("MEMORY_POSTGRES_URL", "host=localhost dbname=postgres")
        _GLOBAL_LOCK_MANAGER = PostgresLockManager(dsn)
    else:
        # Default: local file-descriptor lock (fcntl.flock).
        # SQLiteLockManager is deprecated because it opens SQLite connections
        # to the very DB it coordinates, creating a circular dependency.
        from infra.infrastructure import resolve_active_memory_dir
        db_path = resolve_active_memory_dir() / "memory.db"
        _GLOBAL_LOCK_MANAGER = FlockLockManager(db_path)

    return _GLOBAL_LOCK_MANAGER


def clear_lock_manager_cache() -> None:
    """Clear the cached lock manager configuration (useful for testing)."""
    global _GLOBAL_LOCK_MANAGER
    _GLOBAL_LOCK_MANAGER = None
