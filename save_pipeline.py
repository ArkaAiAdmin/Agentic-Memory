"""Save-related functions extracted from memory_mcp.py.

Contains _update_memory_index_incremental, _recalculate_fitness_scores,
_auto_backlink_multi_part, and save_memory.
"""

__all__ = [
    "SaveRequest",
    "save_memory",
    "upsert_row",
    "memory_supersede_db",
    "reinforce_memories_db",
    "_update_memory_index_incremental",
    "_recalculate_fitness_scores",
    "_auto_backlink_multi_part",
    "clear_pragma_cache",
    "patch_memory",
    "revert_supersede",
    "_record_revision_log",
    # Re-exports from the save/ subpackage (preserved for callers that
    # import these from save_pipeline directly).
    "_crdt_agent_id",
    "_is_crdt_enabled",
    "_is_legacy_note_crdt_enabled",
    "_crdt_bump_version",
]
from dataclasses import dataclass
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable, Any
from infra.memory_common import (
    safe_close_db,
    acquire_flock_with_retry,
    release_flock,
    atomic_write,
    get_memory_paths,
    parse_frontmatter,
)
from infra.infrastructure import (
    _err,
    ErrorCode,
    resolve_active_memory_dir,
    GLOBAL_MEM_DIR,
)
from infra.db import AnyConnection, open_db  # noqa: E402,F401 — backward compat re-export
import infra.audit as audit
from self_directed import _assign_tier as assign_tier
from backfill.orchestrator import auto_backfill


@dataclass(frozen=True)
class SaveRequest:
    content: str
    category: str
    title_slug: str
    tags: Optional[list] = None
    pinned: bool = False
    is_global: bool = False
    safety_wiring: bool = True
    db_path: str | None = None
    importance: int = 3
    note_id: str = ""
    context: str = "generic"
    defer_expensive: bool = False
    tenant_id: str = "default"
    epistemic_source: str = "agent"
    belief_status: str = "active"
    asserting_agent_id: str = ""
    evidence_chain: list | None = None
    fact_type: str = "observation"


def _md5_to_uint64(memory_id: str) -> int:
    """Derive a stable uint64 key from a memory id.

    md5 first 8 bytes -> unsigned int, masked to signed int64 range
    (0..2^63-1) so the value fits in SQLite's INTEGER column. usearch
    accepts uint64 keys, but Python's sqlite3 module refuses values
    that exceed signed int64 — masking the high bit is the simplest
    way to make the key round-trip through both.

    Collision probability for 1M items: ~2.7e-7.
    """
    digest = hashlib.md5(memory_id.encode("utf-8")).digest()
    raw = int.from_bytes(digest[:8], "big", signed=False)
    return raw & ((1 << 63) - 1)


def _write_vec_key(db, note_id: str) -> int:
    """Write the memory_vec_keys mapping for a note.

    Returns the generated key for saga rollback tracking.
    """
    key = _md5_to_uint64(note_id)
    db.execute(
        "INSERT OR REPLACE INTO memory_vec_keys (key, memory_id) VALUES (?, ?)",
        (key, note_id),
    )
    return key


parse_version_vector: Callable[..., Any] | None = None
try:
    from crdt.crdt_merge import parse_version_vector
except ImportError:  # FLAVOR_A: optional dependency guard
    pass

logger = logging.getLogger(__name__)

# Try to import saga coordinator; fall back gracefully if unavailable.
_saga_save_memory: Callable[..., Any] | None = None
try:
    from infra.saga import saga_save_memory as _saga_save_memory
except ImportError:  # FLAVOR_A: optional dependency guard
    pass

_get_config: Callable[[], Any] | None = None
try:
    from config import get_config as _get_config
except ImportError:  # FLAVOR_A: optional dependency guard
    pass

# Cache for PRAGMA table_info results (per db_path)
_pragma_cache: dict[str, set] = {}
_pragma_cache_lock = threading.Lock()

# Schema feature sets (B5 consolidation: one source of truth for what
# the schema *can* do, not six copies of the same PRAGMA walk).
#
# 2026-06-22 (D5 fix): the feature set now includes every column that
# a future migration might add to ``memories``.  Previously the check
# only covered the temporal trio (``valid_from``, ``valid_to``,
# ``superseded_by``), which meant that columns added by later
# migrations (e.g. ``success_score``, ``tier``, ``fitness_score``) were
# silently absent from the upsert path — the row would be inserted
# with the column default and never written through ``_upsert_memory_row``.
# The single source of truth below is what the B5 fix replaced six
# inline copies of, and is the only place to update when a new column
# is added to the ``memories`` table.
_TEMPORAL_COLS = frozenset({"valid_from", "valid_to", "superseded_by"})
# Core columns: always in the upsert SQL or managed by a non-upsert
# write path (search index, vector index, tier updates).  Collected
# here so the drift check in _detect_schema_features can distinguish
# between "deliberately unmanaged (core)" and "forgotten by a migration".
_CORE_COLS = frozenset(
    {
        "id",
        "content",
        "embedding",
        "vec_key",
        "category",
        "created_at",
        "updated_at",
        "access_count",
        "search_score",
        # Always in the upsert INSERT + ON CONFLICT UPDATE SET.
        "source_file",
        "tags",
        "observed_at",
        "pinned",
        "importance",
        "repo_id",
        "tenant_id",
    }
)
# Optional columns that the upsert conditionally includes based on
# runtime schema detection (PRAGMA table_info results).  A column
# present in the live schema but absent from _CORE_COLS, _MANAGED_COLS,
# and _LEGACY_COLS will trigger a drift warning — it's likely a
# migration that forgot to wire the new column into the write path.
_MANAGED_COLS = frozenset(
    {
        "valid_from",
        "valid_to",
        "superseded_by",
        "tier",
        "success_score",
        "fitness_score",
        "importance_score",
        "metadata",
        "deleted_at",
    }
)
# Columns that exist in the schema for historical/legacy reasons
# but are no longer written by any active code path.  Safe to ignore.
_LEGACY_COLS = frozenset(
    {
        "decay",
        "score",
        "supersedes",
        "conflict_policy",
        "version_vector",
        "logical_clock",
        "consolidation_state",
        "last_accessed",
        "deleted_by",
        "context_prefix",
    }
)


def _detect_schema_features(db_path, conn=None) -> dict:
    """Return a dict of schema features for the memories table.

    Centralizes the ``PRAGMA table_info(memories)`` walk that used to be
    duplicated across ``upsert_row`` (line 321-328), the saga/CRDT
    upsert path in ``_update_memory_index_incremental`` (line 441-447),
    and ``memory_supersede_db`` (line 978-980).  Caches the column set
    per ``db_path`` so the cost is paid once per process per DB file.

    Returns:
        dict with keys:
            - ``has_temporal``: True iff {valid_from, valid_to, superseded_by} are present
            - ``has_tier``:     True iff the ``tier`` column is present
            - ``has_metadata``: True iff the ``metadata`` column is present
            - ``cols``:         the full set of column names (for downstream checks)

    Args:
        db_path: Path-like. Used as the cache key.  Required (not optional)
            so the cache is bounded by the set of actually-opened DBs.
        conn: Optional open sqlite3.Connection. If None, opens a short-lived
            connection from ``connection_pool`` using ``str(db_path)``.
    """
    cache_key = str(db_path)
    cols: set[str] = set()
    with _pragma_cache_lock:
        if cache_key in _pragma_cache:
            cols = _pragma_cache[cache_key]
        else:
            if conn is not None:
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(memories)").fetchall()
                }
                _pragma_cache[cache_key] = cols
            else:
                # Need to release the lock while we open a connection
                # so we don't deadlock if the pool blocks. Open outside
                # the lock, then re-check the cache and populate.
                pass
    if cache_key not in _pragma_cache:
        # Cache miss: open a short-lived pooled connection.
        from infra.db import open_db
        try:
            with open_db(db_path, timeout=5.0, pooled=True, write=False) as pool_conn:
                try:
                    cols = {
                        row[1]
                        for row in pool_conn.execute(
                            "PRAGMA table_info(memories)"
                        ).fetchall()
                    }
                except Exception:
                    logger.warning("PRAGMA table_info failed for %s", db_path)
                    cols = set()
        except Exception as exc:
            logger.warning("Failed to open connection to %s: %s", db_path, exc)
            cols = set()
        with _pragma_cache_lock:
            # Another caller may have populated the cache while we were
            # blocked on the pool; their value is fine, just keep theirs.
            _pragma_cache.setdefault(cache_key, cols)
            cols = _pragma_cache[cache_key]
    # Warn if any live schema column is unmanaged (not in any known
    # set) — such columns are silently dropped on every save.
    unmanaged = cols - _MANAGED_COLS - _CORE_COLS - _LEGACY_COLS
    if unmanaged:
        logger.warning(
            "Schema drift: columns %s exist in memories table but are not in "
            "_CORE_COLS, _MANAGED_COLS, or _LEGACY_COLS. They will be "
            "silently dropped on save.",
            sorted(unmanaged),
        )
    return {
        "has_temporal": _TEMPORAL_COLS.issubset(cols),
        "has_tier": "tier" in cols,
        "has_metadata": "metadata" in cols,
        "has_success_score": "success_score" in cols,
        "has_fitness_score": "fitness_score" in cols,
        "has_importance_score": "importance_score" in cols,
        "has_deleted_at": "deleted_at" in cols,
        "managed_cols": _MANAGED_COLS,
        "legacy_cols": _LEGACY_COLS,
        "cols": cols,
    }


