#!/usr/bin/env python3
"""Semantic clustering cron job — assign cluster labels to memories.

Reads all embeddings from memory_embeddings, runs DBSCAN clustering with
cosine distance, and writes cluster labels to each memory's metadata JSON.

The cluster label is stored as ``metadata.cluster_label`` (a string like
``"c-N"`` where N is the cluster ID). Noise points (DBSCAN label -1) are
not stored.

Run weekly:
    venv/bin/python cron/cron_semantic_clusters.py [--db PATH] [--eps 0.3] [--min-samples 2] [--dry-run]

Or via enqueue_task:
    venv/bin/python cron/enqueue_task.py --task-type cron_semantic_clusters

Design
------
- DBSCAN with cosine distance: no pre-specified cluster count, robust to
  noise, finds arbitrarily-shaped clusters.
- eps=0.3: reasonable for high-dim embeddings (adjust via --eps).
- Incremental: re-clusters ALL notes each run (clusters drift over time
  as new notes arrive). The metadata update is per-note and idempotent.
- Avoids sklearn import overhead by importing lazily.
"""

from __future__ import annotations

import argparse
import json
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

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _flock import acquire_lock_or_exit

from infra.infrastructure import resolve_active_memory_dir
from infra.log import setup_logging

logger = setup_logging("cron_semantic_clusters")

CLUSTER_KEY = "cluster_label"


def _get_db_path(cli_override: str | None) -> Path:
    if cli_override:
        return Path(cli_override)
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env)
    return resolve_active_memory_dir() / "memory.db"


def _load_embeddings(conn) -> dict[str, bytes]:
    """Return {memory_id: embedding_blob} for all notes with embeddings."""
    rows = conn.execute(
        "SELECT e.memory_id, e.embedding FROM memory_embeddings e "
        "JOIN memories m ON m.id = e.memory_id "
        "WHERE m.category IN ('lessons', 'decisions', 'projects')"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _cluster_embeddings(
    memory_ids: list[str], blobs: list[bytes], eps: float, min_samples: int
) -> dict[str, str]:
    """Run DBSCAN with cosine distance, return {memory_id: cluster_id_string}.

    Returns only non-noise assignments. Noise points (label -1) are
    excluded.
    """
    import numpy as np
    from sklearn.cluster import DBSCAN
    from sklearn.metrics.pairwise import cosine_distances

    vectors = np.array([np.frombuffer(b, dtype=np.float32) for b in blobs])
    dist_matrix = cosine_distances(vectors)
    clustering = DBSCAN(eps=eps, min_samples=min_samples, metric="precomputed")
    labels = clustering.fit_predict(dist_matrix)

    result: dict[str, str] = {}
    for mid, label in zip(memory_ids, labels):
        if label >= 0:
            result[mid] = f"c-{label}"
    return result


def _write_cluster_labels(conn, assignments: dict[str, str]) -> int:
    """Update metadata.cluster_label for each memory. Returns count updated."""
    updated = 0
    for memory_id, cluster_label in assignments.items():
        row = conn.execute(
            "SELECT metadata FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            continue
        meta = json.loads(row[0]) if row[0] else {}
        meta[CLUSTER_KEY] = cluster_label
        conn.execute(
            "UPDATE memories SET metadata = ? WHERE id = ?",
            (json.dumps(meta), memory_id),
        )
        updated += 1
    conn.commit()
    return updated


def _drop_stale_labels(conn, active_memory_ids: set[str], dry_run: bool) -> int:
    """Remove cluster_label from memories no longer clustered (or deleted).

    Stale labels happen when a note was previously clustered but the
    current run didn't assign it (e.g., it became a noise point or was
    deleted).
    """
    dropped = 0
    rows = conn.execute(
        "SELECT id, metadata FROM memories WHERE metadata LIKE '%cluster_label%'"
    ).fetchall()
    for mid, meta_json in rows:
        if mid in active_memory_ids:
            continue
        meta = json.loads(meta_json) if meta_json else {}
        if CLUSTER_KEY in meta:
            if not dry_run:
                del meta[CLUSTER_KEY]
                conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(meta), mid))
            dropped += 1
    if not dry_run:
        conn.commit()
    return dropped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Semantic clustering of memories.")
    parser.add_argument("--db", default=None, help="Path to memory.db")
    parser.add_argument("--eps", type=float, default=0.3, help="DBSCAN eps (cosine distance), default 0.3")
    parser.add_argument("--min-samples", type=int, default=2, help="DBSCAN min_samples, default 2")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args(argv)

    db_path = _get_db_path(args.db)
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        return 1

    acquire_lock_or_exit("cron_semantic_clusters")

    t0 = time.time()

    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path), timeout=10)
        install_tenant_context(conn, os.environ.get("MEMORY_CRON_TENANT_ID"))

        try:
            embeddings = _load_embeddings(conn)
            if not embeddings:
                print("semantic_clusters: no embeddings found, skipping")
                return 0

            memory_ids = list(embeddings.keys())
            blobs = [embeddings[mid] for mid in memory_ids]

            if len(memory_ids) < args.min_samples:
                print(f"semantic_clusters: only {len(memory_ids)} notes (< min_samples), skipping")
                return 0

            assignments = _cluster_embeddings(memory_ids, blobs, args.eps, args.min_samples)

            prefix = "[DRY RUN] " if args.dry_run else ""
            if args.dry_run:
                n_clusters = len(set(assignments.values()))
                n_assigned = len(assignments)
                n_noise = len(memory_ids) - n_assigned
                print(
                    f"{prefix}semantic_clusters: {n_assigned} assigned to "
                    f"{n_clusters} clusters, {n_noise} noise (of {len(memory_ids)} total)"
                )
            else:
                updated = _write_cluster_labels(conn, assignments)
                # Keep noise-point labels as stale so they won't reappear
                # until the note's embedding changes. But drop labels for
                # notes that were previously clustered but now have no
                # embedding at all (deleted).
                active_set = set(memory_ids)
                dropped = _drop_stale_labels(conn, active_set, dry_run=False)
                elapsed = time.time() - t0
                print(
                    f"semantic_clusters: {updated} labels written, {dropped} stale "
                    f"labels dropped in {elapsed:.2f}s ({len(memory_ids)} total)"
                )
        finally:
            conn.close()
    except Exception as e:
        logger.warning("main failed: %s", e)
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
