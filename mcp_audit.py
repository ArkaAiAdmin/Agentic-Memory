"""
Audit subsystem MCP tools — audit, audit_query, check_integrity.

Extracted from mcp_maintenance.py to reduce module size.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from mcp_common import (
    _resolve_memory_dir,
    _err,
    ErrorCode,
    GLOBAL_MEM_DIR,
    connection_pool,
    run_db_migrations,
    safe_close_db,
    logger,
    with_audit,
)
from mcp_instance import mcp


@with_audit("memory_audit")
def memory_audit() -> str:
    """Audit memory system health using SRMA-inspired metrics."""
    active_dir = _resolve_memory_dir()
    db_path = active_dir / "memory.db"
    if not db_path.exists():
        return _err(
            ErrorCode.DB_ERROR,
            f"No memory database found. Looked at:\n  - local:  {db_path} (cwd resolves to a project with no `memory/`)\n  - global: {GLOBAL_MEM_DIR / 'memory.db'}\nRun memory_rebuild first, or pass is_global=True to memory_save.",
        )
    try:
        db = connection_pool.get(str(db_path), timeout=30.0)
        db.execute("PRAGMA busy_timeout = 30000;")
        run_db_migrations(db)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, content, created_at, updated_at, access_count, pinned FROM memories"
        ).fetchall()
        safe_close_db(db)
        if not rows:
            return "No memories found to audit."
        now = datetime.now(timezone.utc)
        metrics = []
        corrupted_dates = 0
        for row in rows:
            content = row["content"] or ""
            access_count = row["access_count"] or 0
            created_at = row["created_at"]
            updated_at = row["updated_at"]
            try:
                created = datetime.fromisoformat(str(created_at))
                updated = datetime.fromisoformat(str(updated_at))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                corrupted_dates += 1
                continue
            days_since_creation = max(1.0, (now - created).total_seconds() / 86400)
            days_since_updated = max(0.0, (now - updated).total_seconds() / 86400)
            rho = access_count / days_since_creation
            psi = days_since_updated / max(1, access_count)
            omega = len(content) / max(1, access_count)
            metrics.append(
                {
                    "id": row["id"],
                    "pinned": row["pinned"],
                    "access_count": access_count,
                    "rho": rho,
                    "psi": psi,
                    "omega": omega,
                    "content_preview": content[:80]
                    .replace("\n", " ")
                    .replace("\r", ""),
                }
            )
        n = len(metrics)
        max_rho = max(m["rho"] for m in metrics) or 1.0
        max_psi = max(m["psi"] for m in metrics) or 1.0
        max_omega = max(m["omega"] for m in metrics) or 1.0
        health_scores = []
        for m in metrics:
            n_rho = m["rho"] / max_rho
            n_psi = m["psi"] / max_psi
            n_omega = m["omega"] / max_omega
            health_scores.append((n_rho + (1 - n_psi) + (1 - n_omega)) / 3)
        overall_health = sum(health_scores) / n if n else 0
        drifted = sorted(metrics, key=lambda m: m["psi"], reverse=True)[:5]
        efficient = sorted(metrics, key=lambda m: m["omega"])[:5]
        never_accessed = [m for m in metrics if m["access_count"] == 0]
        lines = [
            "=== Memory Audit Report ===",
            f"Scope: {'global' if active_dir == GLOBAL_MEM_DIR else 'local'} ({active_dir})",
            f"Total memories: {n}",
            f"Overall health score: {overall_health:.3f} (0=worst, 1=best)",
            "",
            "--- Top 5 Most Drifted (candidates for archival) ---",
        ]
        for m in drifted:
            lines.append(
                f"  [{m['id']}] psi={m['psi']:.1f}  accesses={m['access_count']}  {m['content_preview']}"
            )
        lines.append("")
        lines.append("--- Top 5 Most Efficient (core knowledge) ---")
        for m in efficient:
            lines.append(
                f"  [{m['id']}] omega={m['omega']:.1f}  accesses={m['access_count']}  {m['content_preview']}"
            )
        lines.append("")
        lines.append(
            f"--- Never Accessed ({len(never_accessed)} memories, candidates for deletion) ---"
        )
        for m in never_accessed:
            lines.append(f"  [{m['id']}] {m['content_preview']}")
        if corrupted_dates:
            lines.append("")
            lines.append(
                f"--- Skipped {corrupted_dates} memories with corrupt created_at/updated_at ---"
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("during audit")
        return _err(ErrorCode.DB_ERROR, "during audit")


@mcp.tool()
@with_audit("memory_audit_query")
def memory_audit_query(
    tool_name: Optional[str] = None,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    only_errors: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Query the memory_audit_log table for observability."""
    active_dir = _resolve_memory_dir()
    db_path = active_dir / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"No memory database found at {db_path}.")
    if limit < 1 or limit > 500:
        return _err(ErrorCode.INVALID_PARAMS, "limit must be 1..500")
    if offset < 0:
        return _err(ErrorCode.INVALID_PARAMS, "offset must be >= 0")
    if since_ts is not None and until_ts is not None and since_ts > until_ts:
        return _err(ErrorCode.INVALID_PARAMS, "since_ts must be <= until_ts")
    where: list[str] = []
    params: list[Any] = []
    if tool_name is not None:
        where.append("tool = ?")
        params.append(tool_name)
    if since_ts is not None:
        where.append("ts >= ?")
        params.append(since_ts)
    if until_ts is not None:
        where.append("ts <= ?")
        params.append(until_ts)
    if only_errors:
        where.append("error IS NOT NULL")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        from _lazy_imports import open_db

        with open_db(db_path, timeout=5.0, row_factory=sqlite3.Row) as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM memory_audit_log {where_sql}", params
            ).fetchone()[0]
            error_count = db.execute(
                f"SELECT COUNT(*) FROM memory_audit_log {where_sql + (' AND' if where_sql else 'WHERE')} error IS NOT NULL",
                params,
            ).fetchone()[0]
            if total == 0:
                return json.dumps(
                    {
                        "ok": True,
                        "rows": [],
                        "summary": {
                            "total": 0,
                            "errors": 0,
                            "returned": 0,
                            "limit": limit,
                            "offset": offset,
                        },
                    }
                )
            ts_min, ts_max = db.execute(
                f"SELECT MIN(ts), MAX(ts) FROM memory_audit_log {where_sql}", params
            ).fetchone()
            rows = db.execute(
                f"SELECT id, ts, tool, args, results_count, top1_id, latency_ms, error, request_id FROM memory_audit_log {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            out_rows = []
            for r in rows:
                out_rows.append(
                    {
                        "id": r["id"],
                        "ts": r["ts"],
                        "tool": r["tool"],
                        "args": json.loads(r["args"]) if r["args"] else None,
                        "results_count": r["results_count"],
                        "top1_id": r["top1_id"],
                        "latency_ms": r["latency_ms"],
                        "error": r["error"],
                        "request_id": r["request_id"],
                    }
                )
            return json.dumps(
                {
                    "ok": True,
                    "rows": out_rows,
                    "summary": {
                        "total": total,
                        "errors": error_count,
                        "returned": len(out_rows),
                        "limit": limit,
                        "offset": offset,
                        "ts_min": ts_min,
                        "ts_max": ts_max,
                    },
                }
            )
    except sqlite3.OperationalError:
        logger.exception("Query failed")
        return _err(ErrorCode.DB_ERROR, "Query failed")


@mcp.tool()
@with_audit("memory_circuit_breaker_status")
def memory_circuit_breaker_status(
    limit: int = 20,
    since_ts: Optional[float] = None,
) -> str:
    """Return the most recent auto-save circuit-breaker events.

    Audit-gap fix (2026-06-22 follow-up): the breaker state used to
    live only in process memory.  ``_persist_circuit_state`` now
    appends ``auto_save_circuit_open`` and ``auto_save_circuit_close``
    events to ``memory_audit_log`` (see ``auto_save.py``).  This
    function surfaces them so an operator can see the open/close
    history across process restarts.

    Args:
        limit: Maximum number of events to return (1..200, default 20).
        since_ts: Optional Unix-epoch lower bound; only events at or
            after this timestamp are returned.

    Returns:
        JSON with:
          - events: list of {ts, tool, args} dicts, newest first.
            ``args`` is the JSON-encoded details dict written by
            ``_persist_circuit_state`` (open: n_failures / window_s /
            cb_seconds / open_until; close: open_until_was /
            recovered_at).
          - summary: {total_events, open_count, close_count,
            ts_min, ts_max, limit}
    """
    try:
        active_dir = _resolve_memory_dir()
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(
                ErrorCode.DB_ERROR,
                f"memory.db not found at {db_path} -- run memory_rebuild first.",
            )
        if limit < 1 or limit > 200:
            return _err(ErrorCode.INVALID_PARAMS, "limit must be 1..200")
        where_parts = ["tool LIKE 'auto_save_circuit_%'"]
        params: list[Any] = []
        if since_ts is not None:
            where_parts.append("ts >= ?")
            params.append(since_ts)
        where_sql = "WHERE " + " AND ".join(where_parts)
        from _lazy_imports import open_db

        with open_db(db_path, timeout=5.0, row_factory=sqlite3.Row) as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM memory_audit_log {where_sql}", params
            ).fetchone()[0]
            open_count = db.execute(
                f"SELECT COUNT(*) FROM memory_audit_log "
                f"{where_sql} AND tool = 'auto_save_circuit_open'",
                params,
            ).fetchone()[0]
            close_count = db.execute(
                f"SELECT COUNT(*) FROM memory_audit_log "
                f"{where_sql} AND tool = 'auto_save_circuit_close'",
                params,
            ).fetchone()[0]
            if total == 0:
                return json.dumps(
                    {
                        "ok": True,
                        "events": [],
                        "summary": {
                            "total_events": 0,
                            "open_count": 0,
                            "close_count": 0,
                            "ts_min": None,
                            "ts_max": None,
                            "limit": limit,
                        },
                    }
                )
            ts_min, ts_max = db.execute(
                f"SELECT MIN(ts), MAX(ts) FROM memory_audit_log {where_sql}",
                params,
            ).fetchone()
            rows = db.execute(
                f"SELECT id, ts, tool, args FROM memory_audit_log {where_sql} "
                f"ORDER BY id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            out = []
            for r in rows:
                out.append(
                    {
                        "id": r["id"],
                        "ts": r["ts"],
                        "tool": r["tool"],
                        "args": json.loads(r["args"]) if r["args"] else None,
                    }
                )
            return json.dumps(
                {
                    "ok": True,
                    "events": out,
                    "summary": {
                        "total_events": total,
                        "open_count": open_count,
                        "close_count": close_count,
                        "ts_min": ts_min,
                        "ts_max": ts_max,
                        "limit": limit,
                    },
                }
            )
    except sqlite3.OperationalError:
        logger.exception("circuit_breaker_status query failed")
        return _err(ErrorCode.DB_ERROR, "circuit_breaker_status query failed")


@mcp.tool()
@with_audit("memory_check_integrity")
def memory_check_integrity(deep: bool = False) -> str:
    """Run a health check on the memory DB.

    G6 fix (2026-06-22): this is the *index / data* integrity check.
    Compare with the internal ``run_db_migrations`` (mcp_common.py)
    which only runs the schema migration forward — it does NOT
    check that the data is consistent.

    * ``memory_check_integrity`` (this tool) — calls
      ``memory_integrity.check_index_integrity``.  Reports FTS5
      drift, missing embeddings, dangling backlinks, FK violations,
      corrupt date columns, and a summary count of orphans.  Use
      this when an operator asks "is the DB healthy?".

    * ``run_db_migrations`` (mcp_common.py) — applies any pending
      migrations.  The ``memory_audit`` tool calls it implicitly
      before reading the audit data, so a fresh-migration DB is
      upgraded on the first audit call.  Use ``migration_runner.py``
      for explicit, operator-driven migration control.

    Both are safe to run repeatedly.  The two paths share an
    implicit dependency: ``check_integrity`` may report
    "schema_version out of date" if a migration was never applied;
    the fix is to run ``memory_audit`` (which triggers
    ``run_db_migrations``) or to call ``migration_runner.py
    upgrade`` directly.
    """
    try:
        from memory_integrity import check_index_integrity

        try:
            active_dir = _resolve_memory_dir()
        except Exception:
            active_dir = None
        if active_dir is None:
            return _err(ErrorCode.DB_ERROR, "No active memory directory found.")
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(
                ErrorCode.DB_ERROR,
                f"memory.db not found at {db_path} -- run memory_rebuild first.",
            )
        report = check_index_integrity(db_path, deep=deep)
        lines = [
            f"Integrity check ({report['summary']})",
            f"  ok: {report['ok']}",
            "  findings:",
        ]
        for f in report["findings"]:
            lines.append(
                f"    [{f['severity']:8s}] {f.get('check', ''):20s} {f['message']}"
            )
            if f.get("fix_hint"):
                lines.append(f"               fix: {f['fix_hint']}")
        return "\n".join(lines)
    except Exception:
        logger.exception("memory_check_integrity failed")
        return _err(ErrorCode.DB_ERROR, "Integrity check failed")


@mcp.tool()
@with_audit("memory_temporal_contradictions")
def memory_temporal_contradictions(
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    reason: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """T3.6: list fact-level supersession events in a time window.

    A "supersession event" is when one fact is marked as replaced by
    another (via the temporal KG's auto-detector or via manual override).
    Each row in the result includes:

      * the OLD fact (id, subject, predicate, object, event_time, invalid_at)
      * the NEW fact (id, subject, predicate, object, event_time)
      * reason (e.g. 'contradicted' / 'superseded' / 'expired' / 'manual')
      * contradiction_score (1.0 for deterministic auto-detection)
      * transaction_time — when the supersession was recorded

    Args:
      since_ts: Filter to events with transaction_time >= since_ts.
        Default: no lower bound.
      until_ts: Filter to events with transaction_time <= until_ts.
        Default: no upper bound.
      reason: Filter by invalidation_reason (e.g. 'contradicted'). Default: all.
      limit: Max rows to return (1..500). Default: 50.
      offset: Skip first N rows (for paging). Default: 0.

    Returns:
      JSON with ok, summary (total/returned/limit/offset), and rows.

    Use case: audit a window of time for fact changes — e.g. "what
    facts changed in the last 7 days?" or "show me all auto-detected
    contradictions this week".
    """
    active_dir = _resolve_memory_dir()
    db_path = active_dir / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"No memory database found at {db_path}.")
    if limit < 1 or limit > 500:
        return _err(ErrorCode.INVALID_PARAMS, "limit must be 1..500")
    if offset < 0:
        return _err(ErrorCode.INVALID_PARAMS, "offset must be >= 0")
    if since_ts is not None and until_ts is not None and since_ts > until_ts:
        return _err(ErrorCode.INVALID_PARAMS, "since_ts must be <= until_ts")

    where: list[str] = ["old.superseded_by IS NOT NULL"]
    params: list[Any] = []
    if since_ts is not None:
        where.append("old.transaction_time >= ?")
        params.append(since_ts)
    if until_ts is not None:
        where.append("old.transaction_time <= ?")
        params.append(until_ts)
    if reason is not None:
        where.append("old.invalidation_reason = ?")
        params.append(reason)
    where_sql = " AND ".join(where)

    try:
        # Read-only URI bypasses the flock — WAL allows concurrent reads
        # alongside the live auto-save daemon.  This is a read-only query
        # so no flock protection is needed.
        ro_uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(ro_uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "kg_facts" not in tables:
                return _err(
                    ErrorCode.DB_ERROR,
                    "kg_facts table not found (pre-temporal-KG schema).",
                )
            total = db.execute(
                f"SELECT COUNT(*) FROM kg_facts AS old WHERE {where_sql}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT
                    old.id AS old_id,
                    old.subject AS old_subject,
                    old.predicate AS old_predicate,
                    old.object AS old_object,
                    old.event_time AS old_event_time,
                    old.event_time_granularity AS old_granularity,
                    old.invalid_at AS old_invalid_at,
                    new.id AS new_id,
                    new.subject AS new_subject,
                    new.predicate AS new_predicate,
                    new.object AS new_object,
                    new.event_time AS new_event_time,
                    new.event_time_granularity AS new_granularity,
                    old.invalidation_reason AS reason,
                    old.contradiction_score AS score,
                    old.transaction_time AS ts
                FROM kg_facts AS old
                JOIN kg_facts AS new ON old.superseded_by = new.id
                WHERE {where_sql}
                ORDER BY old.transaction_time DESC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()
            out = []
            for r in rows:
                out.append(
                    {
                        "old": {
                            "id": r["old_id"],
                            "subject": r["old_subject"],
                            "predicate": r["old_predicate"],
                            "object": r["old_object"],
                            "event_time": r["old_event_time"],
                            "event_time_granularity": r["old_granularity"],
                            "invalid_at": r["old_invalid_at"],
                        },
                        "new": {
                            "id": r["new_id"],
                            "subject": r["new_subject"],
                            "predicate": r["new_predicate"],
                            "object": r["new_object"],
                            "event_time": r["new_event_time"],
                            "event_time_granularity": r["new_granularity"],
                        },
                        "reason": r["reason"],
                        "contradiction_score": r["score"],
                        "transaction_time": r["ts"],
                    }
                )
            return json.dumps(
                {
                    "ok": True,
                    "summary": {
                        "total": total,
                        "returned": len(out),
                        "limit": limit,
                        "offset": offset,
                    },
                    "rows": out,
                },
                indent=2,
            )
    except Exception as e:
        logger.exception("memory_temporal_contradictions failed")
        return _err(ErrorCode.DB_ERROR, f"temporal_contradictions failed: {e}")


@mcp.tool()
@with_audit("memory_temporal_query")
def memory_temporal_query(
    operation: str,
    as_of: Optional[float] = None,
    fact_id: Optional[int] = None,
    since_ts: Optional[float] = None,
    query: Optional[str] = None,
    limit: int = 100,
) -> str:
    """T4.5: time-aware queries on the fact-level knowledge graph.

    Single entry point for three time-aware operations:

      * ``operation="at_time"``      — facts valid at epoch ``as_of``.
                                       ``query`` (optional) adds a
                                       case-insensitive substring
                                       filter on subject/predicate/object.
      * ``operation="chain"``        — walk the ``superseded_by`` chain
                                       for ``fact_id`` (oldest first).
      * ``operation="changed_since"``— facts inserted or invalidated
                                       since epoch ``since_ts``.

    Args:
      operation: one of 'at_time' / 'chain' / 'changed_since'.
      as_of: epoch seconds (for at_time).
      fact_id: int (for chain).
      since_ts: epoch seconds (for changed_since).
      query: optional case-insensitive substring filter (at_time only).
      limit: max rows (at_time and changed_since; default 100).

    Returns:
      JSON with ok, operation, and the rows.
    """
    active_dir = _resolve_memory_dir()
    db_path = active_dir / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"No memory database found at {db_path}.")
    if operation not in ("at_time", "chain", "changed_since"):
        return _err(
            ErrorCode.INVALID_PARAMS,
            f"operation must be 'at_time', 'chain', or 'changed_since' (got {operation!r})",
        )
    if limit < 1 or limit > 1000:
        return _err(ErrorCode.INVALID_PARAMS, "limit must be 1..1000")
    if operation == "at_time" and as_of is None:
        return _err(
            ErrorCode.INVALID_PARAMS,
            "as_of (epoch seconds) is required for operation='at_time'",
        )
    if operation == "chain" and fact_id is None:
        return _err(
            ErrorCode.INVALID_PARAMS,
            "fact_id is required for operation='chain'",
        )
    if operation == "changed_since" and since_ts is None:
        return _err(
            ErrorCode.INVALID_PARAMS,
            "since_ts (epoch seconds) is required for operation='changed_since'",
        )
    try:
        from fact_temporal import (
            query_facts_at_time,
            query_fact_supersession_chain,
            query_facts_changed_since,
        )

        # Read-only URI bypasses the flock — same as
        # memory_temporal_contradictions above.
        ro_uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(ro_uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            if operation == "at_time":
                assert as_of is not None  # validated above
                rows = query_facts_at_time(db, as_of, query=query, limit=limit)
            elif operation == "chain":
                assert fact_id is not None  # validated above
                rows = query_fact_supersession_chain(db, fact_id)
            else:  # changed_since
                assert since_ts is not None  # validated above
                rows = query_facts_changed_since(db, since_ts, limit=limit)
            return json.dumps(
                {
                    "ok": True,
                    "operation": operation,
                    "count": len(rows),
                    "rows": rows,
                },
                indent=2,
            )
    except Exception as e:
        logger.exception("memory_temporal_query failed")
        return _err(ErrorCode.DB_ERROR, f"temporal_query failed: {e}")


@mcp.tool()
@with_audit("memory_compliance_check")
def memory_compliance_check(session_id: str = "") -> str:
    """Check whether AGENTS.md workflow rules were followed in this session.

    Reads the session marker file and auto-save logs to determine if
    key steps were performed. This is a non-blocking audit — it reports
    compliance but does not enforce anything.

    Checks:
      - Was a session-start save performed? (Rule #1)
      - Were there tool calls without a session-end save? (Rule #7)
      - Does the health_status.json exist and pass? (Rules #5, #9-11)

    Args:
        session_id: optional session identifier to scope the check.

    Returns:
        JSON with compliance_score (0-1), checks list, and skipped items.
    """
    try:

        mem_dir = GLOBAL_MEM_DIR
        marker_file = mem_dir / ".last_session_save.json"
        health_file = mem_dir / ".health_status.json"

        checks = []
        skipped = []
        score = 1.0

        # Rule #7: session-end save
        if marker_file.exists():
            try:
                marker = json.loads(marker_file.read_text())
                saved_at = marker.get("saved_at", 0)
                tool_count = marker.get("tool_count", 0)
                if saved_at > 0:
                    checks.append(
                        {
                            "rule": "#7",
                            "name": "session_end_save",
                            "status": "ok",
                            "detail": f"saved_at={saved_at}, tools={tool_count}",
                        }
                    )
                elif tool_count > 0:
                    checks.append(
                        {
                            "rule": "#7",
                            "name": "session_end_save",
                            "status": "skipped",
                            "detail": f"{tool_count} tool calls without session-end save",
                        }
                    )
                    skipped.append("Rule #7: session-end memory_save not performed")
                    score -= 0.2
                else:
                    checks.append(
                        {
                            "rule": "#7",
                            "name": "session_end_save",
                            "status": "no_activity",
                            "detail": "no tool calls recorded",
                        }
                    )
            except (json.JSONDecodeError, OSError) as e:
                checks.append(
                    {
                        "rule": "#7",
                        "name": "session_end_save",
                        "status": "error",
                        "detail": str(e),
                    }
                )
        else:
            checks.append(
                {
                    "rule": "#7",
                    "name": "session_end_save",
                    "status": "no_marker",
                    "detail": "marker file not found",
                }
            )

        # Rules #5, #9-11: health_status.json
        if health_file.exists():
            try:
                health = json.loads(health_file.read_text())
                overall = health.get("overall_healthy", True)
                alerts = health.get("alerts", [])
                ts = health.get("timestamp", "unknown")
                checks.append(
                    {
                        "rule": "#5,#9-11",
                        "name": "system_health",
                        "status": "ok" if overall else "unhealthy",
                        "detail": f"timestamp={ts}, alerts={len(alerts)}, healthy={overall}",
                        "alerts": alerts[:5],
                    }
                )
                if not overall:
                    score -= 0.15
                    skipped.append(
                        f"System health check found {len(alerts)} alerts — see .health_status.json"
                    )
            except (json.JSONDecodeError, OSError) as e:
                checks.append(
                    {
                        "rule": "#5,#9-11",
                        "name": "system_health",
                        "status": "error",
                        "detail": str(e),
                    }
                )
        else:
            checks.append(
                {
                    "rule": "#5,#9-11",
                    "name": "system_health",
                    "status": "no_file",
                    "detail": ".health_status.json not found — cron_health_check may not have run yet",
                }
            )

        score = max(0.0, min(1.0, score))

        return json.dumps(
            {
                "ok": True,
                "compliance_score": round(score, 2),
                "checks": checks,
                "skipped": skipped,
                "recommendation": (
                    "Run memory_save(category='sessions') before ending session"
                    if any(c.get("status") == "skipped" for c in checks)
                    else "All checked rules are satisfied."
                ),
            },
            indent=2,
        )
    except Exception as e:
        logger.exception("memory_compliance_check failed")
        return _err(ErrorCode.DB_ERROR, f"compliance_check failed: {e}")
