"""Scheduled KG graph analytics cron.

CHANGE 3 (also closes CHANGE 10): global graph analytics — PageRank,
betweenness centrality, community detection, and snapshot capture — used to
be recomputed on *every save* (kg_db.py called update_graph_analytics in the
save path).  That made each write O(V*(V+E)) and was a consistent tail-latency
source.  These are now off the write path and maintained here on a schedule
(daily by default; see cron/jobs.py `kg_analytics`).

The save path (kg_db.persist_kg_triples) now only writes local extraction
stats and returns.  Centrality scores are still read by search/tiers, they
are just refreshed by this job instead of per-write.

This script is idempotent and best-effort: any single analytics step that
fails is logged and skipped so one bad step never blocks the others or the
save path (which no longer depends on it).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from _flock import acquire_lock_or_exit
from infra.infrastructure import resolve_active_memory_dir
from infra.log import setup_logging

logger = logging.getLogger("cron_kg_analytics")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_db_path() -> Path:
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env)
    active_dir = resolve_active_memory_dir()
    return active_dir / "memory.db"


def compute_communities(conn: sqlite3.Connection) -> dict[int, int]:
    """Connected-components community labelling over active edges.

    Cheap O(V + E) union-find; sufficient for memory-scale graphs where
    "community" means "connected knowledge cluster".  Returns {entity_id: community_id}.
    """
    nodes = [r[0] for r in conn.execute("SELECT id FROM kg_entities").fetchall()]
    parent: dict[int, int] = {nid: nid for nid in nodes}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for src, tgt in conn.execute(
        "SELECT source_id, target_id FROM kg_edges WHERE invalid_at IS NULL OR invalid_at = ''"
    ).fetchall():
        if src in parent and tgt in parent:
            union(src, tgt)

    labels: dict[int, int] = {}
    for nid in nodes:
        labels[nid] = find(nid)
    # Renumber communities to dense 0..k-1 for stable ids.
    remap: dict[int, int] = {}
    out: dict[int, int] = {}
    for nid in nodes:
        root = labels[nid]
        if root not in remap:
            remap[root] = len(remap)
        out[nid] = remap[root]
    return out


def capture_snapshot(conn: sqlite3.Connection, communities: dict[int, int]) -> int:
    """Append a row to graph_snapshots with entity/edge counts + top centralities."""
    now = datetime.now(timezone.utc).timestamp()
    entity_count = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
    edge_count = conn.execute(
        "SELECT COUNT(*) FROM kg_edges WHERE invalid_at IS NULL OR invalid_at = ''"
    ).fetchone()[0]
    community_count = len(set(communities.values())) if communities else 0

    rows = conn.execute(
        "SELECT name, centrality FROM kg_entities WHERE centrality IS NOT NULL ORDER BY centrality DESC LIMIT 10"
    ).fetchall()
    top_entities = json.dumps([{"name": r[0], "centrality": round(r[1], 9)} for r in rows])

    avg_centrality = 0.0
    if entity_count:
        total = conn.execute(
            "SELECT COALESCE(SUM(centrality), 0.0) FROM kg_entities"
        ).fetchone()[0]
        avg_centrality = total / entity_count

    prev = conn.execute(
        "SELECT top_entities FROM graph_snapshots ORDER BY captured_at DESC LIMIT 1"
    ).fetchone()
    prev_names: set[str] = set()
    if prev and prev[0]:
        try:
            prev_names = {e["name"] for e in json.loads(prev[0])}
        except (ValueError, KeyError, TypeError):
            prev_names = set()

    cur_names = {r[0] for r in conn.execute("SELECT name FROM kg_entities").fetchall()}
    new_entities = json.dumps(sorted(cur_names - prev_names))
    removed_entities = json.dumps(sorted(prev_names - cur_names))

    cur = conn.execute(
        """INSERT INTO graph_snapshots
           (captured_at, entity_count, edge_count, community_count, avg_centrality, top_entities, new_entities, removed_entities)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            entity_count,
            edge_count,
            community_count,
            round(avg_centrality, 9),
            top_entities,
            new_entities,
            removed_entities,
        ),
    )
    return cur.lastrowid or 0


def run_analytics(db_path: Path) -> dict[str, Any]:
    import sqlite3 as _sql

    from kg.graph_analytics import update_betweenness, update_graph_analytics

    conn = _sql.connect(str(db_path))
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        stats: dict[str, Any] = {}

        # 1. PageRank centrality (kg_entities.pagerank)
        try:
            pr = update_graph_analytics(conn)
            stats["pagerank"] = pr
        except Exception as exc:  # best-effort
            logger.exception("PageRank update failed: %s", exc)
            stats["pagerank"] = {"error": str(exc)}

        # 2. Betweenness centrality (kg_entities.betweenness)
        try:
            bw = update_betweenness(conn)
            stats["betweenness"] = bw
        except Exception as exc:
            logger.exception("Betweenness update failed: %s", exc)
            stats["betweenness"] = {"error": str(exc)}

        # 3. Community detection (kg_entities.community_id)
        communities: dict[int, int] = {}
        try:
            communities = compute_communities(conn)
            for entity_id, cid in communities.items():
                conn.execute(
                    "UPDATE kg_entities SET community_id = ? WHERE id = ?",
                    (cid, entity_id),
                )
            stats["communities"] = {"community_count": len(set(communities.values()))}
        except Exception as exc:
            logger.exception("Community detection failed: %s", exc)
            stats["communities"] = {"error": str(exc)}

        # 4. Snapshot capture
        try:
            snap_id = capture_snapshot(conn, communities)
            stats["snapshot"] = {"id": snap_id}
        except Exception as exc:
            logger.exception("Snapshot capture failed: %s", exc)
            stats["snapshot"] = {"error": str(exc)}

        conn.commit()
        return stats
    finally:
        conn.close()


def main() -> int:
    import argparse

    acquire_lock_or_exit("cron_kg_analytics")

    parser = argparse.ArgumentParser(description="Scheduled KG graph analytics")
    parser.add_argument("--dry-run", action="store_true", help="Resolve DB + report only")
    parser.add_argument(
        "--log-file",
        default=None,
        help="Path to JSON log file (default: memory/kg-analytics-cron.log)",
    )
    args = parser.parse_args()

    setup_logging("cron_kg_analytics", level="INFO", fmt="%(asctime)s [%(levelname)s] %(message)s")

    db_path = _resolve_db_path()
    if not db_path.exists():
        logger.error("No memory.db at %s", db_path)
        sys.exit(1)

    logger.info("=== KG analytics starting (db=%s) ===", db_path)
    if args.dry_run:
        logger.info("dry-run: skipping analytics")
        return 0

    stats = run_analytics(db_path)
    logger.info(
        "KG analytics: pagerank=%s betweenness=%s communities=%s snapshot=%s",
        stats.get("pagerank"),
        stats.get("betweenness"),
        stats.get("communities"),
        stats.get("snapshot"),
    )

    log_file = (
        Path(args.log_file) if args.log_file else db_path.parent / "kg-analytics-cron.log"
    )
    try:
        with open(log_file, "a") as f:
            f.write(
                json.dumps(
                    {"captured_at": datetime.now(timezone.utc).isoformat(), "stats": stats}
                )
                + "\n"
            )
    except OSError as exc:
        logger.warning("Failed to write log file: %s", exc)

    logger.info("=== KG analytics complete ===")
    return 0


if __name__ == "__main__":
    main()