def clear_pragma_cache():
    """Clear the PRAGMA table_info cache. Call when DB file is replaced."""
    with _pragma_cache_lock:
        _pragma_cache.clear()


# ---------------------------------------------------------------------------
# CRDT version vector helpers
#
# Extracted to save.crdt_helpers (2026-06-20). Re-exported here so
# existing callers using ``from save_pipeline import _crdt_agent_id``
# keep working without modification.
# ---------------------------------------------------------------------------
from save.crdt_helpers import (  # noqa: E402, F401
    _crdt_agent_id,
    _is_crdt_enabled,
    _is_legacy_note_crdt_enabled,
    _crdt_bump_version,
)


def _ensure_db_exists(db_path: Path):
    """Create the DB and run migrations if it doesn't exist yet."""
    if not db_path.exists():
        try:
            with open_db(db_path, timeout=30.0):
                pass
        except Exception as e:
            logger.error("_ensure_db_exists: could not initialize DB at %s: %s", db_path, e)
            return False
    # A5 fix: only invalidate pragma cache for this specific db_path,
    # not all databases (clear_pragma_cache was called unconditionally
    # on every save, destroying the PRAGMA table_info cache for all DBs).
    with _pragma_cache_lock:
        _pragma_cache.pop(str(db_path), None)
    return True


def _acquire_lock(db_path: Path):
    """Acquire a flock for the write path. Returns lock_file or None.

    P1-15 fix: write-path callers must not silently proceed without
    the lock — the vec_keys drift we saw in the worker log is the
    exact signature of two concurrent saves both running. Pass
    ``strict=True`` so a held lock surfaces as ``FileLockError`` at
    the call site rather than a silent race. The lock_file handle is
    only returned on success; on failure the temp file is closed.

    P0-5 fix (2026-06-22): the previous version caught
    ``FileLockError`` and returned ``None`` here, which silently
    defeated the strict-mode contract.  The docstring has always
    said FileLockError must surface, but the implementation
    swallowed it.  Now FileLockError propagates so the caller can
    decide whether to retry, fall back, or surface the error.
    Non-lock-related exceptions (e.g. ``OSError`` opening the lock
    file) still return ``None`` because they're not contention
    errors — they're infrastructure errors that the caller can't
    usefully handle.
    """
    lock_path = db_path.parent / ".rebuild.lock"
    try:
        lock_file = open(lock_path, "w")
    except Exception as e:
        logger.warning("Could not open lock file for incremental update: %s", e)
        return None
    # FileLockError propagates (strict=True contract).  Other
    # exceptions (e.g. OSError from a stale lock file) are caught
    # and converted to None so the caller can proceed without a
    # lock in the case of an infrastructure error, not contention.
    from infra._lazy_imports import FileLockError
    try:
        acquire_flock_with_retry(
            lock_file, max_attempts=5, initial_backoff=0.05, strict=True
        )
        return lock_file
    except FileLockError:
        from infra.file_lock import _is_stale_lock

        if _is_stale_lock(lock_path):
            logger.info(
                "Removing stale lock %s (no live flock holder detected)",
                lock_path,
            )
            try:
                lock_file.close()
            except Exception as close_exc:
                logger.debug("lock_file.close() failed for stale lock %s: %s", lock_path, close_exc)
            try:
                lock_path.unlink()
            except Exception as e:
                logger.debug("Could not remove stale lock %s: %s", lock_path, e)
                raise
            try:
                lock_file = open(lock_path, "w")
            except Exception as e:
                logger.warning(
                    "Could not reopen lock file after stale cleanup: %s", e
                )
                raise
            try:
                acquire_flock_with_retry(
                    lock_file, max_attempts=3, initial_backoff=0.02, strict=True
                )
                return lock_file
            except FileLockError:
                raise
        raise
    except Exception as e:
        try:
            lock_file.close()
        except Exception as close_exc:
            logger.debug("lock_file.close() failed during flock error: %s", close_exc)
        logger.warning(
            "Could not acquire flock for lock file %s: %s", lock_path, e
        )
        return None


def _upsert_memory_row(
    db,
    note_id: str,
    source_file: str,
    content: str,
    tags_json: str,
    now_iso: str,
    pinned: bool,
    is_global: bool,
    db_path: Path,
    category: str,
    has_temporal: bool,
    metadata_json: str = "{}",
    importance: int = 3,
    tier: str = "warm",
    tenant_id: str = "default",
    cols: set | None = None,
):
    """Insert or update the memory row (and file_mtimes).

    2026-06-19 fix: ``importance`` is now a real parameter. Previously
    the INSERT statement hardcoded ``importance=3`` regardless of
    caller intent — user-supplied importance values were silently
    dropped, and the MCP ``memory_save`` tool had no way to set
    importance at all. The default is 3 (preserves the prior
    behavior for callers that don't pass importance explicitly).
    Values are clamped to [1, 5] so a misbehaving caller can't
    produce rows outside the documented scale.
    """
    if cols is None:
        try:
            cols = {
                row[1]
                for row in db.execute("PRAGMA table_info(memories)").fetchall()
            }
        except Exception as _pragma_exc:
            logger.debug("PRAGMA table_info(memories) failed: %s", _pragma_exc)
            cols = set()
    has_tenant = "tenant_id" in cols
    importance = max(1, min(5, int(importance)))
    repo_id = None if is_global else db_path.parent.parent.name
    try:
        json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(
            "_upsert_memory_row: invalid metadata_json for %s: %s — defaulting to {}",
            note_id,
            exc,
        )
        metadata_json = "{}"

    def _mk_insert(temporal: bool) -> tuple[str, tuple[Any, ...]]:
        base_cols = [
            "id", "source_file", "content", "tags",
            "created_at", "updated_at", "observed_at",
            "fitness_score", "importance", "importance_score",
            "pinned", "repo_id",
        ]
        if temporal:
            base_cols += ["valid_from", "valid_to", "superseded_by"]
        base_cols += ["category", "tier", "metadata"]
        if has_tenant:
            base_cols.append("tenant_id")
        col_sql = ", ".join(base_cols)
        ph = ", ".join(["?"] * len(base_cols))
        update_cols = [
            "content = excluded.content",
            "tags = excluded.tags",
            "updated_at = excluded.updated_at",
            "observed_at = excluded.observed_at",
            "pinned = excluded.pinned",
            "fitness_score = excluded.fitness_score",
            "importance = excluded.importance",
            "importance_score = excluded.importance_score",
            "deleted_at = NULL",
            "category = excluded.category",
            "tier = excluded.tier",
            "metadata = COALESCE(excluded.metadata, memories.metadata)",
        ]
        if temporal:
            update_cols.append(
                "valid_from = COALESCE(memories.valid_from, excluded.valid_from)"
            )
        if has_tenant:
            update_cols.append("tenant_id = excluded.tenant_id")
        update_sql = ", ".join(update_cols)
        vals: tuple[Any, ...]
        if temporal:
            vals = (
                note_id, source_file, content, tags_json,
                now_iso, now_iso, now_iso,
                0.5, importance, float(importance),
                1 if pinned else 0, repo_id,
                now_iso, None, None,
                category, tier, metadata_json,
            )
        else:
            vals = (
                note_id, source_file, content, tags_json,
                now_iso, now_iso, now_iso,
                0.5, importance, float(importance),
                1 if pinned else 0, repo_id,
                category, tier, metadata_json,
            )
        if has_tenant:
            vals = vals + (tenant_id,)
        sql = (
            f"INSERT INTO memories ({col_sql}) VALUES ({ph}) "
            f"ON CONFLICT(id) DO UPDATE SET {update_sql}"
        )
        return sql, vals

    if has_temporal:
        sql, vals = _mk_insert(temporal=True)
    else:
        sql, vals = _mk_insert(temporal=False)
    db.execute(sql, vals)
    try:
        fm_cols = {
            row[1] for row in db.execute("PRAGMA table_info(file_mtimes)").fetchall()
        }
        fm_path_col = (
            "path"
            if "path" in fm_cols
            else "file_path"
            if "file_path" in fm_cols
            else None
        )
        if fm_path_col:
            db.execute(
                f"INSERT INTO file_mtimes ({fm_path_col}, mtime, content_hash) VALUES (?, strftime('%s', 'now'), '') ON CONFLICT({fm_path_col}) DO UPDATE SET mtime = excluded.mtime",
                (source_file,),
            )
    except Exception as e:
        logger.debug("file_mtimes update skipped for %s: %s", source_file, e)


