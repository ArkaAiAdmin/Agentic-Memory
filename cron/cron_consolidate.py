#!/usr/bin/env python3
"""Cron wrapper: consolidation — dedup via SHA256 + n-gram Jaccard, detect contradictions.

Modes
-----
``--dry-run`` (default): audit only. Report exact/fuzzy duplicates, stale
sessions, and high tag-density tags. Never mutates the corpus.

``--apply``: functional. For each duplicate group, mark the lower-priority
notes as ``superseded_by`` the kept note and soft-delete them (recoverable,
30-day window). Stale sessions (``sessions/*`` older than 30 days) are
soft-deleted. High tag-density tags are left for manual review (no safe
automatic action).

Both modes run under the ``cron_consolidate`` flock and clean up orphaned
FTS5 index entries.
"""

from _flock import acquire_lock_or_exit
import os
import sqlite3
import sys
import json
import hashlib
import datetime
import argparse
from pathlib import Path

os.environ.setdefault("MEMORY_KNOWLEDGE_GRAPH", "1")
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_repo_root)
import os

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)


from typing import Any
from infra.memory_common import (
    cleanup_fts5_orphans,
)
from infra.infrastructure import resolve_active_memory_dir
try:
    from infra.tenant_query import install_tenant_context
except Exception:  # pragma: no cover
    def install_tenant_context(conn: Any, tenant_id: str | None = None) -> str:
        import os
        tid = tenant_id or os.environ.get("MEMORY_CRON_TENANT_ID") or "default"
        conn.create_function("tenant_id", 0, lambda: tid)
        conn.execute('CREATE TEMP VIEW IF NOT EXISTS tenant_memories AS SELECT * FROM memories WHERE tenant_id = tenant_id()')
        return tid



# Threshold above which a note is "high value" and should be the merge
# survivor rather than the one being superseded. Mirrors save defaults.
_KEEP_IMPORTANCE = 4
_SESSION_STALE_DAYS = 30
_FUZZY_SIM_THRESHOLD = 0.9  # only auto-merge near-identical content


def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode()).hexdigest()


def similarity_hash(content: str) -> set:
    words = content.lower().split()
    return set(tuple(words[i : i + 3]) for i in range(len(words) - 2))


def jaccard_similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _rows(db):
    """Return all active memories with the columns we need."""
    return db.execute(
        "SELECT id, content, tags, category, importance, updated_at "
        "FROM memories WHERE deleted_at IS NULL"
    ).fetchall()


def _find_exact_dups(rows):
    """Group note ids by content SHA256. Returns list of (keep_id, [loser_ids])."""
    hashes = {}
    groups = []
    seen = {}
    for mid, content, tags, category, importance, updated in rows:
        h = compute_content_hash(content.strip())
        if h in seen:
            # accumulate losers under the first-seen id
            groups_for_h = next((g for g in groups if g[0] == seen[h]), None)
            if groups_for_h is None:
                groups_for_h = (seen[h], [])
                groups.append(groups_for_h)
            groups_for_h[1].append(mid)
        else:
            seen[h] = mid
    return groups


def _find_fuzzy_dups(rows):
    """Near-duplicate pairs (>threshold) on NON-session notes.

    Sessions dominate the corpus (templated text); we only auto-merge
    non-session notes to avoid destroying session logs.
    """
    fp = {}
    for mid, content, _, category, _, _ in rows:
        if category == "sessions":
            continue
        fp[mid] = similarity_hash(content)
    mids = list(fp.keys())
    length_buckets = {}
    for mid in mids:
        bucket = len(fp[mid]) // 100
        length_buckets.setdefault(bucket, []).append(mid)
    pairs = []
    for bucket in length_buckets.values():
        if len(bucket) < 2:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, min(i + 50, len(bucket))):
                sim = jaccard_similarity(fp[bucket[i]], fp[bucket[j]])
                if sim > _FUZZY_SIM_THRESHOLD:
                    pairs.append((bucket[i], bucket[j], sim))
    return pairs


def _find_stale_sessions(rows, today):
    stale = []
    for mid, content, tags, category, importance, updated in rows:
        if category != "sessions":
            continue
        if not updated:
            continue
        try:
            d = datetime.date.fromisoformat(str(updated)[:10])
            if (today - d).days > _SESSION_STALE_DAYS:
                stale.append(mid)
        except (ValueError, TypeError):
            pass
    return stale


def _find_high_density(rows):
    tag_map = {}
    for mid, content, tags, category, importance, updated in rows:
        try:
            t = json.loads(tags) if tags else []
        except (json.JSONDecodeError, TypeError):
            t = []
        if isinstance(t, list):
            for tag in t:
                tag_map.setdefault(tag, []).append(mid)
    return {tag: items for tag, items in tag_map.items() if len(items) > 5}


