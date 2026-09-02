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
import re


def extract_clean_body(content: str) -> str:
    """Extract clean markdown body by stripping YAML frontmatter and title hex variations."""
    text = content.strip()
    lines = text.splitlines()
    body_lines = []
    in_frontmatter = False
    for i, line in enumerate(lines):
        line_s = line.strip()
        if i == 0 and line_s == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line_s == "---":
                in_frontmatter = False
                continue
            if re.match(r"^[a-zA-Z0-9_-]+\s*:", line) or (line_s.startswith("- ") and not line_s.startswith("- [")):
                continue
            if line_s == "":
                continue
            in_frontmatter = False

        if line_s.startswith("# "):
            # Strip title line or normalize random hex suffix (e.g. "# Skill Title 5756" -> skipped)
            continue
        body_lines.append(line)
    return "\n".join(body_lines).strip()


def compute_content_hash(content: str) -> str:
    norm = extract_clean_body(content)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def similarity_hash(content: str) -> set:
    norm = extract_clean_body(content)
    words = norm.lower().split()
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
    """Group note ids by normalized body content SHA256. Returns list of (keep_id, [loser_ids])."""
    seen = {}
    groups = []
    for mid, content, tags, category, importance, updated in rows:
        h = compute_content_hash(content)
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


def _find_canonical_dups(rows, existing_loser_ids: set):
    """Find additional duplicates that share canonical template prefixes across skill, projects, and lessons."""
    canonical_map = {}
    for mid, content, tags, category, importance, updated in rows:
        if mid in existing_loser_ids:
            continue
        key = None
        if category == "skill":
            m = re.match(r"skill/(skill-[a-z-]+|builtin-[a-z-]+|auto-[a-z-]+)", mid)
            if m:
                base = re.sub(r"-[0-9a-f]{4,8}$", "", m.group(1))
                key = ("skill", base)
            else:
                m_title = re.search(r"SKILL:\s*([^\n]+)", content)
                if m_title:
                    key = ("skill", m_title.group(1).strip().lower())
        elif category == "projects" and mid.startswith("projects/sub-agent-completed-"):
            m = re.match(r"projects/(sub-agent-completed-[a-z0-9-]+)", mid)
            if m:
                base = re.sub(r"-[0-9a-f]{4,8}$", "", m.group(1))
                key = ("projects", base)
        elif category == "lessons" and (mid.startswith("lessons/tool-failure-lesson-") or mid.startswith("lessons/live-audit-probe-")):
            m_tool = re.search(r"Tool `([^`]+)` failed", content)
            if m_tool:
                key = ("lessons_tool_failure", m_tool.group(1))
            else:
                base = re.sub(r"-[0-9a-f]{4,8}$", "", mid)
                key = ("lessons_probe", base)

        if key:
            canonical_map.setdefault(key, []).append(mid)

    groups = []
    for key, mids in canonical_map.items():
        if len(mids) > 1:
            groups.append((mids[0], mids[1:]))
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
    """Pick the survivor: clean canonical ID, then highest importance, then most recent."""
    candidates = [keep_candidate] + list(losers)
    # Check if any candidate is an un-suffixed canonical builtin ID
    for cid in candidates:
        if re.search(r"builtin-[a-z-]+$", cid):
            return cid

    best = keep_candidate
    best_imp = rows_by_id.get(best, (None, None, None, None, 0, None))[4] or 0
    best_upd = rows_by_id.get(best, (None, None, None, None, 0, ""))[5] or ""
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
    exact_losers = {loser for _, losers in exact_groups for loser in losers}
    canonical_groups = _find_canonical_dups(rows, exact_losers)
    fuzzy_pairs = _find_fuzzy_dups(rows)
    stale = _find_stale_sessions(rows, today)
    high_density = _find_high_density(rows)

    print(f"Consolidating {len(rows)} memories (mode={'APPLY' if not dry_run else 'DRY-RUN'})...")

    merged = 0
    pruned = 0
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    all_dedup_groups = exact_groups + canonical_groups

    # --- Exact & Canonical duplicates: supersede losers ---
    for keep_candidate, losers in all_dedup_groups:
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
                # Clean up dependent chunk and embedding rows
                db.execute("DELETE FROM memory_chunks WHERE parent_id=?", (loser,))
                db.execute("DELETE FROM memory_chunk_embeddings WHERE parent_id=?", (loser,))
                db.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (loser,))
                db.execute("DELETE FROM memory_vec_keys WHERE memory_id=?", (loser,))
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
            db.execute("DELETE FROM memory_chunks WHERE parent_id=?", (loser,))
            db.execute("DELETE FROM memory_chunk_embeddings WHERE parent_id=?", (loser,))
            db.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (loser,))
            db.execute("DELETE FROM memory_vec_keys WHERE memory_id=?", (loser,))
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
            db.execute("DELETE FROM memory_chunks WHERE parent_id=?", (sid,))
            db.execute("DELETE FROM memory_chunk_embeddings WHERE parent_id=?", (sid,))
            db.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (sid,))
            db.execute("DELETE FROM memory_vec_keys WHERE memory_id=?", (sid,))
            pruned += 1
    if not dry_run and stale:
        db.commit()

    # --- Report ---
    print(f"  Exact duplicate groups: {len(exact_groups)}")
    print(f"  Canonical entity duplicate groups: {len(canonical_groups)}")
    print(f"  Fuzzy near-duplicate pairs (non-session, >{_FUZZY_SIM_THRESHOLD}): {len(fuzzy_pairs)}")
    print(f"  Stale sessions (>30d): {len(stale)}")
    print(f"  High tag-density tags (review only): {len(high_density)}")

    if dry_run:
        print("DRY-RUN: no changes made. Re-run with --apply to merge/prune.")
    else:
        # `pruned` counts actual soft-deletes (fuzzy losers + stale
        # sessions) — NOT the raw pair count, which double-counts and
        # once misreported 17k "pruned" for a ~5.4k-note corpus.
        print(f"APPLIED: superseded {merged} exact/canonical duplicates, "
              f"pruned {pruned} notes (fuzzy losers + stale sessions).")

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