def upsert_row(
    conn,
    note_id: str,
    content: str,
    source_file: str,
    tags,
    category: str,
    pinned: bool = False,
    tier: str = "warm",
    metadata=None,
    db_path=None,
    is_global: bool = False,
    importance: int = 3,
    tenant_id: str = "default",
) -> None:
    """Public upsert: insert or update a single memory row.

    Thin public wrapper around the private ``_upsert_memory_row`` so the
    few code paths that need to manage their own transaction (e.g.
    ``crdt_merge`` conflict resolution, ``cross_session_learn`` cron
    job, ``multi_agent`` import) can still go through the schema-aware
    save pipeline.

    B4 docstring fix (2026-06-22): actual callers (verified via grep):
      * ``crdt_field.py:601`` — ``crdt_field_save`` field-level path
        (writes field updates through this function so the row stays
        in sync with the field CRDT table).
      * ``crdt_field.py:806`` — ``_seed_note_into_field_crdt`` backfill
        path used when a note appears in ``memory_field_crdt`` but
        somehow has no row in ``memories``.
      * ``crdt_merge.py:290`` — legacy note-level LWW merge, kept
        after the v13 field-CRDT refactor for pre-v13 peers.
      * ``crdt_merge.py:335`` — second note-level merge site (the
        ``supersede`` policy).
      * ``cross_session_learn.py:183`` — cron job that imports
        previously-cross-session lessons back into the active DB.
      * ``memory_sharing.py:404`` — shared memory pool import (in-DB
        shared pool, not a network import).

    New callers should route through ``save_memory`` instead unless
    they need to manage their own transaction (in which case
    ``upsert_row`` is the supported public entry point).

    P0-5 fix (2026-06-19): the four callers that previously did raw
    ``INSERT INTO memories`` now route through this function instead
    of bypassing the save pipeline.  Routing through the public API
    keeps the row schema (fitness_score, importance, repo_id,
    valid_from, valid_to, metadata, file_mtimes) consistent with the
    canonical ``save_memory`` path, and gives the saga transaction /
    audit pipeline / safety wiring a single choke point.

    Transaction control is the caller's responsibility: this function
    does NOT commit and does NOT run any of the post-insert indexers
    (chunks, embedding, KG, facts, semantic/FTS backlinks, adaptive
    retention, CRDT version bump, contextual enrichment).  Callers
    that need the full save-pipeline behavior should call
    ``save_memory`` instead; ``upsert_row`` is for code paths that
    either already manage their own transaction or want a custom
    subset of indexers.

    Args:
        conn: An open ``sqlite3.Connection``.  The caller controls the
            transaction; this function will not commit or rollback.
        note_id: Canonical note id (e.g. ``"lessons/foo"``).
        content: The note's text content.
        source_file: The path the note is "from" (markdown file path).
        tags: A list of tag strings, or ``None`` / empty for no tags.
        category: The note's category (e.g. ``"lessons"``).
        pinned: Whether the note should be pinned.
        tier: The note's tier (``"warm"``, ``"untrusted"``, etc.).
            Default ``"warm"``.  Applied as a separate UPDATE so the
            private ``_upsert_memory_row`` keeps its existing
            signature.
        metadata: Optional dict to store in the ``metadata`` column.
            ``None`` stores an empty JSON object.
        db_path: Path to the memory DB.  Defaults to
            ``Path.cwd() / "memory" / "memory.db"`` if not given.
        is_global: Whether this is a global (cross-project) memory.
            Default ``False``.
    """
    if db_path is None:
        db_path = Path.cwd() / "memory" / "memory.db"

    # Normalize tags: accept list, comma/semicolon string, or None.
    if tags is None:
        tags_list: list = []
    elif isinstance(tags, str):
        tags_list = [t.strip() for t in re.split("[,; ]+", tags) if t.strip()]
    else:
        tags_list = [str(t).strip() for t in tags if t]
    tags_json = json.dumps(tags_list)

    # Normalize metadata: dict -> JSON, None -> empty object.
    if metadata is None:
        metadata_json = "{}"
    elif isinstance(metadata, str):
        try:
            json.loads(metadata)
            metadata_json = metadata
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "upsert_row: non-JSON-serializable string metadata for %s (%s): %s — defaulting to {}",
                note_id,
                metadata[:80] if len(metadata) > 80 else metadata,
                exc,
            )
            metadata_json = "{}"
    else:
        try:
            metadata_json = json.dumps(metadata)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "upsert_row: non-JSON-serializable metadata for %s: %s — defaulting to {}",
                note_id,
                exc,
            )
            metadata_json = "{}"

    # Detect schema features (temporal columns + tier) by PRAGMA.
    # B5 fix: use the centralized helper instead of duplicating the
    # PRAGMA walk + subset check at every save entry point.
    features = _detect_schema_features(db_path, conn=conn)
    has_temporal = features["has_temporal"]

    now_iso = datetime.now(timezone.utc).isoformat()

    _upsert_memory_row(
        conn,
        note_id,
        source_file,
        content,
        tags_json,
        now_iso,
        pinned,
        is_global,
        db_path,
        category,
        has_temporal,
        metadata_json,
        importance,
        tier,
        tenant_id,
    )


# ---------------------------------------------------------------------------
# Per-signal indexer functions
#
# Extracted to save.indexers (2026-06-20). Re-exported here so existing
# callers using ``from save_pipeline import _index_chunks`` etc. keep
# working without modification.
# ---------------------------------------------------------------------------
from save.indexers import (  # noqa: E402, F401
    _index_backlinks,
    _index_chunks,
    _index_chunk_embeddings,
    _index_embedding,
    _index_kg,
    _index_facts,
    _index_adaptive_retention,
)


# ---------------------------------------------------------------------------
# Backlink generation functions
#
# Extracted to save.backlinks (2026-06-20). Re-exported here so existing
# callers using ``from save_pipeline import _auto_fts_backlinks`` etc.
# keep working without modification.
# ---------------------------------------------------------------------------
from save.backlinks import (  # noqa: E402, F401
    _auto_fts_backlinks,
    _auto_semantic_backlinks,
    _auto_backlink_multi_part,
)


# ---------------------------------------------------------------------------
# Post-save hook functions
#
# Extracted to save.post_save_hooks (2026-06-20). Re-exported here so
# existing callers using ``from save_pipeline import _enrich_context``
# etc. keep working without modification.
# ---------------------------------------------------------------------------
from save.post_save_hooks import (  # noqa: E402, F401
    _enrich_context,
    _recalculate_fitness_scores,
    _run_post_save_hooks,
    _enqueue_background_tasks,
)


def _update_memory_index_incremental(
    db_path: Path,
    category: str,
    title_slug: str,
    content: str,
    tags: list,
    pinned: bool,
    now_iso: str,
    is_global: bool,
    metadata_json: str = "{}",
    db=None,
    importance: int = 3,
    tier: str = "warm",
    defer_expensive: bool = False,
    tenant_id: str = "default",
    epistemic_source: str = "agent",
    belief_status: str = "active",
    asserting_agent_id: str = "",
    evidence_chain: list | None = None,
    fact_type: str = "observation",
):
    """Update memory index incrementally.

    If ``db`` is provided, use that connection (caller manages commit and close).
    When using an external ``db``, exceptions propagate upward so the caller
    can rollback the transaction. When ``db`` is None (default), manage our
    own connection, commit on success, and swallow exceptions on failure.

    When ``defer_expensive`` is True, skip embedding, KG, fact extraction, and
    context enrichment — these are enqueued as background tasks by the caller
    so the synchronous path stays fast.

    Sequential SQL statements per call (non-deferred, ``defer_expensive=False``):
      1.  _upsert_memory_row            → 1 INSERT/UPDATE
      2.  _crdt_bump_version            → 1 UPDATE
      3.  _index_backlinks              → 2–4 (DELETE + INSERT backlink rows)
      4.  _index_chunks                 → 2–3 (DELETE old chunks + INSERT new)
      5.  _index_chunk_embeddings       → 1 INSERT
      6.  _index_embedding              → 1 INSERT into memory_embeddings
      7.  _index_kg                     → 2–3 (INSERT entities + edges)
      8.  _index_facts                  → 3–5 (INSERT facts + FTS triggers)
      9.  _enrich_context               → 1–2 context-enrichment INSERTs
      10. _auto_semantic_backlinks      → 2 (semantic edge INSERTs)
      11. _auto_fts_backlinks           → 1 FTS backlink UPDATE
      12. _index_adaptive_retention     → 1 retention UPDATE
      13. file_mtimes INSERT/UPDATE     → 1 (in _upsert_memory_row finally block)
      ~Total non-deferred: 18–25 sequential conn.execute calls

    With ``defer_expensive=True`` (default for MCP memory_save since 2026-06-22):
      ~Total deferred: 8–10 sequential conn.execute calls
      Remaining 8–15 calls enqueued as background tasks (embedding_index,
      kg_and_fact_index, semantic_backlinks).
    """
    external_db = db is not None
    if not external_db:
        if not _ensure_db_exists(db_path):
            return
    source_file = f"{category}/{title_slug}.md"
    note_id = f"{category}/{title_slug}"
    tags_json = json.dumps(tags)
    # P0-5 fix (2026-06-22): _acquire_lock now re-raises FileLockError
    # on contention.  Catch it here so a contended lock doesn't crash
    # the caller — log and proceed without the lock.  The pool's
    # per-thread connection keys still serialise intra-process writes
    # on the same path, so this is a defence-in-depth fallback rather
    # than a primary correctness mechanism.
    from infra._lazy_imports import FileLockError

    if external_db:
        lock_file = None
    else:
        # C2 fix (2026-06-24): retry lock acquisition with backoff instead of
        # proceeding lockless. Cross-process writes must serialize via flock.
        # Intra-process writes are already serialized by pool's per-thread keys.
        max_attempts = 3
        base_backoff = 0.1
        for attempt in range(max_attempts):
            try:
                lock_file = _acquire_lock(db_path)
                break
            except FileLockError as _fle:
                if attempt == max_attempts - 1:
                    logger.error(
                        "incremental update: write lock contended for %s after %d attempts, failing: %s",
                        db_path,
                        max_attempts,
                        _fle,
                    )
                    raise
                wait_time = base_backoff * (2**attempt)
                logger.warning(
                    "incremental update: write lock contended for %s (attempt %d/%d), retrying in %.2fs: %s",
                    db_path,
                    attempt + 1,
                    max_attempts,
                    wait_time,
                    _fle,
                )
                time.sleep(wait_time)
    local_db = None
    try:
        if external_db:
            conn = db
        else:
            from infra.db_write_queue import sqlite_write_queue
            conn = sqlite_write_queue.start_session(db_path)
            from infra.db_migrations import run_schema_setup
            run_schema_setup(conn)
            local_db = conn
        # B5 fix: use the centralized helper instead of duplicating the
        # PRAGMA walk + subset check that lived here.
        features = _detect_schema_features(db_path, conn=conn)
        has_temporal = features["has_temporal"]
        cols = features["cols"]
        # Compute tier early so it can be written atomically in the upsert.
        if _get_config is not None:
            cfg = _get_config()
            if getattr(cfg, "temporal_tiers", False):
                tier = assign_tier(importance=float(importance))
        _upsert_memory_row(
            conn,
            note_id,
            source_file,
            content,
            tags_json,
            now_iso,
            pinned,
            is_global,
            db_path,
            category,
            has_temporal,
            metadata_json,
            importance,
            tier,
            tenant_id,
            cols=cols,
        )
        if _is_crdt_enabled():
            _crdt_bump_version(conn, note_id, cols)
        _index_backlinks(conn, note_id, content)
        _index_chunks(conn, note_id, content)
        _index_chunk_embeddings(conn, note_id)
        if not defer_expensive:
            _index_embedding(conn, note_id, content, category, tags, source_file)
            _index_kg(conn, note_id, content)
            _index_facts(conn, note_id, content, belief_status, epistemic_source,
                         asserting_agent_id=asserting_agent_id, evidence_chain=evidence_chain,
                         fact_type=fact_type)
            _enrich_context(conn, note_id, content, category, tags)
            _auto_semantic_backlinks(conn, note_id, content, db_path=str(db_path))
        _auto_fts_backlinks(conn, note_id, content)
        _index_adaptive_retention(conn, note_id, db_path=str(db_path))
        if defer_expensive:
            _defer_indexing_background_tasks(db_path, note_id, content, source_file,
                                             belief_status=belief_status,
                                             epistemic_source=epistemic_source,
                                             asserting_agent_id=asserting_agent_id,
                                             evidence_chain=evidence_chain,
                                             fact_type=fact_type,
                                             conn=conn)
    except Exception as e:
        if external_db:
            raise
        logger.error("Error in incremental update: %s", e)
    else:
        if not external_db:
            conn.commit()
            try:
                auto_backfill(db_path)
            except Exception as _e:
                logger.warning("Auto-backfill skipped: %s", _e)
    finally:
        if lock_file:
            release_flock(lock_file)
        if local_db is not None:
            try:
                local_db.close()
            except Exception as _close_exc:
                logger.debug("local_db.close() failed in _update_memory_index_incremental: %s", _close_exc)


