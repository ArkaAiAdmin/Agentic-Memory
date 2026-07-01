"""Adaptive Retention for agentic-memory.

Auto-tunes decay half-life based on access patterns.
Notes with high access rates get longer retention; low-access notes decay faster.

Opt-in via MEMORY_ADAPTIVE_RETENTION=1.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import Counter
from typing import Optional

from memory_common import safe_close_db, connection_pool

__all__ = [
    "ADAPTIVE_RETENTION_ENABLED",  # noqa: F822 — dynamically resolved via __getattr__
    "ensure_adaptive_schema",
    "compute_adaptive_halflife",
    "batch_update_retention",
    "retention_stats",
    "invalidate_audit_hits_cache",
    "get_last_access_query",
    "get_last_access_queries_batch",
]

# ADAPTIVE_RETENTION_ENABLED is dynamically resolved via __getattr__

_DEFAULT_HALF_LIFE_DAYS = 180
_MIN_HALF_LIFE_DAYS = 30
_MAX_HALF_LIFE_DAYS = 730  # 2 years
_ACCESS_BOOST_FACTOR = 0.75  # each access event extends half-life by this factor
_MAX_BOOST_MULTIPLIER = 4.0  # max multiplier on base half-life

# Module-level cache for audit_log → note_id hit counts.
# Keyed by db_path so multiple DBs are tracked independently.
# Bypasses the O(N×M) re-scan of audit_log when compute_adaptive_halflife
# is called once per note (e.g. inside a per-note loop).
# Callers that mutate the audit_log (e.g. crons) should call
# invalidate_audit_hits_cache() to drop the stale entry.
_audit_hits_cache_by_db: dict[str, dict[str, int]] = {}
_audit_hits_cache_lock = threading.Lock()


def _build_audit_hits_index(conn: sqlite3.Connection) -> dict[str, int]:
    """Scan audit_log once and return a {note_id: hit_count} map.

    Catches sqlite3.OperationalError (table doesn't exist) and returns {}.
    """
    try:
        rows = conn.execute(
            "SELECT result_preview FROM audit_log WHERE tool_name = 'memory_search'"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    hits: dict[str, int] = {}
    for row in rows:
        try:
            preview = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(preview, dict):
            for k in preview:
                if isinstance(k, str):
                    hits[k] = hits.get(k, 0) + 1
        elif isinstance(preview, str) and len(preview) < 200:
            hits[preview] = hits.get(preview, 0) + 1
    return hits


def invalidate_audit_hits_cache(db_path: str | None = None) -> None:
    """Drop the audit_hits cache. If db_path is given, drop only that entry.

    Call this after writing to audit_log to ensure the next
    compute_adaptive_halflife call sees fresh data.
    """
    with _audit_hits_cache_lock:
        if db_path is None:
            _audit_hits_cache_by_db.clear()
        else:
            _audit_hits_cache_by_db.pop(str(db_path), None)


def ensure_adaptive_schema(conn: sqlite3.Connection) -> None:
    """Create user_access_log table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_access_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id    TEXT    NOT NULL,
            access_ts  REAL    NOT NULL,
            source     TEXT    NOT NULL DEFAULT 'unknown',
            FOREIGN KEY (note_id) REFERENCES memories(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_access_note ON user_access_log(note_id)"
    )
    # TTL cleanup: drop rows older than 90 days to prevent unbounded growth
    try:
        import time

        cutoff = time.time() - (90 * 86400)
        conn.execute("DELETE FROM user_access_log WHERE access_ts < ?", (cutoff,))
    except sqlite3.OperationalError:
        pass


def record_access(
    conn: sqlite3.Connection, note_id: str, source: str = "search"
) -> None:
    """Record a user access event for a note.

    Args:
        conn: database connection
        note_id: the note that was accessed
        source: how it was accessed ('search', 'read', 'list', etc.)
    """
    import sys

    if not sys.modules[__name__].ADAPTIVE_RETENTION_ENABLED:
        return
    try:
        # Live schema column is `access_ts` (not `accessed_at` as
        # originally assumed in the 2026-06-14 audit). The earlier
        # "fix" flow unintentionally inverted this and reintroduced
        # the silent-swallowed-OperationalError class. Reverted to
        # access_ts which is the actual live column.
        conn.execute(
            "INSERT INTO user_access_log (note_id, access_ts, source) VALUES (?, ?, ?)",
            (note_id, time.time(), source),
        )
        # Do NOT commit here — let the caller manage the transaction.
        # Explicit commits break the Saga coordinator's rollback semantics.
    except sqlite3.OperationalError:
        pass  # table may not exist yet


def compute_adaptive_halflife(
    note_id: str,
    base_halflife: float = _DEFAULT_HALF_LIFE_DAYS,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
    audit_hits: int | None = None,
) -> float:
    """Compute adaptive half-life for a note based on its access history.

    High access count → longer half-life (slower decay).
    Low access count → shorter half-life (faster decay).

    Args:
        note_id: the note to evaluate
        base_halflife: default half-life in days
        db_path: optional path to memory.db

    Returns:
        adapted half-life in days
    """
    import sys

    if not sys.modules[__name__].ADAPTIVE_RETENTION_ENABLED:
        return base_halflife

    try:
        from _lazy_imports import get_memory_paths

        _, local_mem, global_mem = get_memory_paths()
        actual_db = db_path or str(local_mem / "memory.db")
        if not actual_db:
            return base_halflife
    except ImportError:
        return base_halflife

    try:
        should_close = conn is None
        if conn is None:
            conn = connection_pool.get(actual_db)
        try:
            # Count access events for this note
            access_count = 0
            try:
                access_count = conn.execute(
                    "SELECT COUNT(*) FROM user_access_log WHERE note_id = ?", (note_id,)
                ).fetchone()[0]
            except sqlite3.OperationalError:
                pass  # table doesn't exist yet

            # Count search hits (via audit log) — use pre-computed value if available
            if audit_hits is not None:
                access_count += audit_hits
            else:
                # Use module-level cache to avoid O(N×M) audit_log re-scan when
                # called once per note. Cache is per-db_path and invalidated on
                # writes via invalidate_audit_hits_cache().
                cache_key = actual_db
                with _audit_hits_cache_lock:
                    cached_hits_map = _audit_hits_cache_by_db.get(cache_key)
                    if cached_hits_map is None:
                        cached_hits_map = _build_audit_hits_index(conn)
                        _audit_hits_cache_by_db[cache_key] = cached_hits_map
                access_count += cached_hits_map.get(note_id, 0)
        finally:
            if should_close:
                safe_close_db(conn)

        # Compute multiplier: each access boosts half-life
        multiplier = min(
            1.0 + (access_count * _ACCESS_BOOST_FACTOR), _MAX_BOOST_MULTIPLIER
        )
        adapted = base_halflife * multiplier
        return max(_MIN_HALF_LIFE_DAYS, min(_MAX_HALF_LIFE_DAYS, adapted))
    except Exception:
        return base_halflife


def batch_update_retention(
    base_halflife: float = _DEFAULT_HALF_LIFE_DAYS,
    dry_run: bool = False,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Batch compute and optionally store adaptive half-lives for all notes.

    Stores the adapted half-life in each note's metadata under "adaptive_halflife_days".

    Args:
        base_halflife: default half-life in days
        dry_run: if True, don't write anything
        db_path: optional explicit path to the database. If not provided,
                 resolves via get_memory_paths() (CWD-dependent).
        conn: optional active Connection/ProxyConnection to use.
              If provided, prevents opening a new session.

    Returns:
        dict with stats
    """
    import sys

    if not sys.modules[__name__].ADAPTIVE_RETENTION_ENABLED:
        return {"enabled": False}

    try:
        if db_path is None:
            from memory_common import get_memory_paths

            _, local_mem, global_mem = get_memory_paths()
            actual_db = str(local_mem / "memory.db")
        else:
            actual_db = str(db_path)
        if not actual_db:
            return {"enabled": True, "error": "db_path not found"}
    except ImportError:
        return {"enabled": True, "error": "memory_common not found"}

    conn_to_use = None
    should_close = False
    try:
        if conn is None:
            from db_write_queue import sqlite_write_queue
            conn_to_use = sqlite_write_queue.start_session(Path(actual_db))
            should_close = True
        else:
            conn_to_use = conn
            should_close = False

        ensure_adaptive_schema(conn_to_use)
        try:
            rows = conn_to_use.execute(
                "SELECT id, metadata FROM memories WHERE deleted_at IS NULL"
            ).fetchall()

            # Pre-compute audit log search hits once (O(M) instead of O(N*M))
            # and stash in the module-level cache so compute_adaptive_halflife
            # can reuse it without re-scanning.
            _audit_hits_cache = _build_audit_hits_index(conn_to_use)
            with _audit_hits_cache_lock:
                _audit_hits_cache_by_db[actual_db] = _audit_hits_cache

            updated = 0
            halflife_dist: Counter[str] = Counter()

            for note_id, meta_json in rows:
                halflife = compute_adaptive_halflife(
                    note_id,
                    base_halflife,
                    actual_db,
                    conn=conn_to_use,
                    audit_hits=_audit_hits_cache.get(note_id, 0),
                )

                # Bucket for distribution
                if halflife <= 60:
                    halflife_dist["30-60d"] += 1
                elif halflife <= 180:
                    halflife_dist["61-180d"] += 1
                elif halflife <= 365:
                    halflife_dist["181-365d"] += 1
                else:
                    halflife_dist["365d+"] += 1

                if not dry_run:
                    try:
                        meta = json.loads(meta_json or "{}")
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                    meta["adaptive_halflife_days"] = round(halflife, 1)
                    meta["base_halflife_days"] = base_halflife
                    conn_to_use.execute(
                        "UPDATE memories SET metadata = ? WHERE id = ?",
                        (json.dumps(meta), note_id),
                    )
                    updated += 1

            if not dry_run:
                conn_to_use.commit()
        finally:
            if should_close and conn_to_use:
                try:
                    conn_to_use.close()
                except Exception:
                    pass
        return {
            "enabled": True,
            "total_notes": len(rows),
            "updated": updated,
            "halflife_distribution": dict(halflife_dist),
            "dry_run": dry_run,
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)}


def retention_stats(db_path: str | None = None) -> dict:
    """Return retention statistics."""
    import sys

    if not sys.modules[__name__].ADAPTIVE_RETENTION_ENABLED:
        return {"enabled": False}

    try:
        from _lazy_imports import get_memory_paths

        _, local_mem, global_mem = get_memory_paths()
        actual_db = db_path or str(local_mem / "memory.db")
        if not actual_db:
            return {"enabled": True, "error": "db_path not found"}
    except ImportError:
        return {"enabled": True, "error": "memory_common not found"}

    try:
        import json

        conn = connection_pool.get(actual_db)
        try:
            rows = conn.execute(
                "SELECT metadata FROM memories WHERE deleted_at IS NULL"
            ).fetchall()
        finally:
            safe_close_db(conn)

        halflives = []
        for (meta_json,) in rows:
            try:
                meta = json.loads(meta_json or "{}")
                hl = meta.get("adaptive_halflife_days")
                if hl is not None:
                    halflives.append(hl)
            except (json.JSONDecodeError, TypeError):
                pass

        if not halflives:
            return {
                "enabled": True,
                "notes_with_adaptive_halflife": 0,
                "mean_halflife_days": _DEFAULT_HALF_LIFE_DAYS,
            }

        return {
            "enabled": True,
            "notes_with_adaptive_halflife": len(halflives),
            "mean_halflife_days": round(sum(halflives) / len(halflives), 1),
            "min_halflife_days": round(min(halflives), 1),
            "max_halflife_days": round(max(halflives), 1),
        }
    except Exception:
        return {"enabled": True, "error": "stats unavailable"}


def get_last_access_query(
    note_id: str,
    db_path: str | None = None,
) -> Optional[str]:
    """Return the most recent search query that returned this note.

    Looks up ``memory_audit_log`` for the most recent ``memory_search``
    entry whose ``top1_id`` matches ``note_id``.  Returns the query
    string extracted from ``args`` (JSON), or ``None`` if no such
    search exists.

    P1-10 fix (2026-06-24): this function was previously missing from
    ``adaptive_retention``, causing the N+1 caller in
    ``search/scoring.py::_apply_neural_forget_curve`` to be a silent
    no-op.  The batch version ``get_last_access_queries_batch`` is the
    preferred interface for callers that need to look up many note_ids
    at once.

    Args:
        note_id: The note id to look up.
        db_path: Optional explicit path to the database.  If not given,
            resolves via ``get_memory_paths()``.

    Returns:
        The query string, or ``None`` if no matching audit entry exists.
    """
    result = get_last_access_queries_batch([note_id], db_path=db_path)
    return result.get(note_id)


def get_last_access_queries_batch(
    note_ids: list[str],
    db_path: str | None = None,
) -> dict[str, Optional[str]]:
    """Batch version of ``get_last_access_query``.

    P1-10 fix (2026-06-24): replaces the N+1 pattern in
    ``search/scoring.py::_apply_neural_forget_curve`` which called
    ``_galq(note_id)`` once per result row.  Now we do a single
    query against ``memory_audit_log`` that joins on the most recent
    ``memory_search`` per note_id via a window function, returning
    all results in one round trip.

    Args:
        note_ids: List of note ids to look up.  Empty list returns {}.
        db_path: Optional explicit path to the database.

    Returns:
        ``{note_id: query_string_or_None}`` dict.  Note ids that have
        no matching audit entry map to ``None``.
    """
    if not note_ids:
        return {}

    try:
        from _lazy_imports import get_memory_paths

        _, local_mem, _ = get_memory_paths()
        actual_db = db_path or str(local_mem / "memory.db")
        if not actual_db:
            return {nid: None for nid in note_ids}
    except ImportError:
        return {nid: None for nid in note_ids}

    result: dict[str, Optional[str]] = {nid: None for nid in note_ids}

    try:
        conn = connection_pool.get(actual_db)
    except Exception:
        return result

    try:
        # Use a single query that picks the most recent memory_search
        # per top1_id in the given set.  Window functions (ROW_NUMBER
        # OVER) are supported in SQLite 3.25+; if the table doesn't
        # exist or the query fails, we fall back to per-note queries
        # (better than nothing).
        placeholders = ",".join("?" * len(note_ids))
        try:
            rows = conn.execute(
                f"""
                SELECT top1_id, args FROM (
                    SELECT top1_id, args, ts,
                           ROW_NUMBER() OVER (
                               PARTITION BY top1_id ORDER BY ts DESC
                           ) AS rn
                    FROM memory_audit_log
                    WHERE tool = 'memory_search'
                      AND top1_id IN ({placeholders})
                ) WHERE rn = 1
                """,
                tuple(note_ids),
            ).fetchall()
            for top1_id, args_json in rows:
                if not top1_id or top1_id not in result:
                    continue
                try:
                    args = json.loads(args_json) if args_json else {}
                except (json.JSONDecodeError, TypeError):
                    continue
                # The query is typically in args["query"] for memory_search
                if isinstance(args, dict):
                    q = args.get("query")
                    if isinstance(q, str):
                        result[top1_id] = q
        except sqlite3.OperationalError:
            # Table doesn't exist yet — leave all results as None
            pass
    except Exception:
        pass
    finally:
        safe_close_db(conn)

    return result


from memory_common import make_lazy_getattr

__getattr__ = make_lazy_getattr({"ADAPTIVE_RETENTION_ENABLED": "adaptive_retention"})