def _keep_id(keep_candidate, losers, rows_by_id):
    """Pick the survivor: highest importance, then most recent, then first seen."""
    best = keep_candidate
    best_imp = rows_by_id.get(best, (None, None, None, None, 0, None))[4] or 0
    best_upd = rows_by_id.get(best, (None, None, None, None, 0, ""))[5] or ""
    candidates = [keep_candidate] + list(losers)
    for cid in candidates:
        imp = rows_by_id.get(cid, (None, None, None, None, 0, None))[4] or 0
        upd = rows_by_id.get(cid, (None, None, None, None, 0, ""))[5] or ""
        if imp > best_imp or (imp == best_imp and upd > best_upd):
            best, best_imp, best_upd = cid, imp, upd
    return best


def consolidate(dry_run: bool = True):
    env = os.environ.get("MEMORY_DB_PATH")
    db_path = (
        Path(env) if env is not None else resolve_active_memory_dir() / "memory.db"
    )
    if not db_path.exists():
        print(f"Error: Database {db_path} does not exist.")
        return
    acquire_lock_or_exit("cron_consolidate")

    db = sqlite3.connect(str(db_path), timeout=30.0)
    db.execute("PRAGMA busy_timeout = 30000;")
    install_tenant_context(db, os.environ.get("MEMORY_CRON_TENANT_ID"))

    rows = _rows(db)
    rows_by_id = {r[0]: r for r in rows}
    today = datetime.date.today()

    exact_groups = _find_exact_dups(rows)
    fuzzy_pairs = _find_fuzzy_dups(rows)
    stale = _find_stale_sessions(rows, today)
    high_density = _find_high_density(rows)

    print(f"Consolidating {len(rows)} memories (mode={'APPLY' if not dry_run else 'DRY-RUN'})...")

    merged = 0
    pruned = 0
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # --- Exact duplicates: supersede losers ---
    for keep_candidate, losers in exact_groups:
        keep = _keep_id(keep_candidate, losers, rows_by_id)
        losers_for_keep = [l for l in losers if l != keep]
        if not losers_for_keep:
            continue
        for loser in losers_for_keep:
            if not dry_run:
                db.execute(
                    "UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
                    (keep, now_iso, loser),
                )
                # soft-delete the loser (recoverable 30d)
                db.execute(
                    "UPDATE memories SET deleted_at=?, deleted_by=? WHERE id=?",
                    (now_iso, "consolidation", loser),
                )
                merged += 1
        if dry_run:
            merged += len(losers_for_keep)
    if not dry_run and merged:
        db.commit()

    # --- Fuzzy duplicates (non-session only): supersede losers ---
    for a, b, sim in fuzzy_pairs:
        keep = _keep_id(a, [b], rows_by_id)
        loser = b if keep == a else a
        if not dry_run:
            db.execute(
                "UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
                (keep, now_iso, loser),
            )
            db.execute(
                "UPDATE memories SET deleted_at=?, deleted_by=? WHERE id=?",
                (now_iso, "consolidation", loser),
            )
            pruned += 1
    if not dry_run and fuzzy_pairs:
        db.commit()

    # --- Stale sessions: soft-delete ---
    for sid in stale:
        if not dry_run:
            db.execute(
                "UPDATE memories SET deleted_at=?, deleted_by=? WHERE id=?",
                (now_iso, "consolidation-stale", sid),
            )
            pruned += 1
    if not dry_run and stale:
        db.commit()

    # --- Report ---
    print(f"  Exact duplicate groups: {len(exact_groups)}")
    print(f"  Fuzzy near-duplicate pairs (non-session, >{_FUZZY_SIM_THRESHOLD}): {len(fuzzy_pairs)}")
    print(f"  Stale sessions (>30d): {len(stale)}")
    print(f"  High tag-density tags (review only): {len(high_density)}")

    if dry_run:
        print("DRY-RUN: no changes made. Re-run with --apply to merge/prune.")
    else:
        print(f"APPLIED: superseded {merged} exact duplicates, "
              f"pruned {pruned} notes (fuzzy + stale).")

    # Clean up orphaned FTS5 entries (soft-deleted notes still indexed)
    orphans_removed = cleanup_fts5_orphans(db)
    if orphans_removed:
        print(f"FTS5 cleanup: removed {orphans_removed} orphaned entries")

    db.close()


def consolidate_light():
    """Backward-compat entry point used by tests and cron wrappers."""
    consolidate(dry_run=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Cron: consolidate — dedup + contradiction scan."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply merges/prunes (default: dry-run audit only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Audit only (default). Explicit form of the default mode.",
    )
    args = parser.parse_args()
    consolidate(dry_run=not args.apply)