def _defer_indexing_background_tasks(
    db_path: Path, note_id: str, content: str, source_file: str,
    belief_status: str = "active", epistemic_source: str = "agent",
    asserting_agent_id: str = "", evidence_chain: list | None = None,
    fact_type: str = "observation",
    conn=None,
) -> None:
    """Enqueue expensive indexing operations as background tasks.

    Called when ``defer_expensive=True`` in the save path.  The background
    worker processes these asynchronously so the MCP tool returns fast.
    """
    try:
        from background.background_queue import init_task_queue, enqueue_task
        from infra._lazy_imports import get_config

        cfg = get_config()
        max_qs = getattr(cfg, "background_max_queue_size", 500)
        reject_pol = getattr(cfg, "background_reject_policy", "reject_new")

        bq_conn = conn
        if bq_conn is None:
            from infra.db_write_queue import sqlite_write_queue
            bq_conn = sqlite_write_queue.start_session(db_path)
        init_task_queue(bq_conn)
        enqueue_task(
            bq_conn,
            "embedding_index",
            {"memory_id": note_id, "content": content, "source_file": source_file},
            max_queue_size=max_qs,
            reject_policy=reject_pol,
        )
        enqueue_task(
            bq_conn,
            "kg_and_fact_index",
            {"memory_id": note_id, "content": content,
             "belief_status": belief_status, "epistemic_source": epistemic_source,
             "asserting_agent_id": asserting_agent_id,
             "evidence_chain": evidence_chain, "fact_type": fact_type},
            max_queue_size=max_qs,
            reject_policy=reject_pol,
        )
        enqueue_task(
            bq_conn,
            "semantic_backlinks",
            {"memory_id": note_id, "content": content},
            max_queue_size=max_qs,
            reject_policy=reject_pol,
        )
        enqueue_task(
            bq_conn,
            "chunk_embedding_index",
            {"memory_id": note_id, "content": content},
            max_queue_size=max_qs,
            reject_policy=reject_pol,
        )
    except Exception as _e:
        logger.warning("save: background task enqueue failed: %s", _e)
    finally:
        if conn is None and bq_conn is not None:
            try:
                bq_conn.close()
            except Exception as _bq_close_exc:
                logger.debug("bq_conn.close() failed in _defer_indexing_background_tasks: %s", _bq_close_exc)


def _validate_save_params(content, category, title_slug, tags):
    if not isinstance(content, str):
        return _err(ErrorCode.INVALID_PARAMS, "content must be a string.")
    from infra._lazy_imports import get_config

    max_content = get_config().save_max_content_bytes
    if len(content) > max_content:
        return _err(
            ErrorCode.CONTENT_TOO_LARGE,
            f"Content too large ({len(content)} bytes). Maximum is {max_content // 1024}KB.",
        )
    if (
        not category
        or category.strip() in (".", "..")
        or "/" in category
        or ("\\" in category)
        or category.startswith("~")
    ):
        return _err(
            ErrorCode.INVALID_CATEGORY,
            "Invalid category. Must be a non-empty single segment like 'lessons' or 'decisions'.",
        )
    from infra._lazy_imports import get_config

    cfg = get_config()
    max_slug = cfg.save_max_slug_len
    max_category = cfg.save_max_category_len
    max_tags = cfg.save_max_tags

    if (
        not title_slug
        or "/" in title_slug
        or "\\" in title_slug
        or (len(title_slug) > max_slug)
    ):
        return _err(
            ErrorCode.INVALID_SLUG,
            f"Invalid title_slug. Must be a non-empty single segment up to {max_slug} chars (no slashes).",
        )
    if len(category) > max_category:
        return _err(
            ErrorCode.INVALID_CATEGORY,
            f"Invalid category. Must be up to {max_category} chars.",
        )
    if tags is not None and (not isinstance(tags, (str, list))):
        return _err(
            ErrorCode.INVALID_PARAMS, "tags must be a string or a list of strings."
        )
    if isinstance(tags, str):
        tags_list = [t.strip() for t in re.split("[,; ]+", tags) if t.strip()]
    elif isinstance(tags, list):
        tags_list = [str(t).strip() for t in tags if t]
    else:
        tags_list = []
    if len(tags_list) > max_tags:
        return _err(
            ErrorCode.INVALID_PARAMS,
            f"Too many tags ({len(tags_list)}). Maximum is {max_tags}.",
        )
    return tags_list, ", ".join(tags_list)


def _scan_for_injection_or_skip(
    content: str, category: str, title_slug: str
) -> Optional[str]:
    """Run the prompt-injection scan. Return an error string if the
    save should be rejected (high-risk content); return ``None`` if
    the save is allowed to proceed (clean content OR a scan failure).

    Extracted from save_memory() (2026-06-22) so the orchestrator
    stays readable. H9: runs on every save path since both the MCP
    tool and the auto-save hook delegate to save_memory.
    """
    try:
        from infra._lazy_imports import scan_for_injection

        inj = scan_for_injection(content)
        if inj["is_suspicious"] and inj["risk_score"] >= 0.5:
            logger.warning(
                "save_memory: rejecting injection-suspicious content for %s/%s "
                "(risk_score=%.2f, category=%s, matches=%s)",
                category,
                title_slug,
                inj["risk_score"],
                inj["category"],
                inj["matches"],
            )
            return _err(
                ErrorCode.INJECTION_DETECTED,
                f"Content rejected: injection risk score {inj['risk_score']:.2f} "
                f"(category: {inj['category']}). "
                f"If this is legitimate, rephrase the content to avoid instruction-like patterns.",
            )
        elif inj["is_suspicious"]:
            logger.info(
                "save_memory: low-risk injection patterns in %s/%s "
                "(risk_score=%.2f, matches=%s) — allowing save",
                category,
                title_slug,
                inj["risk_score"],
                inj["matches"],
            )
    except Exception as _ie:
        logger.debug("save_memory: injection scan failed (benign): %s", _ie)
    return None


