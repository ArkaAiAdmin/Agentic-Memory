#!/usr/bin/env python3
"""Cron script: Pre-compute answer rerank scores for hot memo IDs.

Scans recent memories (last 30 days) and pre-computes answer-level
rerank scores against their most common queries.  This keeps the
online latency tight by avoiding snippet extraction + CE scoring
at query time.

Also cleans up stale cache entries older than 7 days.
"""

from __future__ import annotations

import argparse
import logging
import os
try:
    from infra.tenant_query import install_tenant_context
except Exception:  # pragma: no cover
    def install_tenant_context(conn, tenant_id=None):
        import os
        tid = tenant_id or os.environ.get("MEMORY_CRON_TENANT_ID") or "default"
        conn.create_function("tenant_id", 0, lambda: tid)
        conn.execute('CREATE TEMP VIEW IF NOT EXISTS tenant_memories AS SELECT * FROM memories WHERE tenant_id = tenant_id()')
        return tid

import sqlite3
import sys
import time
import traceback
from pathlib import Path

# Bootstrap path
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from _flock import acquire_lock_or_exit
from infra.memory_common import GLOBAL_MEM_DIR
from infra.log import setup_logging

logger = setup_logging("cron_answer_rerank")

DEFAULT_DB_PATH = str(GLOBAL_MEM_DIR / "memory.db")


def get_recent_memories(conn: sqlite3.Connection, days: int = 30, limit: int = 100) -> list[tuple[str, str]]:
    """Return (memory_id, content) for recently accessed memories."""
    cutoff = time.time() - (days * 86400)
    rows = conn.execute(
        "SELECT id, content FROM memories "
        "WHERE deleted_at IS NULL AND updated_at > ? "
        "ORDER BY access_count DESC, updated_at DESC "
        "LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    return [(r[0], r[1]) for r in rows if r[0] and r[1]]


def get_popular_queries(conn: sqlite3.Connection, limit: int = 20) -> list[str]:
    """Return the most common recent search queries."""
    rows = conn.execute(
        "SELECT DISTINCT query_id FROM memory_search_interaction "
        "WHERE ts > ? "
        "ORDER BY ts DESC LIMIT ?",
        (time.time() - (30 * 86400), limit * 10),
    ).fetchall()
    # Extract unique queries from query_ids (heuristic: use the action field)
    queries = []
    seen = set()
    for row in rows:
        qid = row[0]
        if qid and qid not in seen:
            seen.add(qid)
            # Use query_id as a proxy for the query text
            queries.append(qid)
        if len(queries) >= limit:
            break
    return queries


def main():
    parser = argparse.ArgumentParser(description="Pre-compute answer rerank scores.")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Path to database")
    parser.add_argument("--days", type=int, default=30, help="Look back N days for memories")
    parser.add_argument("--limit", type=int, default=50, help="Max memories to pre-compute")
    args = parser.parse_args()

    lock_file = Path(args.db).parent / "cron_answer_rerank.lock"
    acquire_lock_or_exit(str(lock_file))

    t0 = time.time()
    try:
        conn = sqlite3.connect(args.db)
        install_tenant_context(conn, os.environ.get("MEMORY_CRON_TENANT_ID"))

        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")

        # Ensure cache table exists
        from search.answer_rerank import _ensure_cache_schema, clear_stale_cache

        _ensure_cache_schema(conn)

        # Clean stale cache
        cleared = clear_stale_cache(conn, max_age_days=7)
        logger.info("Cleared %d stale cache entries", cleared)

        # Get recent memories and popular queries
        memories = get_recent_memories(conn, days=args.days, limit=args.limit)
        queries = get_popular_queries(conn, limit=20)

        if not memories or not queries:
            logger.info("No memories or queries to pre-compute")
            print("answer_rerank: nothing to pre-compute")
            conn.close()
            return

        # Load cross-encoder for scoring
        model = None
        try:
            from sentence_transformers import CrossEncoder

            model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            logger.info("Loaded cross-encoder for pre-computation")
        except Exception as e:
            logger.warning("Could not load cross-encoder: %s (using keyword fallback)", e)

        # Pre-compute
        from search.answer_rerank import precompute_for_memory

        total_scored = 0
        for mid, content in memories:
            scored = precompute_for_memory(conn, mid, content, queries, model=model)
            total_scored += scored

        conn.commit()
        conn.close()

        elapsed = time.time() - t0
        logger.info(
            "Pre-computed %d scores for %d memories × %d queries in %.2fs",
            total_scored, len(memories), len(queries), elapsed,
        )
        print(f"answer_rerank: scored={total_scored} memories={len(memories)} queries={len(queries)}")
    except Exception:
        logger.error("Script failed with exception:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
