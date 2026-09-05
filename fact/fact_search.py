"""Fact search, list, and stats for agentic-memory.

FTS5-backed search with LIKE fallback, temporal decay scoring,
list with confidence filtering, and DB-lifecycle wrappers.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from pathlib import Path

from .fact_schema import ensure_facts_schema
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fact Search
# ---------------------------------------------------------------------------


def _build_fts_query(query_lower: str) -> str | None:
    """Build an FTS5 OR-joined query string from a user search.

    Each whitespace-separated token is wrapped in double quotes and OR-joined.
    FTS5 special characters (`*`, `^`) are stripped from tokens to avoid
    syntax errors on untrusted input.  Returns None for an empty/blank query.
    """
    tokens = query_lower.split()
    if not tokens:
        return None
    safe: list[str] = []
    for t in tokens:
        # Strip FTS5 operators that would change query semantics.
        t = t.replace('"', '""').replace("*", "").replace("^", "")
        t = t.strip()
        if t:
            safe.append(f'"{t}"')
    if not safe:
        return None
    return " OR ".join(safe)


_KG_FACTS_COLS_CACHE: dict[int, set[str]] = {}


def _get_kg_facts_cols(conn: AnyConnection) -> set[str]:
    conn_id = id(conn)
    cols = _KG_FACTS_COLS_CACHE.get(conn_id)
    if cols is not None:
        return cols
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(kg_facts)").fetchall()}
        if cols:
            _KG_FACTS_COLS_CACHE[conn_id] = cols
            return cols
    except Exception:
        pass
    return set()


def _facts_search_fts(
    conn: AnyConnection, fts_query: str, limit: int, include_superseded: bool = False
) -> list[sqlite3.Row] | None:
    """FTS5-backed fact search.

    Returns up to `limit` rows ordered by FTS5 BM25 rank.  Returns None on
    any FTS5 error (caller falls back to LIKE).  The SELECT is column-stable
    with the LIKE fallback so downstream scoring is identical.
    """
    try:
        where_extra = ""
        if not include_superseded:
            cols = _get_kg_facts_cols(conn)
            if "superseded_by" in cols and "invalid_at" in cols:
                where_extra = " AND kf.superseded_by IS NULL AND (kf.invalid_at IS NULL OR kf.invalid_at = '')"
            elif "superseded_by" in cols:
                where_extra = " AND kf.superseded_by IS NULL"

        rows = conn.execute(
            "SELECT kf.id, kf.subject, kf.predicate, kf.object, kf.confidence, "
            "kf.locked, kf.first_seen, kf.last_seen, kf.mention_count, "
            "kf.source_memory "
            "FROM kg_facts_fts "
            "JOIN kg_facts kf ON kf.rowid = kg_facts_fts.rowid "
            f"WHERE kg_facts_fts MATCH ?{where_extra} "
            "ORDER BY kg_facts_fts.rank "
            "LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        return rows
    except Exception:
        logger.warning(
            "FTS5 fact search failed; falling back to LIKE scan", exc_info=True
        )
        return None


def _facts_search_like(
    conn: AnyConnection, query_lower: str, limit: int, include_superseded: bool = False
) -> list[sqlite3.Row]:
    """Original LIKE-based fact search.  Fallback for pre-v20 DBs and FTS5
    syntax errors.  O(n) full table scan due to leading-wildcard LIKE."""
    where_extra = ""
    try:
        if not include_superseded:
            cols = _get_kg_facts_cols(conn)
            if "superseded_by" in cols and "invalid_at" in cols:
                where_extra = " AND superseded_by IS NULL AND (invalid_at IS NULL OR invalid_at = '')"
            elif "superseded_by" in cols:
                where_extra = " AND superseded_by IS NULL"
    except Exception:
        pass

    return conn.execute(
        "SELECT id, subject, predicate, object, confidence, locked, "
        "first_seen, last_seen, mention_count, source_memory "
        "FROM kg_facts "
        f"WHERE (subject LIKE ? OR predicate LIKE ? OR object LIKE ?){where_extra} "
        "LIMIT ?",
        (f"%{query_lower}%", f"%{query_lower}%", f"%{query_lower}%", limit),
    ).fetchall()


def facts_search(
    conn: AnyConnection, query: str, limit: int = 10,
    belief_status: str | None = None,
    epistemic_source: str | None = None,
    fact_type: str | None = None,
    include_superseded: bool = False,
) -> list[dict]:
    query_lower = query.lower().strip()
    now = time.time()
    half_life = 180 * 86400

    if not query_lower:
        return []

    fts_query = _build_fts_query(query_lower)
    rows: list | None = None
    if fts_query is not None:
        rows = _facts_search_fts(conn, fts_query, limit * 3, include_superseded=include_superseded)
    if not rows:
        rows = _facts_search_like(conn, query_lower, limit * 3, include_superseded=include_superseded)

    # Apply belief filters if specified
    if rows and (belief_status is not None or epistemic_source is not None or fact_type is not None):
        ids = [r[0] for r in rows]
        if not ids:
            rows = []
        else:
            conditions = []
            params = []
            if belief_status is not None:
                conditions.append("belief_status = ?")
                params.append(belief_status)
            if epistemic_source is not None:
                conditions.append("epistemic_source = ?")
                params.append(epistemic_source)
            if fact_type is not None:
                conditions.append("fact_type = ?")
                params.append(fact_type)
            placeholders = ",".join("?" for _ in ids)
            conditions.append(f"id IN ({placeholders})")
            params.extend(ids)
            sql = f"SELECT id FROM kg_facts WHERE {' AND '.join(conditions)}"
            valid_ids = {r[0] for r in conn.execute(sql, params).fetchall()}
            rows = [r for r in rows if r[0] in valid_ids]

    def _effective(conf: float, locked: int, last_seen: float) -> float:
        if locked:
            return conf
        age = now - (last_seen or now)
        return conf * math.pow(0.5, age / half_life)

    scored = []
    for r in rows:
        eff = _effective(r[4], r[5], r[7])
        scored.append(
            (
                eff,
                {
                    "id": r[0],
                    "subject": r[1],
                    "predicate": r[2],
                    "object": r[3],
                    "confidence": r[4],
                    "locked": bool(r[5]),
                    "first_seen": r[6],
                    "last_seen": r[7],
                    "mention_count": r[8],
                    "source_memory": r[9],
                    "effective_confidence": round(eff, 4),
                },
            )
        )
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s[1] for s in scored[:limit]]


# ---------------------------------------------------------------------------
# Fact List
# ---------------------------------------------------------------------------


def facts_list(
    conn: AnyConnection, limit: int = 20, min_confidence: float = 0.0,
    belief_status: str | None = None,
    epistemic_source: str | None = None,
    fact_type: str | None = None,
    include_superseded: bool = False,
    tenant_id: str | None = None,
) -> list[dict]:
    resolved_tenant = tenant_id
    if not resolved_tenant:
        try:
            from agent_context import get_agent
            _ctx = get_agent()
            resolved_tenant = getattr(_ctx, "tenant_id", None)
        except Exception:
            pass
    if not resolved_tenant and conn is not None:
        try:
            val = conn.execute("SELECT tenant_id()").fetchone()[0]
            if val:
                resolved_tenant = str(val)
        except Exception:
            pass

    conditions = ["confidence >= ?"]
    params: list = [min_confidence]
    if resolved_tenant:
        conditions.append("tenant_id = ?")
        params.append(resolved_tenant)
    if not include_superseded:
        try:
            cols = _get_kg_facts_cols(conn)
            if "superseded_by" in cols and "invalid_at" in cols:
                conditions.append("(superseded_by IS NULL AND (invalid_at IS NULL OR invalid_at = ''))")
            elif "superseded_by" in cols:
                conditions.append("superseded_by IS NULL")
        except Exception:
            pass
    if belief_status is not None:
        conditions.append("belief_status = ?")
        params.append(belief_status)
    if epistemic_source is not None:
        conditions.append("epistemic_source = ?")
        params.append(epistemic_source)
    if fact_type is not None:
        conditions.append("fact_type = ?")
        params.append(fact_type)
    sql = (
        "SELECT id, subject, predicate, object, confidence, locked, "
        "first_seen, last_seen, mention_count, source_memory "
        "FROM kg_facts WHERE {} "
        "ORDER BY confidence DESC, mention_count DESC LIMIT ?"
    ).format(" AND ".join(conditions))
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r[0],
            "subject": r[1],
            "predicate": r[2],
            "object": r[3],
            "confidence": r[4],
            "locked": bool(r[5]),
            "first_seen": r[6],
            "last_seen": r[7],
            "mention_count": r[8],
            "source_memory": r[9],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Fact Stats
# ---------------------------------------------------------------------------


def facts_stats(conn: AnyConnection) -> dict:
    try:
        total_row = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()
        total = int(total_row[0]) if total_row is not None else 0
        locked_row = conn.execute(
            "SELECT COUNT(*) FROM kg_facts WHERE locked = 1"
        ).fetchone()
        locked = int(locked_row[0]) if locked_row is not None else 0
        predicates = {}
        for row in conn.execute(
            "SELECT predicate, COUNT(*) FROM kg_facts GROUP BY predicate"
        ).fetchall():
            predicates[row[0]] = row[1]
        avg_conf_row = conn.execute("SELECT AVG(confidence) FROM kg_facts").fetchone()
        avg_conf = (avg_conf_row[0] if avg_conf_row is not None else 0.0) or 0.0
        return {
            "total_facts": total,
            "locked_facts": locked,
            "avg_confidence": round(avg_conf, 4),
            "predicate_distribution": predicates,
        }
    except sqlite3.OperationalError:
        return {
            "total_facts": 0,
            "locked_facts": 0,
            "error": "facts table not initialized",
        }


# ---------------------------------------------------------------------------
# DB-lifecycle wrappers (T3-item3: push conn mgmt out of MCP layer)
# ---------------------------------------------------------------------------


def facts_search_db(
    db_path: str | Path,
    query: str,
    limit: int = 10,
    belief_status: str | None = None,
    epistemic_source: str | None = None,
    fact_type: str | None = None,
) -> list[dict]:
    """facts_search with connection lifecycle managed."""
    from infra.memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_facts_schema(conn)
    try:
        return facts_search(
            conn, query, limit=limit,
            belief_status=belief_status,
            epistemic_source=epistemic_source,
            fact_type=fact_type,
        )
    finally:
        safe_close_db(conn)


def facts_list_db(
    db_path: str | Path, limit: int = 20, min_confidence: float = 0.0,
    tenant_id: str | None = None,
) -> list[dict]:
    """facts_list with connection lifecycle managed."""
    from infra.memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0, tenant_id=tenant_id)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_facts_schema(conn)
    try:
        return facts_list(conn, limit=limit, min_confidence=min_confidence, tenant_id=tenant_id)
    finally:
        safe_close_db(conn)


def facts_stats_db(db_path: str | Path) -> dict:
    """facts_stats with connection lifecycle managed."""
    from infra.memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_facts_schema(conn)
    try:
        return facts_stats(conn)
    finally:
        safe_close_db(conn)