def _acquire_db_connection(db_path_obj, category, title_slug, start_time, tenant_id: str = "default"):
    """Acquire a write-serialized SQLite connection via the write queue.
    Returns the connection on success, or a string error message if
    acquisition failed (the message is already audit-logged).

    Extracted from save_memory() (2026-06-22) so the orchestrator
    stays readable.
    """
    try:
        from infra.db_write_queue import sqlite_write_queue
        conn = sqlite_write_queue.start_session(db_path_obj)
        from infra.db_migrations import run_schema_setup
        run_schema_setup(conn)
        return conn
    except Exception as e:
        try:
            audit.enqueue_audit(
                db_path=str(db_path_obj),
                tool="memory_save",
                args={
                    "category": category,
                    "title_slug": title_slug,
                    "error": str(e)[:200],
                },
                results_count=0,
                top1_id=None,
                latency_ms=(time.time() - start_time) * 1000.0,
                error=str(e)[:500],
            )
        except Exception as audit_exc:
            logger.warning("audit.enqueue_audit failed after DB error: %s", audit_exc)
        return _err(ErrorCode.DB_ERROR, f"saving memory: {e}")


def _resolve_save_paths(category, title_slug, is_global, db_path):
    if db_path is not None:
        mem_dir = Path(db_path).parent
        project_root = mem_dir.parent
        global_mem = Path(GLOBAL_MEM_DIR)
    else:
        project_root, _, global_mem = get_memory_paths()
    if is_global:
        env_path = os.environ.get("MEMORY_DB_PATH")
        target_base = Path(env_path).parent if env_path else global_mem
    else:
        target_base = resolve_active_memory_dir(
            base_dir=Path(db_path).parent if db_path else None
        )
    if not target_base.exists():
        if is_global:
            return _err(
                ErrorCode.NOT_FOUND,
                f"Target memory directory {target_base} does not exist.",
            )
        has_project_marker = any(
            (
                (project_root / m).exists() or (project_root / m).is_dir()
                for m in (
                    ".git",
                    ".agents",
                    "AGENTS.md",
                    "CLAUDE.md",
                    "package.json",
                    "pyproject.toml",
                )
            )
        )
        if not has_project_marker and (not (project_root / "memory").exists()):
            return _err(
                ErrorCode.NOT_FOUND,
                f"No project context found at {project_root}. Run from inside a project (with .git, .agents, AGENTS.md, CLAUDE.md, package.json, or pyproject.toml) or pass is_global=True. To bootstrap a new project, run setup_memory.sh first.",
            )
        try:
            target_base.mkdir(parents=True, exist_ok=True)
            memory_md = target_base / "MEMORY.md"
            if not memory_md.exists():
                atomic_write(
                    memory_md,
                    "# Agentic Memory Index\n\n## Active Projects\n\n## Architecture Decisions (ADRs)\n\n## Hard-Won Lessons\n\n## User Preferences\n",
                    encoding="utf-8",
                )
        except Exception as e:
            return _err(
                ErrorCode.DB_ERROR,
                f"Target memory directory {target_base} does not exist and could not be created: {e}",
            )
    try:
        target_base_resolved = target_base.resolve()
        effective_category = category
        redirected = False
        if category == "lessons" and title_slug.startswith("audit-"):
            effective_category = "audits"
            redirected = True
        category_dir = (target_base_resolved / effective_category).resolve()
        if redirected:
            logger.warning(
                "Audit redirect: category=lessons + title_slug='%s' routed to dir 'audits' (effective_category=%s). "
                "The returned note_id remains backward-compatible (%s), "
                "but the on-disk path is memory/audits/%s.md",
                title_slug,
                effective_category,
                f"{category}/{title_slug}",
                title_slug,
            )
        # Note: is_relative_to returns True for self, so the second clause
        # below is the one that catches an empty/identity category. Split the
        # two conditions into distinct error messages for debuggability.
        if not category_dir.is_relative_to(target_base_resolved):
            return _err(
                ErrorCode.TRAVERSAL,
                f"Category path '{category}' escapes the target base directory.",
            )
        if category_dir == target_base_resolved:
            return _err(
                ErrorCode.TRAVERSAL,
                "Category resolves to the target base itself; an empty or "
                "identity category is not allowed.",
            )
        file_path = (category_dir / f"{title_slug}.md").resolve()
        if not file_path.is_relative_to(category_dir):
            return _err(
                ErrorCode.TRAVERSAL,
                f"Title slug '{title_slug}' escapes the category directory.",
            )
    except Exception as e:
        return _err(ErrorCode.INVALID_PARAMS, f"validating paths: {e}")
    category_dir.mkdir(parents=True, exist_ok=True)
    return target_base, file_path, category_dir, project_root, effective_category


def _build_memory_file(
    content, category, title_slug, tags_list, pinned, now_iso=None, note_id=None
):
    import datetime

    if now_iso is None:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tags_str = ", ".join(tags_list)
    pinned_str = "true" if pinned else "false"
    markdown_content = f"---\ncreated: {now_iso}\nupdated: {now_iso}\nobserved_at: {now_iso}\ntags: [{tags_str}]\npinned: {pinned_str}\nrelated: []\nvalid_from: {now_iso}\nvalid_to: null\nsuperseded_by: null\n\n# {title_slug.replace('-', ' ').title()}\n\n{content.strip()}\n"
    fm_metadata, _ = parse_frontmatter(content)
    raw_meta = fm_metadata.get("metadata")
    if isinstance(raw_meta, str) and raw_meta.startswith("{"):
        try:
            fm_metadata = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            pass
    elif raw_meta is not None and not isinstance(raw_meta, dict):
        fm_metadata = {"value": raw_meta}
    else:
        fm_metadata = fm_metadata if isinstance(fm_metadata, dict) else {}
    for k in (
        "category",
        "title_slug",
        "tags",
        "valid_from",
        "valid_to",
        "superseded_by",
        "pinned",
        "related",
        "created",
        "updated",
        "observed_at",
    ):
        fm_metadata.pop(k, None)
    try:
        metadata_json = json.dumps(fm_metadata) if fm_metadata else "{}"
    except (TypeError, ValueError) as exc:
        logger.warning(
            "save_memory: non-JSON-serializable metadata for %s/%s (%s): %s — defaulting to {}",
            category,
            title_slug,
            note_id or "unknown",
            exc,
        )
        metadata_json = "{}"
    return markdown_content, fm_metadata, now_iso, metadata_json


def _persist_to_db(
    conn,
    db_path_obj,
    category,
    title_slug,
    content,
    tags_list,
    pinned,
    now_iso,
    is_global,
    metadata_json,
    file_path,
    markdown_content,
    importance: int = 3,
    _conn_is_shared: bool = False,
    defer_expensive: bool = False,
    tenant_id: str = "default",
    epistemic_source: str = "agent",
    belief_status: str = "active",
    asserting_agent_id: str = "",
    evidence_chain: list | None = None,
    fact_type: str = "observation",
):
    try:
        _update_memory_index_incremental(
            db_path_obj,
            category,
            title_slug,
            content,
            tags_list,
            pinned,
            now_iso,
            is_global,
            metadata_json=metadata_json,
            db=conn,
            importance=importance,
            defer_expensive=defer_expensive,
            tenant_id=tenant_id,
            epistemic_source=epistemic_source,
            belief_status=belief_status,
            asserting_agent_id=asserting_agent_id,
            evidence_chain=evidence_chain,
            fact_type=fact_type,
        )
        try:
            atomic_write(file_path, markdown_content, encoding="utf-8")
        except Exception as file_err:
            if not _conn_is_shared:
                conn.rollback()
                safe_close_db(conn)
            logger.error("save_memory: file write failed after DB insert: %s", file_err)
            return _err(
                ErrorCode.DB_ERROR, f"file write failed after DB insert: {file_err}"
            )
        if not _conn_is_shared:
            conn.commit()
            safe_close_db(conn)
        return f"{category}/{title_slug}"
    except Exception as e:
        if not _conn_is_shared:
            try:
                conn.rollback()
            except Exception as _rb_exc:
                logger.debug("conn.rollback() failed in _persist_to_db: %s", _rb_exc)
            safe_close_db(conn)
        return _err(ErrorCode.DB_ERROR, f"saving memory: {e}")


# ---------------------------------------------------------------------------
# Saga persistence helpers (extracted 2026-06-22 from save_memory)
#
# The save-memory orchestrator used to contain a 112-line inline block
# that (1) tried the saga path, (2) decided whether to fall back based
# on MEMORY_SAGA_FALLBACK, and (3) called the legacy _persist_to_db on
# fallback. That block mixed three concerns — saga call wiring, the
# fallback policy decision, and the legacy persist — and made the
# orchestrator hard to read.
#
# The helpers below split those concerns apart. The orchestrator now
# reads as a single call to _persist_via_saga_or_fallback() plus a
# single call to _audit_save_failure() on the error path.
# ---------------------------------------------------------------------------


def _is_saga_enabled() -> bool:
    """Return True if the saga-wrapped save path should be used.

    Saga is enabled when (a) the saga module is importable, and (b) the
    config singleton reports ``saga_enabled=True``.  Any config-load
    failure is swallowed and treated as "saga enabled" — the saga path
    is the documented default since 2026-06-22 BLK-1, so a missing or
    broken config should not silently disable it.
    """
    if _saga_save_memory is None:
        return False
    if _get_config is None:
        return True
    try:
        return bool(_get_config().saga_enabled)
    except Exception:
        return True


