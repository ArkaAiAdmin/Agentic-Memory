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

from infra.db_write_queue import ProxyConnection


AnyConnection = Union[sqlite3.Connection, ProxyConnection]

# Column list for the memories table (used by the INSTEAD OF UPDATE trigger
# on the tenant_memories view).  Kept in sync with the migration schema.
_MEMORIES_COLUMNS = (
    "id", "content", "source_file", "tags", "created_at", "updated_at",
    "observed_at", "pinned", "importance", "decay", "score", "supersedes",
    "repo_id", "access_count", "success_score", "fitness_score",
    "conflict_policy", "version_vector", "logical_clock",
    "consolidation_state", "tenant_id", "valid_from", "valid_to",
    "superseded_by", "last_accessed", "deleted_at", "deleted_by",
    "context_prefix", "category", "tier", "importance_score", "metadata",
    "data_subject_sub",
)

# Module-level flag for tenant_memories view: when True, the view returns
# ALL memories (bypassing tenant filtering). Set via set_include_global().
_include_global = False


def set_include_global(enabled: bool) -> None:
    """Set the global flag for the tenant_memories view.

    When enabled=True, the view returns all memories regardless of tenant.
    Must be called before executing queries that use tenant_memories.
    Reset to False after the query completes.
    """
    global _include_global
    _include_global = enabled


def _setup_tenant_view(conn: Any, tenant_id: str) -> None:
    """Register tenant_id() UDF and create the tenant_memories view.

    Creates an INSTEAD OF UPDATE trigger so that helpers like
    ``_crdt_bump_version`` and ``_enrich_context`` can write through
    the view transparently (SQLite views are not directly writable).

    The view respects ``set_include_global()``: when the global flag is
    set, the view returns ALL memories (bypassing tenant filtering).
    This allows ``include_global=True`` searches to see memories across
    all tenants.
    """
    try:
        conn.create_function("tenant_id", 0, lambda: tenant_id)
        conn.create_function("is_global", 0, lambda: int(_include_global))
        conn.execute("DROP VIEW IF EXISTS tenant_memories")
        conn.execute(
            "CREATE TEMP VIEW tenant_memories AS "
            "SELECT * FROM memories WHERE tenant_id = tenant_id() OR is_global() = 1"
        )
        # INSTEAD OF UPDATE trigger: redirect writes to the base table.
        cols = ", ".join(f"NEW.{c}" for c in _MEMORIES_COLUMNS)
        col_list = ", ".join(_MEMORIES_COLUMNS)
        conn.execute(f"DROP TRIGGER IF EXISTS _tenant_memories_update")
        conn.execute(
            f"CREATE TEMP TRIGGER _tenant_memories_update "
            f"INSTEAD OF UPDATE ON tenant_memories BEGIN "
            f"UPDATE memories SET "
            f"({col_list}) = (SELECT {cols}) "
            f"WHERE id = OLD.id; END"
        )
    except Exception:
        pass

__all__ = [
    "connection_pool",
    "safe_close_db",
    "open_db",
    "wal_checkpoint_idle",
    "count_rows",
    "AnyConnection",
]


