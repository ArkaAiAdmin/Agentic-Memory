"""SQLite connection pool, open_db context manager, WAL checkpoint, count_rows.

Extracted from memory_common.py during the 6-module refactor.

Provides:
  * ``class _ConnectionPool`` and module-level ``connection_pool`` singleton.
  * ``open_db(path, timeout, row_factory, pooled)`` — context manager.
  * ``safe_close_db(conn)`` — commit + return-to-pool / close.
  * ``wal_checkpoint_idle(db_path, threshold)`` — passive WAL checkpoint.
  * ``_maybe_checkpoint_on_startup(path)`` — one-shot startup checkpoint.
  * ``count_rows(db_dir) -> int``.

Migration helpers (``_migrate_ensure_columns``, ``_migrate_ensure_indexes``,
``_migrate_memory_embeddings``, ``_migrate_memory_audit_log``,
``_migrate_memory_vec_idx``, ``_migrate_kg_tables``,
``_migrate_ensure_chunks_table``, ``_migrate_memory_ctr_feedback``,
``_migrate_concept_drift``) and the SQL-migration runner
(``run_db_migrations``) live in ``db_migrations.py``, which both
``db.py`` and ``memory_common.py`` import — no circular dependency.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Union, cast

from db_write_queue import ProxyConnection
from fts import _migrate_fts5_porter_tokenizer, _migrate_ensure_fts_triggers

AnyConnection = Union[sqlite3.Connection, ProxyConnection]

__all__ = [
    "connection_pool",
    "safe_close_db",
    "open_db",
    "wal_checkpoint_idle",
    "count_rows",
]


class _ConnectionPool:
    """Thread-safe per-path connection pool for SQLite.

    Reuses connections within a single thread. Connections are validated
    with a lightweight PRAGMA check before reuse. Dead connections are
    silently replaced. Enforces a max pool size with LRU eviction.
    """

    def __init__(self, max_size: int = 10) -> None:
        self._lock = threading.Lock()
        self._pool: dict[tuple[str, int], sqlite3.Connection] = {}
        self._pooled_ids: set[int] = set()
        self._max_size = max_size
        self._lru: deque[tuple[str, int]] = deque()
        self._migrated: set[int] = set()  # conn ids that have full schema
        self._depth: dict[tuple[str, int], int] = {}
        self._migration_locks: dict[str, threading.Lock] = {}
        self._migration_locks_lock = threading.Lock()
        # 2026-06-26 fix: track the inode each connection was opened
        # against. If memory.db is replaced (e.g. rebuild_index.py
        # os.replace()s the tmp db into place), connections in long-
        # lived daemons would otherwise keep pointing at the old
        # inode forever. get() compares the stored inode against the
        # current st_ino and evicts + reopens on mismatch.
        self._inodes: dict[tuple[str, int], int] = {}
        # Intra-process serialization is handled by per-thread connection
        # keys in the pool; inter-process serialization uses flock files.

    @staticmethod
    def _inode_of(path: str) -> int:
        """Return the current inode of *path*, or 0 if it can't be determined.

        Returning 0 (a value no real inode will match) means "inode
        tracking is unavailable" — callers should treat that as a
        no-op rather than a forced reopen.
        """
        try:
            return os.stat(path).st_ino
        except OSError:
            return 0

    def _inode_mismatch(self, key: tuple[str, int], conn: sqlite3.Connection) -> bool:
        """True if *conn*'s stored inode no longer matches the current file.

        A mismatch means the on-disk file was replaced (e.g. via
        os.replace by rebuild_index.py). The old conn is still valid
        for the old inode but is stale relative to the live data.
        """
        stored = self._inodes.get(key)
        if stored is None:
            return False
        current = self._inode_of(key[0])
        if current == 0:
            return False  # can't stat — don't churn
        return stored != current

    def _evict_lru(self) -> None:
        """Close the least recently used connection to make room.

        P0-3 fix (2026-06-22): skip connections with ``self._depth[key] > 0``.
        Before the fix, a long-running operation that holds a conn (e.g.
        a multi-step saga) could have its conn closed mid-transaction
        when a new ``get()`` triggered eviction.  The check is a guard
        against losing the active conn out from under the caller.

        If every pooled conn is active (depth > 0), the pool is
        genuinely exhausted and the caller is asking for a new conn
        while holding N others open.  We raise ``PoolExhaustedError`` so
        the caller can fail fast instead of silently breaking the
        active transaction by closing its conn.

        Implementation note: we iterate over a snapshot of the LRU so
        active keys stay in the deque (preserving their relative order)
        without causing an infinite loop on the same key.  An earlier
        attempt that popleft'd + appendleft'd active keys into a
        back-into-the-front loop, and the next attempt (a ``tried`` set)
        dropped active keys from the LRU on the first pass, which then
        prevented the second ``get()`` from ever finding the now-idle
        conn.  Snapshotting is the only correct design.
        """
        snapshot = list(self._lru)
        scanned = 0
        evicted = 0
        for key in snapshot:
            if len(self._pool) < self._max_size:
                break  # pool no longer at max — done
            if key not in self._pool:
                continue
            # Skip active connections — closing them mid-transaction
            # would corrupt the caller's view of the DB.  Leave the key
            # in ``self._lru`` so a future get() (after the caller
            # releases the conn) can still see it.
            if self._depth.get(key, 0) > 0:
                continue
            # Evict: remove from LRU and pool, then close the conn.
            try:
                self._lru.remove(key)
            except ValueError:
                pass
            conn = self._pool.pop(key)
            conn_id = id(conn)
            self._pooled_ids.discard(conn_id)
            self._migrated.discard(conn_id)
            try:
                conn.close()
            except Exception:
                logger.warning("db: connection close_failed during LRU eviction")
                pass
            evicted += 1
            scanned += 1
            if scanned >= self._max_size:
                break
        # If the pool is still at max and we didn't evict anything,
        # every pooled conn is active.  The caller is asking for a new
        # conn while holding N others — fail fast.
        if evicted == 0 and len(self._pool) >= self._max_size:
            raise PoolExhaustedError(
                f"connection pool exhausted: {self._max_size} connections "
                f"all active, cannot allocate another"
            )

    def clear(self) -> None:
        """Close all pooled connections. Used by test fixtures."""
        with self._lock:
            for conn in self._pool.values():
                try:
                    conn.close()
                except Exception:
                    logger.warning("db: connection close_failed during clear()")
                    pass
            self._pool.clear()
            self._pooled_ids.clear()
            self._migrated.clear()
            self._lru.clear()
            self._depth.clear()

    def _ensure_full_schema(self, conn: sqlite3.Connection) -> None:
        """Run all Python schema migrations on a new connection.

        This is the single source of truth for schema setup on pooled
        connections. Previously only open_db() ran these, causing drift
        when save_pipeline.py used connection_pool.get() directly.

        Migration helpers live in ``db_migrations.py``.

        Thread safety: Always invoked *after* ``self._lock`` has been
        released by ``get()`` (see line ~191). To prevent deadlock,
        callers must NEVER call this method while holding self._lock.
        """
        conn_id = id(conn)
        if conn_id in self._migrated:
            return

        try:
            db_path = conn.execute("PRAGMA database_list").fetchone()[2]
        except Exception:
            logger.warning("db: db_path resolve_failed from PRAGMA database_list")
            db_path = ""

        # Resolve or create the per-path migration lock. We hold
        # _migration_locks_lock just long enough to look up / allocate.
        if db_path:
            with self._migration_locks_lock:
                if db_path not in self._migration_locks:
                    self._migration_locks[db_path] = threading.Lock()
                path_lock = self._migration_locks[db_path]
        else:
            path_lock = None

        # Use context manager on the lock to guarantee release even if
        # run_schema_setup raises. Previously the code used a bare
        # .acquire()/.release() pair that would leak the lock on any
        # exception raised between them.
        if path_lock:
            with path_lock:
                # Re-check after acquiring: another thread may have
                # already completed migrations for this path.
                # run_schema_setup maintains its own _MIGRATIONS_DONE
                # cache keyed by id(conn); we don't need a duplicate
                # mirror here. The pool's _migrated set is sufficient.
                if conn_id in self._migrated:
                    return
                try:
                    from db_migrations import run_schema_setup

                    run_schema_setup(conn)

                    # Perform one-shot startup checkpoint if needed
                    if db_path:
                        _maybe_checkpoint_on_startup(Path(db_path).parent)
                    self._migrated.add(conn_id)
                except Exception as exc:
                    logger.warning(
                        "_ensure_full_schema failed: %s", exc
                    )  # best-effort; callers can retry via open_db()
        else:
            # No path lock (e.g. :memory: DB) — proceed without serialisation.
            if conn_id in self._migrated:
                return
            try:
                from db_migrations import run_schema_setup

                run_schema_setup(conn)
                self._migrated.add(conn_id)
            except Exception as exc:
                logger.warning("db: schema ensure_failed: %s", exc)

    def get(self, path: str, timeout: float = 30.0) -> sqlite3.Connection:
        """Return a live connection for *path*, creating one if needed.

        Connections are keyed by ``(path, thread_ident)`` so different
        threads always get separate connections. Within the same thread,
        repeated calls return the same connection — callers must
        ``put()`` it back before calling ``get()`` again for the same
        path.
        """
        current_thread = threading.current_thread().ident or 0
        key = (path, current_thread)
        with self._lock:
            conn = self._pool.get(key)
            if conn is not None:
                self._depth[key] = self._depth.get(key, 0) + 1
                conn_id = id(conn)
                # B17 fix: remove any prior occurrence of this key to keep
                # the deque bounded; previous behaviour appended without
                # removing, slowly leaking memory in long-lived daemons.
                try:
                    self._lru.remove(key)
                except ValueError:
                    pass
                self._lru.append(key)
                try:
                    conn.execute("SELECT 1")
                except Exception:
                    logger.warning(
                        "db: connection stale_detected during pool get, closing"
                    )
                    try:
                        conn.close()
                    except Exception:
                        logger.warning(
                            "db: connection stale_close_failed during pool get"
                        )
                        pass
                    self._pooled_ids.discard(conn_id)
                    self._pool.pop(key, None)
                    self._depth.pop(key, None)
                    self._inodes.pop(key, None)
                else:
                    # 2026-06-26: also evict if the on-disk inode changed
                    # (e.g. rebuild_index.py os.replace()'d the db file).
                    # The old conn is still valid for the old inode, but
                    # it's stale relative to the live data — writes
                    # through it would never reach the new file.
                    if self._inode_mismatch(key, conn):
                        logger.warning(
                            "memory.db inode changed under pooled connection "
                            "(%s) — reopening against new file",
                            path,
                        )
                        try:
                            conn.close()
                        except Exception:
                            pass
                        self._pooled_ids.discard(conn_id)
                        self._pool.pop(key, None)
                        self._depth.pop(key, None)
                        self._inodes.pop(key, None)
                    else:
                        return conn
            # Close any orphaned connection from another thread holding the same path
            for other_key in list(self._pool):
                if other_key[0] == path and self._depth.get(other_key, 0) == 0:
                    orphan = self._pool.pop(other_key)
                    orphan_id = id(orphan)
                    self._pooled_ids.discard(orphan_id)
                    self._depth.pop(other_key, None)
                    self._inodes.pop(other_key, None)
                    try:
                        orphan.close()
                    except Exception:
                        logger.warning(
                            "Failed to close orphaned connection during pool get"
                        )
                        pass
            self._evict_lru()
            conn = sqlite3.connect(path, timeout=timeout)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            # S4.1 (2026-06-23): apply mmap_size from config. The
            # value 0 disables mmap (SQLite default).  The value
            # is bytes. If requested mmap exceeds available
            # virtual memory, the kernel may map partially and
            # SQLite will silently fall back; that's acceptable.
            # We log the configured value for the operator to see
            # in cron logs.
            mmap_bytes = _resolve_mmap_size()
            if mmap_bytes > 0:
                try:
                    conn.execute(f"PRAGMA mmap_size={int(mmap_bytes)}")
                except sqlite3.OperationalError as e:
                    logger.debug(
                        "mmap_size=%d not applied for %s: %s",
                        mmap_bytes,
                        path,
                        e,
                    )
            self._pool[key] = conn
            self._depth[key] = 1
            self._inodes[key] = self._inode_of(path)
            conn_id = id(conn)
            self._pooled_ids.add(conn_id)
            # B17 fix: remove any prior occurrence of this key to keep
            # the deque bounded.
            try:
                self._lru.remove(key)
            except ValueError:
                pass
            self._lru.append(key)
        # Run full schema outside the lock (migrations are idempotent)
        self._ensure_full_schema(conn)
        return conn

    def put(self, conn: AnyConnection) -> None:
        """Return a pooled connection for reuse. Validates before accepting."""
        conn_id = id(conn)
        with self._lock:
            key = None
            for k, v in self._pool.items():
                if v is conn:
                    key = k
                    break
            if key is not None:
                d = self._depth.get(key, 0)
                if d > 1:
                    self._depth[key] = d - 1
                    return
                else:
                    self._depth[key] = 0
            if conn_id not in self._pooled_ids:
                try:
                    conn.close()
                except Exception:
                    logger.warning("db: connection non_pooled_close_failed during put")
                    pass
                self._migrated.discard(conn_id)
                return
            try:
                conn.execute("SELECT 1")
            except Exception:
                logger.warning("db: connection stale_detected during pool put, closing")
                try:
                    conn.close()
                except Exception:
                    logger.warning("db: connection stale_close_failed during pool put")
                    pass
                self._pooled_ids.discard(conn_id)
                self._migrated.discard(conn_id)
                for k, v in list(self._pool.items()):
                    if v is conn:
                        self._pool.pop(k, None)
                        self._depth.pop(k, None)
                        self._inodes.pop(k, None)
                        break

    def close(self, path: str) -> None:
        """Close and remove the pooled connection for *path*."""
        with self._lock:
            for key in list(self._pool):
                if key[0] == path:
                    conn = self._pool.pop(key)
                    conn_id = id(conn)
                    self._pooled_ids.discard(conn_id)
                    self._migrated.discard(conn_id)
                    self._depth.pop(key, None)
                    self._inodes.pop(key, None)
                    try:
                        conn.close()
                    except Exception:
                        logger.warning(
                            "db: connection close_failed during clear_by_path"
                        )
                        pass
                    try:
                        self._lru.remove(key)
                    except ValueError:
                        pass

    def close_all(self) -> None:
        """Close every pooled connection."""
        with self._lock:
            for conn in self._pool.values():
                try:
                    conn.close()
                except Exception:
                    logger.warning("db: connection close_failed during close_all()")
                    pass
            self._pool.clear()
            self._pooled_ids.clear()
            self._lru.clear()
            self._migrated.clear()
            self._depth.clear()
            self._inodes.clear()

    def get_depth(self, conn: AnyConnection) -> int:
        """Return current checkout depth for a connection."""
        with self._lock:
            for k, v in self._pool.items():
                if v is conn:
                    return self._depth.get(k, 0)
            return 0


connection_pool = _ConnectionPool()


def _resolve_mmap_size() -> int:
    """Return the configured mmap_size in bytes (0 disables mmap).

    S4.1 (2026-06-23): pulled from MemoryConfig.mmap_size.
    S4.9 safety: if the configured value is larger than the
    system's available virtual memory, the OS may either succeed
    (overcommit) or refuse the mmap call. We don't try to detect
    this in advance (the host's available memory is hard to
    measure portably) — we just trust the operator. A misconfigured
    large value will surface as a failed PRAGMA which is logged
    by ``open_db``.

    Note: we read the env var directly here to avoid importing
    ``config.py`` from ``db.py`` (circular-import risk in the
    6-module refactor — ``config.py`` imports nothing from
    ``db`` but historically the chain broke).
    """
    import os

    raw = os.environ.get("MEMORY_SQLITE_MMAP_SIZE", "268435456")
    try:
        return int(raw)
    except ValueError:
        return 268_435_456  # 256 MiB safe default


_startup_checkpoint_lock = threading.Lock()
_STARTUP_CHECKPOINT_DONE: bool = False


class PoolExhaustedError(Exception):
    """Raised when every pooled connection is active (depth > 0).

    P0-3 fix (2026-06-22): previously the pool would close the
    least-recently-used conn regardless of whether the caller was still
    using it.  Now we skip active conns during LRU eviction; if every
    conn is active, we fail fast with this error rather than silently
    corrupting the caller's transaction.
    """


def wal_checkpoint_idle(db_path: Path, wal_size_threshold_mb: float = 10.0) -> dict:
    """Run ``PRAGMA wal_checkpoint(PASSIVE)`` when the WAL exceeds *wal_size_threshold_mb*.

    PASSIVE checkpoint writes dirty pages from the WAL into the main DB
    file without acquiring a write lock — safe under concurrent readers
    and writers.  Called from:
      1. ``open_db()`` on first invocation per process (startup),
      2. ``memory_compact`` (post-rebuild).

    Returns a dict with checkpoint status.  ``status`` is one of
    ``"skipped"``, ``"done"``, or ``"error"``; the actual state is
    reported in ``reason`` (for ``"skipped"`` cases) so callers can
    distinguish an expected no-op (WAL below threshold) from an
    anomalous one (missing parent, ":memory:" path, etc.).  ``ok``
    mirrors the old contract: ``True`` for expected skips and
    successful checkpoints, ``False`` for busy checkpoints, errors,
    and anomalous skip conditions (missing parent dir, missing db).
    """
    db_path = Path(db_path)
    if not db_path.name or str(db_path) == ":memory:":
        return {
            "status": "skipped",
            "ok": True,
            "reason": "in_memory_db",
            "wal_size_mb": 0.0,
            "threshold_mb": wal_size_threshold_mb,
        }
    if not db_path.parent or not db_path.parent.exists():
        return {
            "status": "skipped",
            "ok": True,
            "reason": "parent_dir_missing",
            "wal_size_mb": 0.0,
            "threshold_mb": wal_size_threshold_mb,
        }
    wal_path = db_path.parent / (db_path.name + "-wal")
    wal_size_mb = 0.0
    if wal_path.exists():
        try:
            wal_size_mb = wal_path.stat().st_size / (1024 * 1024)
        except OSError as exc:
            logger.debug("db: wal stat_failed %s: %s", wal_path, exc)
            wal_size_mb = 0.0
    if wal_size_mb < wal_size_threshold_mb:
        return {
            "status": "skipped",
            "ok": True,
            "reason": "wal_below_threshold",
            "wal_size_mb": round(wal_size_mb, 2),
            "threshold_mb": wal_size_threshold_mb,
        }
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000;")
        row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        if row and len(row) >= 4:
            busy, log_pages, ckpt_pages, wal_pages = row[0], row[1], row[2], row[3]
        elif row:
            busy = row[0] if row else 0
            log_pages = ckpt_pages = wal_pages = 0
        else:
            busy = log_pages = ckpt_pages = wal_pages = 0
        return {
            "status": "done",
            "ok": busy == 0,
            "busy": bool(busy),
            "log_pages": log_pages,
            "checkpointed_pages": ckpt_pages,
            "wal_pages": wal_pages,
            "wal_size_mb": round(wal_size_mb, 2),
            "threshold_mb": wal_size_threshold_mb,
        }
    except Exception as e:
        return {
            "status": "error",
            "ok": False,
            "error": str(e),
            "wal_size_mb": round(wal_size_mb, 2),
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.warning(
                    "Failed to close connection in wal_checkpoint_idle finally"
                )
                pass


def _maybe_checkpoint_on_startup(path: Path) -> None:
    """One-shot PASSIVE checkpoint on first ``open_db()`` call per process.

    Runs only once (guarded by ``_STARTUP_CHECKPOINT_DONE``) and only
    when ``MEMORY_WAL_CHECKPOINT_STARTUP=1`` env var is set.
    Errors are swallowed — startup must never fail because of a checkpoint.
    """
    import os

    global _STARTUP_CHECKPOINT_DONE
    # Resolve the config flag *before* claiming the one-shot so that
    # workers whose config has wal_checkpoint_startup=False don't
    # accidentally mark the global as "done" and skip later processes'
    # checkpoints. This was the bug classified as H3 in the audit:
    # the previous code set _STARTUP_CHECKPOINT_DONE outside the
    # config check, causing one process to silently disarm the
    # checkpoint for the entire daemon lifecycle.
    from _lazy_imports import get_config

    if not get_config().wal_checkpoint_startup:
        return
    with _startup_checkpoint_lock:
        if _STARTUP_CHECKPOINT_DONE:
            return
        _STARTUP_CHECKPOINT_DONE = True
    try:
        result = wal_checkpoint_idle(path, wal_size_threshold_mb=10.0)
        if result.get("status") != "skipped":
            logging.getLogger(__name__).info("startup WAL checkpoint: %s", result)
    except Exception:
        logger.warning("db: wal checkpoint_startup_failed")
        pass


def safe_close_db(conn: AnyConnection, *, should_commit: bool = True) -> None:
    """Commit or rollback, then close or return to pool. Never raises.

    ...

    Phase 4 (2026-06-11): ``should_commit`` parameter.  When False
    (exception is active), rollback instead of commit to avoid
    committing partial/corrupted writes.

    NOTE: catches Exception, NOT BaseException, so that KeyboardInterrupt
    and SystemExit propagate rather than being silently swallowed during
    shutdown.
    """
    try:
        depth = connection_pool.get_depth(conn)
        if depth <= 1:
            try:
                if should_commit:
                    conn.commit()
                else:
                    conn.rollback()
            except Exception:
                logger.warning("db: commit_or_rollback_failed in safe_close_db")
                pass
        connection_pool.put(conn)
    except Exception:
        logger.warning("db: pool_return_failed in safe_close_db")
        pass


@contextmanager
def open_db(
    path: Path,
    timeout: float = 30.0,
    row_factory: Optional[Any] = None,
    pooled: bool = False,
    write: bool = True,
) -> Iterator[AnyConnection]:
    """Open a sqlite3 connection as a context manager with sane defaults.

    ...
    """
    from db_migrations import run_schema_setup
    from contextlib import nullcontext

    # B-3 follow-up (2026-06-22): cross-process serialisation.  The
    # flock is acquired on entry and released on exit.  When the
    # env var ``MEMORY_DB_FLOCK=0``, ``db_path_flock()`` is a no-op.
    from db_path_flock import db_path_flock

    path = Path(path)
    if write:
        from db_write_queue import sqlite_write_queue

        conn = cast(AnyConnection, sqlite_write_queue.start_session(path))
        exc_info = None
        try:
            if row_factory is not None:
                conn.row_factory = row_factory
            run_schema_setup(conn)
            yield conn
        except BaseException as exc:
            exc_info = exc
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        else:
            try:
                conn.commit()
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return

    lock_ctx = db_path_flock(path) if write else nullcontext()
    with lock_ctx:
        if pooled:
            conn = connection_pool.get(str(path), timeout=timeout)
            original_row_factory = conn.row_factory
            exc_info = None
            try:
                if row_factory is not None:
                    conn.row_factory = row_factory
                yield conn
            except BaseException as exc:
                exc_info = exc
                raise
            finally:
                conn.row_factory = original_row_factory
                safe_close_db(conn, should_commit=(exc_info is None))
            return
        conn = sqlite3.connect(str(path), timeout=timeout)
        exc_info = None
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)};")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError as e:
                logging.getLogger(__name__).debug(f"WAL not enabled on {path}: {e}")
            # S4.1 (2026-06-23): apply mmap_size for non-pooled path too.
            _mmap = _resolve_mmap_size()
            if _mmap > 0:
                try:
                    conn.execute(f"PRAGMA mmap_size={int(_mmap)}")
                except sqlite3.OperationalError as e:
                    logging.getLogger(__name__).debug(
                        f"mmap_size={_mmap} not applied on {path}: {e}"
                    )
            if row_factory is not None:
                conn.row_factory = row_factory
            run_schema_setup(conn)
            _maybe_checkpoint_on_startup(path)
            yield conn
        except BaseException as exc:
            exc_info = exc
            raise
        finally:
            safe_close_db(conn, should_commit=(exc_info is None))


def count_rows(db_dir: Path) -> int:
    """Return the row count of ``db_dir/memory.db``, or -1 on any error.

    M4 fix: replaces the private ``_row_count`` helper that lived in
    memory_mcp.py. Used by the smart-routing layer to choose between the
    local and global memory DBs without having to duplicate the
    connect/close dance.

    Args:
        db_dir: Directory containing ``memory.db``.

    Returns:
        Row count as int, or -1 if the DB is missing or unreadable.
    """
    db = Path(db_dir) / "memory.db"
    if not db.exists():
        return -1
    try:
        with open_db(db, timeout=5.0) as conn:
            row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            n = row[0] if row is not None else 0
            return n
    except Exception:
        logger.warning("db: row_count_failed in %s", db)
        return -1


# Thread-local state to prevent deadlock during save/telemetry operations
_local_state = threading.local()