def _try_saga_persist(
    *,
    conn,
    db_path_obj,
    category,
    title_slug,
    content,
    tags_list,
    pinned,
    now_iso,
    is_global,
    metadata_json,
    file_path,
    markdown_content,
    importance,
    lock_already_held: bool = False,
    defer_expensive: bool = False,
    tenant_id: str = "default",
    epistemic_source: str = "agent",
    belief_status: str = "active",
    asserting_agent_id: str = "",
    evidence_chain: list | None = None,
    fact_type: str = "observation",
):
    """Wrap the upsert + write + vec-key triple-store steps in a saga.

    Builds the three closure functions that ``_saga_save_memory``
    expects and forwards to it.      Returns the note_id on success.
    Propagates whatever exception the saga raises — the caller decides
    whether to fall back or to surface the error.

    ``lock_already_held`` (P0-2 fix, 2026-06-22): when True, the saga
    skips its internal file-lock acquisition.  See _persist_via_saga_or_fallback.
    """
    assert _saga_save_memory is not None, "caller must check _is_saga_enabled() first"

    def _do_upsert_db():
        _update_memory_index_incremental(
            db_path_obj,
            category,
            title_slug,
            content,
            tags_list,
            pinned,
            now_iso,
            is_global,
            metadata_json=metadata_json,
            db=conn,
            importance=importance,
            defer_expensive=defer_expensive,
            tenant_id=tenant_id,
            epistemic_source=epistemic_source,
            belief_status=belief_status,
            asserting_agent_id=asserting_agent_id,
            evidence_chain=evidence_chain,
            fact_type=fact_type,
        )

    def _do_write_vec_key():
        return _write_vec_key(conn, f"{category}/{title_slug}")

    def _do_write_file():
        atomic_write(file_path, markdown_content, encoding="utf-8")

    return _saga_save_memory(
        conn=conn,
        note_id=f"{category}/{title_slug}",
        file_path=file_path,
        markdown_content=markdown_content,
        db_path=db_path_obj,
        do_upsert_db=_do_upsert_db,
        do_write_vec_key=_do_write_vec_key,
        do_write_file=_do_write_file,
        lock_already_held=lock_already_held,
    )


def _apply_saga_fallback_policy(category, title_slug):
    """Decide what to do when the saga path raises.

    Saga-failure policy (C3 fix, 2026-06-22): the saga path is the
    crash-consistent default.  A failed saga raises ``RuntimeError``
    by default rather than silently dropping the write.  Operators can
    opt into the legacy best-effort fallback by setting
    ``MEMORY_SAGA_FALLBACK=allow``.

    On the default (raise) path, this function:
      1. Increments ``_saga_fallback_counter`` for telemetry, if available.
      2. Raises ``RuntimeError`` with a message that tells the operator
         how to switch to the legacy path.

    On the allow path, this function returns silently — the caller then
    routes through ``_persist_to_db``.
    """
    import os

    if os.environ.get("MEMORY_SAGA_FALLBACK", "raise") == "allow":
        return
    # S2 fix: counter for telemetry.  Even when fallback is allowed,
    # the operator should know it happened.
    try:
        from infra.saga import _saga_fallback_counter

        _saga_fallback_counter.inc()
    except Exception as _ctr_exc:
        logger.debug("_saga_fallback_counter.inc() failed (benign): %s", _ctr_exc)
    raise RuntimeError(
        f"saga_save_memory failed for {category}/{title_slug}; "
        f"refusing to fall back silently.  Set "
        f"MEMORY_SAGA_FALLBACK=allow to opt into the legacy "
        f"non-saga path.  Original error: see logs."
    ) from None


def _persist_via_saga_or_fallback(
    *,
    conn,
    db_path_obj,
    category,
    title_slug,
    content,
    tags_list,
    pinned,
    now_iso,
    is_global,
    metadata_json,
    file_path,
    markdown_content,
    importance,
    lock_already_held: bool = False,
    _conn_is_shared: bool = False,
    defer_expensive: bool = False,
    note_id: str = "",
    tenant_id: str = "default",
    epistemic_source: str = "agent",
    belief_status: str = "active",
    asserting_agent_id: str = "",
    evidence_chain: list | None = None,
    fact_type: str = "observation",
):
    """Persist a memory via the saga path, with policy-driven fallback.

    Tries the saga-wrapped path first.  If the saga raises, the failure
    is logged and the policy in ``_apply_saga_fallback_policy`` decides
    whether to fall back to ``_persist_to_db`` (legacy) or to raise.

    Returns a ``(note_id, conn_after)`` tuple:

    * ``note_id`` is the canonical note id on success, or an ``_err``
      envelope string on legacy-path failure.
    * ``conn_after`` is the connection the caller should pass into
      ``_run_post_save_hooks`` — ``None`` when the saga path committed
      (the saga owns commit/close) or the legacy ``_persist_to_db``
      closed the connection (B7: post-hooks re-acquire if they need
      the conn).

    ``lock_already_held`` (P0-2 fix, 2026-06-22): when True, the saga
    skips its internal file-lock acquisition.  save_memory acquires the
    file lock before the conn and passes True so the saga doesn't try
    to double-acquire the same flock (which would block forever on a
    different fd for the same file).
    """
    saga_ok = False
    if _is_saga_enabled():
        try:
            note_id = _try_saga_persist(
                conn=conn,
                db_path_obj=db_path_obj,
                category=category,
                title_slug=title_slug,
                content=content,
                tags_list=tags_list,
                pinned=pinned,
                now_iso=now_iso,
                is_global=is_global,
                metadata_json=metadata_json,
                file_path=file_path,
                markdown_content=markdown_content,
                importance=importance,
                lock_already_held=lock_already_held,
                defer_expensive=defer_expensive,
                tenant_id=tenant_id,
                epistemic_source=epistemic_source,
                belief_status=belief_status,
                asserting_agent_id=asserting_agent_id,
                evidence_chain=evidence_chain,
                fact_type=fact_type,
            )
            saga_ok = True
        except Exception as saga_exc:
            logger.warning(
                "saga_save_memory failed for %s/%s, considering fallback: %s",
                category,
                title_slug,
                saga_exc,
            )

    if not saga_ok:
        # Saga raised or was disabled.  Decide whether to fall back.
        _apply_saga_fallback_policy(category, title_slug)
        note_id = _persist_to_db(
            conn,
            db_path_obj,
            category,
            title_slug,
            content,
            tags_list,
            pinned,
            now_iso,
            is_global,
            metadata_json,
            file_path,
            markdown_content,
            importance,
            _conn_is_shared=_conn_is_shared,
            defer_expensive=defer_expensive,
            tenant_id=tenant_id,
            epistemic_source=epistemic_source,
            belief_status=belief_status,
            asserting_agent_id=asserting_agent_id,
            evidence_chain=evidence_chain,
            fact_type=fact_type,
        )
        if not _conn_is_shared:
            conn = (
                None  # B7: _persist_to_db closed conn; post-hooks re-acquire if needed
            )
    return note_id, conn


def _audit_save_failure(db_path_obj, note_id, category, title_slug, _start_time):
    """Best-effort audit log of a save that returned an ``_err`` envelope.

    Called when ``save_memory`` produced a failure envelope (validation
    error, DB error, etc.) so the failure shows up in the audit table
    with the right ``tool="memory_save"`` row.  All exceptions are
    swallowed — this is the very last thing the save path does, and
    audit failures must not become save failures.
    """
    try:
        audit.enqueue_audit(
            db_path=str(db_path_obj),
            tool="memory_save",
            args={
                "category": category,
                "title_slug": title_slug,
                "error": note_id[:200] if isinstance(note_id, str) else "",
            },
            results_count=0,
            top1_id=None,
            latency_ms=(time.time() - _start_time) * 1000.0,
            error=note_id[:500] if isinstance(note_id, str) else "",
        )
    except Exception as _capture_exc:
        logger.debug("_capture_pre_state_main audit enqueue failed (benign): %s", _capture_exc)


def save_memory(
    content: str | SaveRequest,
    category: str | None = None,
    title_slug: str | None = None,
    tags: Optional[list] = None,
    pinned: bool = False,
    is_global: bool = False,
    safety_wiring: bool = True,
    db_path: str | None = None,
    _now_iso: str | None = None,
    importance: int = 3,
    _conn=None,
    note_id: str = "",
    context: str = "generic",
    defer_expensive: bool = False,
    tenant_id: str = "default",
    epistemic_source: str = "agent",
    belief_status: str = "active",
    asserting_agent_id: str = "",
    evidence_chain: list | None = None,
    fact_type: str = "observation",
):
    """Write a memory note to disk and update the FTS5 index incrementally.

    Two calling conventions:

    1. **New (preferred):** ``save_memory(SaveRequest(...))``
    2. **Legacy (backward compat):** ``save_memory(content, category, title_slug, ...)``

    Internal params ``_now_iso`` and ``_conn`` are accepted in both forms.

    Returns:
        The canonical note_id string on success, or an _err envelope
        string on failure.
    """
    if isinstance(content, SaveRequest):
        return _save_memory_core(content, _now_iso=_now_iso, _conn=_conn)
    req = SaveRequest(
        content=content,
        category=category or "",
        title_slug=title_slug or "",
        tags=tags,
        pinned=pinned,
        is_global=is_global,
        safety_wiring=safety_wiring,
        db_path=db_path,
        importance=importance,
        note_id=note_id,
        context=context,
        defer_expensive=defer_expensive,
        tenant_id=tenant_id,
        epistemic_source=epistemic_source,
        belief_status=belief_status,
        asserting_agent_id=asserting_agent_id,
        evidence_chain=evidence_chain,
        fact_type=fact_type,
    )
    return _save_memory_core(req, _now_iso=_now_iso, _conn=_conn)


