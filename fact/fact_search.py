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


def _facts_search_fts(
    conn: sqlite3.Connection, fts_query: str, limit: int
) -> list[sqlite3.Row] | None:
    """FTS5-backed fact search.

    Returns up to `limit` rows ordered by FTS5 BM25 rank.  Returns None on
    any FTS5 error (caller falls back to LIKE).  The SELECT is column-stable
    with the LIKE fallback so downstream scoring is identical.
    """
    try:
        rows = conn.execute(
            "SELECT kf.id, kf.subject, kf.predicate, kf.object, kf.confidence, "
            "kf.locked, kf.first_seen, kf.last_seen, kf.mention_count, "
            "kf.source_memory "
            "FROM kg_facts_fts "
            "JOIN kg_facts kf ON kf.rowid = kg_facts_fts.rowid "
            "WHERE kg_facts_fts MATCH ? "
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
    conn: sqlite3.Connection, query_lower: str, limit: int
) -> list[sqlite3.Row]:
    """Original LIKE-based fact search.  Fallback for pre-v20 DBs and FTS5
    syntax errors.  O(n) full table scan due to leading-wildcard LIKE."""
    return conn.execute(
        "SELECT id, subject, predicate, object, confidence, locked, "
        "first_seen, last_seen, mention_count, source_memory "
        "FROM kg_facts "
        "WHERE subject LIKE ? OR predicate LIKE ? OR object LIKE ? "
        "LIMIT ?",
        (f"%{query_lower}%", f"%{query_lower}%", f"%{query_lower}%", limit),
    ).fetchall()


def facts_search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    query_lower = query.lower().strip()
    now = time.time()
    half_life = 180 * 86400

    if not query_lower:
        return []

    fts_query = _build_fts_query(query_lower)
    rows: list | None = None
    if fts_query is not None:
        rows = _facts_search_fts(conn, fts_query, limit * 3)
    if not rows:
        rows = _facts_search_like(conn, query_lower, limit * 3)

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
    conn: sqlite3.Connection, limit: int = 20, min_confidence: float = 0.0
) -> list[dict]:
    rows = conn.execute(
        "SELECT id, subject, predicate, object, confidence, locked, "
        "first_seen, last_seen, mention_count, source_memory "
        "FROM kg_facts WHERE confidence >= ? "
        "ORDER BY confidence DESC, mention_count DESC LIMIT ?",
        (min_confidence, limit),
    ).fetchall()
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


def facts_stats(conn: sqlite3.Connection) -> dict:
    try:
        total = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
        locked = conn.execute(
            "SELECT COUNT(*) FROM kg_facts WHERE locked = 1"
        ).fetchone()[0]
        predicates = {}
        for row in conn.execute(
            "SELECT predicate, COUNT(*) FROM kg_facts GROUP BY predicate"
        ).fetchall():
            predicates[row[0]] = row[1]
        avg_conf = (
            conn.execute("SELECT AVG(confidence) FROM kg_facts").fetchone()[0] or 0.0
        )
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


def facts_search_db(db_path: str | Path, query: str, limit: int = 10) -> list[dict]:
    """facts_search with connection lifecycle managed."""
    from infra.memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_facts_schema(conn)
    try:
        return facts_search(conn, query, limit=limit)
    finally:
        safe_close_db(conn)


def facts_list_db(
    db_path: str | Path, limit: int = 20, min_confidence: float = 0.0
) -> list[dict]:
    """facts_list with connection lifecycle managed."""
    from infra.memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_facts_schema(conn)
    try:
        return facts_list(conn, limit=limit, min_confidence=min_confidence)
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