class _ConnectionPool:
    """Thread-safe per-path connection pool for SQLite.

    Reuses connections within a single thread. Connections are validated
    with a lightweight PRAGMA check before reuse. Dead connections are
    silently replaced. Enforces a max pool size with LRU eviction.
    """

    def __init__(self, max_size: int | None = None) -> None:
        if max_size is None:
            max_size = int(os.environ.get("MEMORY_DB_POOL_SIZE", "24"))
        self._lock = threading.Lock()
        self._pool: dict[tuple[str, int], sqlite3.Connection] = {}
        self._pooled_ids: set[int] = set()
        self._max_size = max_size
        self._lru: deque[tuple[str, int]] = deque()
        self._migrated: set[tuple[str, int]] = set()  # (db_path, conn_id) pairs with full schema
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
        self._reval_thread = threading.Thread(target=self._revalidate_loop, daemon=True)
        self._reval_thread.start()

    def _revalidate_loop(self) -> None:
        import time
        while True:
            time.sleep(30)
            with self._lock:
                keys = list(self._pool.keys())
                for key in keys:
                    if key not in self._pool:
                        continue
                    conn = self._pool[key]
                    if self._depth.get(key, 0) == 0 and self._inode_mismatch(key, conn):
                        if key in self._lru:
                            try:
                                self._lru.remove(key)
                            except ValueError:
                                pass
                        self._pool.pop(key)
                        conn_id = id(conn)
                        self._pooled_ids.discard(conn_id)
                        self._migrated.discard(key)
                        self._inodes.pop(key, None)
                        try:
                            conn.close()
                            logger.info("db pool: evicted connection for %s in background due to inode drift", key[0])
                        except Exception:
                            pass

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

    def _inode_mismatch(self, key: tuple[str, int], conn: AnyConnection) -> bool:
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
            self._migrated.discard((key[0], conn_id))
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

    def _ensure_full_schema(self, conn: AnyConnection) -> None:
        """Run all Python schema migrations on a new connection.

        This is the single source of truth for schema setup on pooled
        connections. Previously only open_db() ran these, causing drift
        when save_pipeline.py used connection_pool.get() directly.

        Migration helpers live in ``db_migrations.py``.

        Thread safety: Always invoked *after* ``self._lock`` has been
        released by ``get()`` (see line ~191). To prevent deadlock,
        callers must NEVER call this method while holding self._lock.
        """
        try:
            db_path_row = conn.execute("PRAGMA database_list").fetchone()
            db_path = db_path_row[2] if db_path_row is not None else ""
        except Exception:
            logger.warning("db: db_path resolve_failed from PRAGMA database_list")
            db_path = ""

        conn_id = id(conn)
        migrated_key = (db_path, conn_id)
        if migrated_key in self._migrated:
            return

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
                if migrated_key in self._migrated:
                    return
                try:
                    from infra.db_migrations import run_schema_setup

                    run_schema_setup(conn)

                    # Perform one-shot startup checkpoint if needed
                    if db_path:
                        _maybe_checkpoint_on_startup(Path(db_path).parent)
                    self._migrated.add(migrated_key)
                except Exception as exc:
                    logger.warning(
                        "_ensure_full_schema failed: %s", exc
                    )  # best-effort; callers can retry via open_db()
        else:
            # No path lock (e.g. :memory: DB) — proceed without serialisation.
            if migrated_key in self._migrated:
                return
            try:
                from infra.db_migrations import run_schema_setup

                run_schema_setup(conn)
                self._migrated.add(migrated_key)
            except Exception as exc:
                logger.warning("db: schema ensure_failed: %s", exc)

    def get(self, path: str, timeout: float = 30.0, tenant_id: str | None = None) -> sqlite3.Connection:
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
                        # Only update tenant_id when explicitly provided.
                        # Many callers don't pass tenant_id — they should
                        # not reset the VIEW to 'default' and break queries
                        # that relied on a previous tenant setting.
                        if tenant_id is not None:
                            t_id = tenant_id
                            _setup_tenant_view(conn, t_id)
                        return conn
            # Reuse an idle connection from another thread holding the same path
            for other_key in list(self._pool):
                if other_key[0] == path and self._depth.get(other_key, 0) == 0:
                    candidate = self._pool.pop(other_key)
                    self._depth.pop(other_key, None)
                    self._inodes.pop(other_key, None)
                    try:
                        self._lru.remove(other_key)
                    except ValueError:
                        pass
                    # Validate candidate connection
                    stale = False
                    try:
                        candidate.execute("SELECT 1")
                    except Exception:
                        stale = True
                    if not stale and self._inode_mismatch(other_key, candidate):
                        stale = True

                    if stale:
                        try:
                            candidate.close()
                        except Exception:
                            pass
                        self._pooled_ids.discard(id(candidate))
                        continue

                    # Re-assign valid candidate connection to current key
                    self._pool[key] = candidate
                    self._depth[key] = 1
                    self._inodes[key] = self._inode_of(path)
                    self._lru.append(key)
                    t_id = tenant_id or "default"
                    _setup_tenant_view(candidate, t_id)
                    return candidate
            self._evict_lru()
            conn = sqlite3.connect(path, timeout=timeout)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            conn.execute("PRAGMA foreign_keys=ON")
            # synchronous=NORMAL trades a small durability window for ~2x
            # write throughput.  With WAL mode, data is fsynced to the WAL
            # on each transaction commit; NORMAL skips the second sync on
            # checkpoint.  Risk: a power loss between checkpoint and the
            # second sync may lose up to one checkpoint worth of writes.
            # FULL would eliminate that window but halve write throughput.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA wal_autocheckpoint=500")
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

        # Bind tenant_id and configure view routing
        t_id = tenant_id or "default"
        _setup_tenant_view(conn, t_id)

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
                self._migrated.discard(key)
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
                self._migrated.discard(key)
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
                    self._migrated.discard(key)
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
                    logger.warning("db: connection close_failed during close_all")
                    pass
            self._pool.clear()
            self._pooled_ids.clear()
            self._migrated.clear()
            self._depth.clear()
            self._lru.clear()
            self._inodes.clear()

    def get_state(self) -> dict:
        """Return a snapshot of pool state for monitoring."""
        with self._lock:
            active = sum(1 for depth in self._depth.values() if depth > 0)
            idle = len(self._pool) - active
            return {
                "active": active,
                "idle": idle,
                "max_size": self._max_size,
            }

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

    global _STARTUP_CHECKPOINT_DONE
    # Resolve the config flag *before* claiming the one-shot so that
    # workers whose config has wal_checkpoint_startup=False don't
    # accidentally mark the global as "done" and skip later processes'
    # checkpoints. This was the bug classified as H3 in the audit:
    # the previous code set _STARTUP_CHECKPOINT_DONE outside the
    # config check, causing one process to silently disarm the
    # checkpoint for the entire daemon lifecycle.
    from infra._lazy_imports import get_config

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