def _project_sql_to_crdt(db_path_obj: Path, note_id: str, conn=None) -> None:
    """Best-effort: project the committed SQL row into memory_field_crdt."""
    try:
        from crdt.crdt_field import project_sql_to_crdt as _proj
        from infra.db import open_db

        if conn is not None:
            _proj(conn, note_id, _crdt_agent_id())
        else:
            with open_db(db_path_obj, timeout=5.0) as c:
                _proj(c, note_id, _crdt_agent_id())
    except Exception as _e:
        logger.debug("save_memory: CRDT SQL projection skipped: %s", _e)


def _save_memory_core(
    req: SaveRequest,
    _now_iso: str | None = None,
    _conn=None,
):
    """Internal implementation of save_memory."""
    content = req.content
    category = req.category
    title_slug = req.title_slug
    tags = req.tags
    pinned = req.pinned
    is_global = req.is_global
    db_path = req.db_path
    importance = req.importance
    note_id = req.note_id
    context = req.context
    defer_expensive = req.defer_expensive
    safety_wiring = req.safety_wiring
    tenant_id = req.tenant_id
    epistemic_source = req.epistemic_source
    belief_status = req.belief_status
    asserting_agent_id = req.asserting_agent_id
    evidence_chain = req.evidence_chain
    fact_type = req.fact_type

    from infra.db import _local_state

    _local_state.in_save_pipeline = True
    try:
        _start_time = time.time()
        result = _validate_save_params(content, category, title_slug, tags)
        if isinstance(result, str):
            return result
        tags_list, _tags_str = result
        if note_id and "/" in note_id:
            _derived_cat, _derived_slug = note_id.split("/", 1)
            if not category:
                category = _derived_cat
            if not title_slug:
                title_slug = _derived_slug
        from infra.memory_common import _resolve_tags

        tags_list = _resolve_tags(category, tags_list, context=context)
        # H9: Prompt-injection scan — pure regex, no side effects, runs on every
        # path (MCP tool + hook) since both delegate to save_memory.
        inj_result = _scan_for_injection_or_skip(content, category, title_slug)
        if inj_result is not None:
            return inj_result
        result = _resolve_save_paths(category, title_slug, is_global, db_path)
        if isinstance(result, str):
            return result
        target_base, file_path, _category_dir, _project_root, effective_category = result
        original_category = category
        if effective_category != category:
            category = effective_category
        _markdown, _fm_meta, now_iso, metadata_json = _build_memory_file(
            content,
            category,
            title_slug,
            tags_list,
            pinned,
            now_iso=_now_iso,
            note_id=note_id,
        )
        db_path_obj = (
            Path(db_path) if db_path is not None else target_base / "memory.db"
        )
        # Scenario 5 fix (2026-06-22): invalidate the per-db-path pragma
        # cache on every save.  Without this, an in-flight save that
        # started before a migration could write to a column the
        # migration had just added, using the stale column list.  The
        # cache lookup is cheap (one dict-pop), and the next call to
        # ``_detect_schema_features`` re-queries ``PRAGMA table_info``
        # to refresh.
        with _pragma_cache_lock:
            _pragma_cache.pop(str(db_path_obj), None)
        # P0-2 fix (2026-06-22): acquire the file lock BEFORE the conn so
        # the save_memory path matches the incremental path's lock order
        # (file lock -> DB conn, in _update_memory_index_incremental).  The
        # previous order (conn -> file lock) was a lock-order inversion that
        # could deadlock if the conn ever became process-wide.  We pass
        # ``lock_already_held=True`` to the saga so it skips its internal
        # file-lock acquisition — double-acquiring the same flock from the
        # same process on a different fd would block forever.
        #
        # P0-5 fix (2026-06-22): _acquire_lock now re-raises FileLockError
        # (per its strict=True contract).  We catch it here and proceed
        # without the lock so a contended lock doesn't fail the save —
        # the pool's per-thread conn still gives intra-process isolation,
        # and intra-process callers using the same path from different
        # threads are already serialized via the pool's per-thread keys.
        from infra._lazy_imports import FileLockError

        try:
            lock_file = _acquire_lock(db_path_obj)
        except FileLockError as _fle:
            logger.warning(
                "save_memory: write lock contended for %s, proceeding without it: %s",
                db_path_obj,
                _fle,
            )
            lock_file = None
        if _conn is not None:
            conn = _conn
        else:
            conn = _acquire_db_connection(
                db_path_obj, category, title_slug, _start_time, tenant_id=tenant_id
            )
            if isinstance(conn, str):
                if lock_file is not None:
                    try:
                        release_flock(lock_file)
                    except Exception as _rfl_err:
                        logger.debug(
                            "save_memory: release_flock in early return: %s", _rfl_err
                        )
                return conn
        # P0-1 fix (2026-06-22): the previous version of this function never
        # called safe_close_db on the saga-path conn, so the pool's depth
        # counter grew unbounded and the pool eventually exhausted in a
        # long-running daemon.  The try/finally below ensures the conn is
        # always returned to the pool — saga path returns the same conn
        # (caller owns close), legacy fallback path returns None
        # (_persist_to_db already closed it; close is a no-op in that case).
        # P0-3 fix (2026-06-22): track exception state so we don't commit
        # a transaction after an error — safe_close_db(should_commit=False)
        # will rollback instead.
        _save_errored = False
        deferred_writes = []
        try:
            # Persist via the saga path.  _persist_via_saga_or_fallback() handles
            # both the saga call and the MEMORY_SAGA_FALLBACK policy; on the
            # default policy, a saga failure raises (C3 fix 2026-06-22) and we
            # never silently lose a write.  The auto-save hook
            # (auto_save._upsert_memory) catches the raise, logs it, and
            # returns False so the Claude Code tool-complete event continues
            # normally — the two surfaces are intentionally decoupled.
            note_id, conn = _persist_via_saga_or_fallback(
                conn=conn,
                db_path_obj=db_path_obj,
                category=category,
                title_slug=title_slug,
                content=content,
                tags_list=tags_list,
                pinned=pinned,
                now_iso=now_iso,
                is_global=is_global,
                metadata_json=metadata_json,
                file_path=file_path,
                markdown_content=_markdown,
                importance=importance,
                lock_already_held=(lock_file is not None),
                _conn_is_shared=(_conn is not None),
                defer_expensive=defer_expensive,
                epistemic_source=epistemic_source,
                belief_status=belief_status,
                asserting_agent_id=asserting_agent_id,
                evidence_chain=evidence_chain,
                fact_type=fact_type,
            )

            if isinstance(note_id, str) and not note_id.startswith("Error ["):
                deferred_writes = _run_post_save_hooks(
                    target_base,
                    db_path_obj,
                    note_id,
                    category,
                    title_slug,
                    content,
                    tags,
                    pinned,
                    is_global,
                    (safety_wiring and not defer_expensive),
                    _start_time,
                    conn=conn,
                )
                _enqueue_background_tasks(db_path_obj, note_id, conn=conn)
                if _is_crdt_enabled():
                    _project_sql_to_crdt(db_path_obj, note_id, conn=conn)
            else:
                _audit_save_failure(
                    db_path_obj, note_id, category, title_slug, _start_time
                )
            if (
                original_category == "lessons"
                and title_slug.startswith("audit-")
                and isinstance(note_id, str)
                and not note_id.startswith("Error [")
                and note_id.startswith("audits/")
            ):
                note_id = f"lessons/{title_slug}"
            return note_id
        except Exception:
            _save_errored = True
            raise
        finally:
            if lock_file is not None:
                try:
                    release_flock(lock_file)
                except Exception as _rfl_err2:
                    logger.debug(
                        "save_memory: release_flock in finally failed: %s", _rfl_err2
                    )
            if conn is not None and _conn is None:
                try:
                    conn.close()
                    if not _save_errored and deferred_writes:
                        from infra.memory_common import safe_atomic_write
                        for filepath, filecontent in deferred_writes:
                            try:
                                safe_atomic_write(Path(filepath), filecontent, encoding="utf-8")
                            except Exception as _we:
                                logger.warning("Failed to run deferred file write for %s: %s", filepath, _we)
                except Exception as _close_err:
                    logger.debug(
                        "save_memory: safe_close_db in finally failed: %s", _close_err
                    )
            elif not _save_errored and deferred_writes:
                from infra.memory_common import safe_atomic_write
                for filepath, filecontent in deferred_writes:
                    try:
                        safe_atomic_write(Path(filepath), filecontent, encoding="utf-8")
                    except Exception as _we:
                        logger.warning("Failed to run deferred file write for %s: %s", filepath, _we)
    finally:
        _local_state.in_save_pipeline = False


