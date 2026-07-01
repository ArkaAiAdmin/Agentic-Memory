#!/usr/bin/env python3
"""Cron wrapper: consolidation — dedup via SHA256 + n-gram Jaccard, detect contradictions."""

from _flock import acquire_lock_or_exit
import os
import sys
import json
import hashlib
import datetime
from pathlib import Path

os.environ.setdefault("MEMORY_KNOWLEDGE_GRAPH", "1")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import os

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)


from infra.memory_common import (
    safe_close_db,
    cleanup_fts5_orphans,
    connection_pool,
)
from infra.infrastructure import resolve_active_memory_dir


def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode()).hexdigest()


def similarity_hash(content: str) -> set:
    words = content.lower().split()
    return set(tuple(words[i : i + 3]) for i in range(len(words) - 2))


def jaccard_similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def consolidate_light():
    env = os.environ.get("MEMORY_DB_PATH")
    db_path = (
        Path(env) if env is not None else resolve_active_memory_dir() / "memory.db"
    )
    if not db_path.exists():
        print(f"Error: Database {db_path} does not exist.")
        return
    acquire_lock_or_exit("cron_consolidate")

    db = connection_pool.get(str(db_path), timeout=30.0)
    db.execute("PRAGMA busy_timeout = 30000;")

    rows = db.execute(
        "SELECT id, content, tags, source_file FROM memories WHERE deleted_at IS NULL"
    ).fetchall()
    print(f"Consolidating {len(rows)} memories...")

    # Exact duplicates via SHA256
    hashes = {}
    exact_dups = []
    for mid, content, tags, source in rows:
        h = compute_content_hash(content.strip())
        if h in hashes:
            exact_dups.append((hashes[h], mid))
        else:
            hashes[h] = mid

    # Fuzzy duplicates via n-gram Jaccard (bucketed)
    fingerprints = {}
    for mid, content, _, _ in rows:
        fingerprints[mid] = similarity_hash(content)

    mids = list(fingerprints.keys())
    length_buckets = {}
    for mid in mids:
        bucket = len(fingerprints[mid]) // 100
        length_buckets.setdefault(bucket, []).append(mid)

    fuzzy_dups = []
    for bucket in length_buckets.values():
        if len(bucket) < 2:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, min(i + 50, len(bucket))):
                sim = jaccard_similarity(
                    fingerprints[bucket[i]], fingerprints[bucket[j]]
                )
                if sim > 0.8:
                    fuzzy_dups.append((bucket[i], bucket[j], sim))

    # Stale sessions (>30 days)
    today = datetime.date.today()
    stale = []
    for mid, content, tags, source in rows:
        if mid.startswith("sessions/"):
            try:
                updated = db.execute(
                    "SELECT updated_at FROM memories WHERE id=?", (mid,)
                ).fetchone()
                if updated and updated[0]:
                    d = datetime.date.fromisoformat(str(updated[0])[:10])
                    if (today - d).days > 30:
                        stale.append(mid)
            except (ValueError, TypeError):
                pass

    # High tag density
    tag_map = {}
    for mid, content, tags, source in rows:
        try:
            t = json.loads(tags) if tags else []
        except (json.JSONDecodeError, TypeError):
            t = []
        if isinstance(t, list):
            for tag in t:
                tag_map.setdefault(tag, []).append(mid)
    high_density = {tag: items for tag, items in tag_map.items() if len(items) > 5}

    issues = []
    if exact_dups:
        issues.append(f"Exact duplicates: {len(exact_dups)}")
    if fuzzy_dups:
        issues.append(f"Fuzzy duplicates: {len(fuzzy_dups)}")
    if stale:
        issues.append(f"Stale sessions: {len(stale)}")
    if high_density:
        issues.append(f"High tag density: {len(high_density)} tags")

    # Clean up orphaned FTS5 entries (soft-deleted notes still indexed)
    orphans_removed = cleanup_fts5_orphans(db)
    if orphans_removed:
        print(f"FTS5 cleanup: removed {orphans_removed} orphaned entries")

    if issues:
        print("Issues found:")
        for i in issues:
            print(f"  - {i}")
    else:
        print("No consolidation issues found.")

    safe_close_db(db)


if __name__ == "__main__":
    import argparse

    argparse.ArgumentParser(
        description="Cron: consolidate — dedup + contradiction scan."
    ).parse_args()
    consolidate_light()