def get_db_connection(db_path: str | Path, timeout: float = 10.0) -> Any:
    """Get a connection from the pool for *db_path*.

    Returns the connection object; the caller is responsible for
    calling ``safe_close_db()`` when done.

    Detects the current agent context and passes its tenant_id to the
    pool so the tenant_memories TEMP VIEW filters correctly.
    """
    from infra._lazy_imports import connection_pool

    tenant_id = "default"
    try:
        from agent_context import get_agent

        ctx = get_agent()
        if ctx and ctx.agent_id:
            tenant_id = ctx.agent_id
    except Exception:
        pass
    return connection_pool.get(str(db_path), timeout=timeout, tenant_id=tenant_id)


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
    pooled: bool = True,
    write: bool = True,
    tenant_id: str | None = None,
) -> Iterator[AnyConnection]:
    """Open a sqlite3 connection as a context manager with sane defaults.

    ...
    """
    from infra.db_migrations import run_schema_setup
    from contextlib import nullcontext



    path = Path(path)
    if str(path) != ":memory:":
        try:
            if not path.exists():
                path.touch(mode=0o600)
            elif path.stat().st_mode & 0o777 != 0o600:
                path.chmod(0o600)
        except OSError:
            logger.debug("Could not set permissions on %s", path)
        except Exception as exc:
            logger.warning("Unexpected error on %s: %s", path, exc)
    if write:
        from infra.db_write_queue import sqlite_write_queue

        conn = cast(AnyConnection, sqlite_write_queue.start_session(path))
        exc_info = None
        try:
            if row_factory is not None:
                conn.row_factory = row_factory
            run_schema_setup(conn)
            # Run saga crash recovery once per process (per DB path).
            try:
                from infra.saga import recover_incomplete_sagas
                n = recover_incomplete_sagas(conn)
                if n:
                    logger.info("saga recovery: recovered %d incomplete sagas", n)
            except Exception:
                pass  # non-fatal: saga_log table may not exist yet
            t_id = tenant_id or "default"
            _setup_tenant_view(conn, t_id)
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

    if not write and not pooled:
        pooled = True

    # Write path returns above (line ~840); only read/pooled reaches here.
    # db_path_flock is engaged per-command by the write queue for write sessions.
    with nullcontext():
        if pooled:
            conn = connection_pool.get(str(path), timeout=timeout, tenant_id=tenant_id)
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
            conn.execute("PRAGMA wal_autocheckpoint=500")
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
            t_id = tenant_id or "default"
            _setup_tenant_view(conn, t_id)
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