def _record_revision_log(
    db: AnyConnection,
    memory_id: str,
    revision_type: str,
    rationale: str = "",
    old_content: str | None = None,
    new_content: str | None = None,
    metadata_json: str | None = None,
    agent_id: str = "",
) -> int | None:
    """Write an entry to the memory_revision_log table.

    Returns the row id of the new entry, or None on failure.
    """
    try:
        now = time.time()
        cur = db.execute(
            "INSERT INTO memory_revision_log "
            "(memory_id, revision_type, old_content, new_content, rationale, metadata, agent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, revision_type, old_content, new_content, rationale, metadata_json, agent_id, now),
        )
        return int(cur.lastrowid) if cur.lastrowid is not None else None
    except Exception as e:
        logger.warning("Failed to record revision log for %s/%s: %s", memory_id, revision_type, e)
        return None


def memory_supersede_db(
    db_path: Path, old_id: str, new_id: str, valid_to: Optional[str] = None,
    rationale: str = "",
) -> tuple[bool, Optional[str]]:
    """Mark a memory as superseded by another memory.

    Sprint 2: accepts optional ``rationale`` and records the supersession
    in ``memory_revision_log``.

    Extracted from mcp_memory.py (2026-06-21) to keep all save-path
    DB operations co-located in save_pipeline.

    Returns:
        (True, None) on success.
        (False, error_msg) on error. The error message contains:
          - "temporal columns" if schema is missing valid_from/valid_to/superseded_by
          - "not found" if either old_id or new_id is missing
          - other error string for unexpected failures
    """
    if valid_to is None:
        valid_to = datetime.now(timezone.utc).isoformat()
    if not db_path.exists():
        return (False, f"memory.db not found at {db_path}")
    db = None
    try:
        from infra.db import open_db

        with open_db(db_path, timeout=30.0) as db:
            features = _detect_schema_features(db_path, conn=db)
            if not features["has_temporal"]:
                return (False, "memory schema does not have temporal columns")
            old_row = db.execute(
                "SELECT id FROM memories WHERE id = ?", (old_id,)
            ).fetchone()
            if not old_row:
                return (False, f"old_id '{old_id}' not found")
            new_row = db.execute(
                "SELECT id FROM memories WHERE id = ?", (new_id,)
            ).fetchone()
            if not new_row:
                return (False, f"new_id '{new_id}' not found")
            old_content = db.execute(
                "SELECT content FROM memories WHERE id = ?", (old_id,)
            ).fetchone()
            db.execute(
                """UPDATE memories
                   SET valid_to = ?, superseded_by = ?, updated_at = ?
                   WHERE id = ?""",
                (valid_to, new_id, datetime.now(timezone.utc).isoformat(), old_id),
            )
            _record_revision_log(
                db, old_id, "supersede", rationale=rationale,
                old_content=old_content[0] if old_content else None,
                metadata_json=json.dumps({"superseded_by": new_id}),
            )
            # Store rationale in metadata if provided
            if rationale:
                try:
                    meta_row = db.execute(
                        "SELECT metadata FROM memories WHERE id = ?", (old_id,)
                    ).fetchone()
                    if meta_row and meta_row[0]:
                        meta = json.loads(meta_row[0])
                    else:
                        meta = {}
                    meta["supersession_rationale"] = rationale
                    db.execute(
                        "UPDATE memories SET metadata = ? WHERE id = ?",
                        (json.dumps(meta), old_id),
                    )
                except Exception as _supersede_meta_exc:
                    logger.debug("supersession metadata update failed for %s: %s", old_id, _supersede_meta_exc)
            return (True, None)
    except Exception as e:
        return (False, str(e))


def reinforce_memories_db(db_path: Path, ids: list[str], delta: float) -> int:
    """Apply +delta/-delta to success_score for each memory, then recalc fitness.

    Extracted from mcp_memory.py (2026-06-21). success_score is clamped
    to [-3.0, 5.0] so a single reinforce call cannot drive the score
    out of the documented scale.

    Returns:
        Number of memory rows actually updated (skips unknown ids).
    """
    if not ids:
        return 0
    if not db_path.exists():
        return 0
    hits = 0
    try:
        from infra.db import open_db

        with open_db(db_path, timeout=30.0) as db:
            for mid in ids:
                row = db.execute(
                    "SELECT success_score FROM memories WHERE id=?", (mid,)
                ).fetchone()
                if row:
                    old_score = row[0] or 0.0
                    new_score = max(-3.0, min(5.0, old_score + delta))
                    db.execute(
                        "UPDATE memories SET success_score = ? WHERE id = ?",
                        (new_score, mid),
                    )
                    hits += 1
    except Exception as e:
        logger.error("reinforce_memories_db: %s", e)
    # Recalculate fitness scores for the touched ids
    if hits:
        try:
            _recalculate_fitness_scores(db_path, ids)
        except Exception as e:
            logger.error("reinforce_memories_db: fitness recalc failed: %s", e)
    return hits


def patch_memory(
    db_path: Path,
    note_id: str,
    additions: list[str] | None = None,
    deletions: list[str] | None = None,
    rationale: str = "",
) -> str:
    """In-place memory amendment: apply additions/deletions to an existing note.

    - ``deletions``: text segments to remove (matched by content, anywhere).
    - ``additions``: text segments to append at the end of the body.
    - ``rationale``: recorded in ``memories.metadata.patch_history`` as JSON
      and in the ``memory_revision_log`` table.

    Returns the updated note_id on success, or an error string otherwise.
    """
    if not additions and not deletions:
        return _err(ErrorCode.INVALID_PARAMS, "At least one of additions or deletions is required")
    try:
        from infra.db import open_db
        from infra.memory_common import safe_atomic_write

        with open_db(db_path, timeout=30.0) as db:
            row = db.execute(
                "SELECT content, metadata, source_file FROM memories WHERE id = ? AND deleted_at IS NULL",
                (note_id,),
            ).fetchone()
            if row is None:
                return _err(ErrorCode.NOT_FOUND, f"note '{note_id}' not found or deleted")
            content, metadata_json, source_file = row
            source_path = db_path.parent / source_file if source_file else None

            # Parse frontmatter boundary: split on ---
            body = content
            frontmatter = ""
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = parts[0] + "---" + parts[1] + "---"
                    body = parts[2]

            # Apply deletions: remove each segment from body
            deletions = deletions or []
            for seg in deletions:
                if seg in body:
                    body = body.replace(seg, "", 1)
                else:
                    return _err(
                        ErrorCode.INVALID_PARAMS,
                        f"deletion text not found in content: {seg[:80]}",
                    )

            # Append additions
            additions = additions or []
            if additions:
                body = body.rstrip() + "\n\n" + "\n\n".join(additions) + "\n"

            new_content = frontmatter + body

            # Update the DB row with new content
            now_iso = datetime.now(timezone.utc).isoformat()
            db.execute(
                "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                (new_content, now_iso, note_id),
            )

            # Update the .md file if it exists
            if source_path and source_path.exists():
                safe_atomic_write(source_path, new_content, encoding="utf-8")

            # Append to metadata.patch_history
            meta = {}
            if metadata_json:
                try:
                    meta = json.loads(metadata_json)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            patch_entry = {
                "timestamp": time.time(),
                "additions": additions,
                "deletions": deletions,
                "rationale": rationale,
            }
            patch_history = meta.get("patch_history", [])
            if not isinstance(patch_history, list):
                patch_history = []
            patch_history.append(patch_entry)
            meta["patch_history"] = patch_history
            db.execute(
                "UPDATE memories SET metadata = ? WHERE id = ?",
                (json.dumps(meta), note_id),
            )

            # Record in revision log
            _record_revision_log(
                db, note_id, "amend", rationale=rationale,
                old_content=content, new_content=new_content,
                metadata_json=json.dumps({"patch_entry": patch_entry}),
            )

    except Exception as e:
        logger.exception("patch_memory failed for %s", note_id)
        return _err(ErrorCode.DB_ERROR, f"patch_memory: {e}")

    return note_id


def revert_supersede(
    db_path: Path,
    note_id: str,
    target_note_id: str | None = None,
    rationale: str = "",
) -> str:
    """Reverse a prior supersession on ``note_id``.

    Sets ``valid_to = NULL`` and ``superseded_by = NULL`` so the note is
    no longer considered superseded.  If ``target_note_id`` is provided,
    the function verifies that the supersession record matches before
    reverting.

    Returns the note_id on success, or an error string otherwise.
    """
    try:
        from infra.db import open_db

        with open_db(db_path, timeout=30.0) as db:
            row = db.execute(
                "SELECT valid_to, superseded_by FROM memories WHERE id = ? AND deleted_at IS NULL",
                (note_id,),
            ).fetchone()
            if row is None:
                return _err(ErrorCode.NOT_FOUND, f"note '{note_id}' not found or deleted")
            valid_to, superseded_by = row
            if superseded_by is None:
                return _err(ErrorCode.INVALID_PARAMS, f"note '{note_id}' is not superseded")
            if target_note_id is not None and superseded_by != target_note_id:
                return _err(
                    ErrorCode.INVALID_PARAMS,
                    f"note '{note_id}' is superseded by '{superseded_by}', not '{target_note_id}'",
                )

            db.execute(
                "UPDATE memories SET valid_to = NULL, superseded_by = NULL, updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), note_id),
            )

            _record_revision_log(
                db, note_id, "revert", rationale=rationale,
                metadata_json=json.dumps({
                    "previous_valid_to": valid_to,
                    "previous_superseded_by": superseded_by,
                }),
            )

    except Exception as e:
        logger.exception("revert_supersede failed for %s", note_id)
        return _err(ErrorCode.DB_ERROR, f"revert_supersede: {e}")

    return note_id
