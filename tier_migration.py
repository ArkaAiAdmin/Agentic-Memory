#!/usr/bin/env python3
"""File-system lifecycle management for agentic-memory."""
import logging

logger = logging.getLogger(__name__)

# P0-9 fix (2026-06-23): renamed the file-system tier categories from
# "hot/warm/cold" to "fresh/consolidated/archived" to disambiguate
# from the DB-column tier model (hot/warm/cold) in ``assign_tier``.
# The two systems operate on different artifacts (DB row vs .md file)
# with different goals (search ranking vs storage cost) and previously
# shared names that caused confusion for maintainers.
#
# File-system stages:
#   Fresh        (<7 days):    Full-content files in sessions/, indexed at full resolution.
#   Consolidated  (7-90 days): Session logs consolidated into lessons/ summaries.
#   Archived     (>90 days):   Archived to archive/ as compressed bundles, excluded from FTS5.
# Pinned files (pinned: true in frontmatter) are never migrated or archived.

import os
import re
import sys
import json
import shutil
import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path.home() / ".config" / "agentic-memory"))
from memory_common import parse_frontmatter, find_project_root, atomic_write
from memory_config import get_memory_paths


def get_note_date(metadata: dict, file_path: Path) -> datetime.date:
    """Return the note's date from frontmatter 'created' field, falling back to mtime."""
    created = metadata.get("created")
    if created:
        try:
            # Handle both date-only and datetime formats
            exp_str = str(created)
            if "T" in exp_str:
                return datetime.datetime.fromisoformat(exp_str).date()
            else:
                return datetime.date.fromisoformat(exp_str)
        except (ValueError, TypeError):
            pass
    try:
        # M26 fix: use UTC consistently. The previous call used the local
        # timezone, which made this function disagree with assign_tier
        # (which uses datetime.now(timezone.utc)) on the same wall-clock
        # second for any note whose mtime straddled midnight.
        return datetime.datetime.fromtimestamp(
            file_path.stat().st_mtime, tz=datetime.timezone.utc
        ).date()
    except (OSError, ValueError, AttributeError):
        return datetime.datetime.now(tz=datetime.timezone.utc).date()


def is_pinned(metadata: dict) -> bool:
    """Check if a note is pinned and should never be migrated."""
    return bool(metadata.get("pinned", False))


def consolidate_consolidated_sessions(memory_dir: Path, dry_run: bool = False):
    """Consolidate session logs (7-90 days old) into lesson summaries.

    P0-9 fix (2026-06-23): renamed from ``consolidate_warm_sessions``
    to use the new file-system stage naming ("fresh/consolidated/
    archived") instead of the DB-tier names ("hot/warm/cold").
    """
    today = datetime.date.today()
    sessions_dir = memory_dir / "sessions"
    lessons_dir = memory_dir / "lessons"
    lessons_dir.mkdir(parents=True, exist_ok=True)

    if not sessions_dir.exists():
        return

    consolidated_count = 0
    for session_file in sessions_dir.glob("*.md"):
        try:
            content = session_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        metadata, body = parse_frontmatter(content)
        note_date = get_note_date(metadata, session_file)
        age_days = (today - note_date).days

        if age_days < 7 or age_days > 90:
            continue

        if is_pinned(metadata):
            continue

        # Create a lesson summary from the session
        title = session_file.stem
        lesson_content = f"""---
created: {datetime.datetime.now().isoformat()}
updated: {datetime.datetime.now().isoformat()}
tags: {json.dumps(metadata.get("tags", []))}
pinned: false
importance: 2
---

# Consolidated Session: {title}

*Consolidated on {today.isoformat()} from session dated {note_date.isoformat()}*

{body}
"""
        lesson_file = lessons_dir / f"{title}.md"
        if not dry_run:
            try:
                temp_file = lesson_file.with_suffix(".md.tmp")
                temp_file.write_text(lesson_content, encoding="utf-8")
                os.replace(str(temp_file), str(lesson_file))
                session_file.unlink()
                consolidated_count += 1
            except Exception as e:
                print(f"  Warning: Failed to consolidate {session_file}: {e}")
        else:
            consolidated_count += 1

    if consolidated_count > 0:
        print(
            f"  Consolidated {consolidated_count} session logs into lessons/ (dry_run={dry_run})"
        )


def archive_cold_files(memory_dir: Path, dry_run: bool = False):
    """Archive cold files (>90 days) to gzip bundles, replacing originals with stubs."""
    import gzip

    today = datetime.date.today()
    archive_dir = memory_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stats: dict[str, Any] = {
        "archived": 0,
        "skipped": 0,
        "pinned_protected": 0,
        "skip_reasons": {
            "not_cold": 0,
            "binary": 0,
            "archive_or_global": 0,
            "already_archived": 0,
        },
        "arc_evictions_recorded": 0,
    }
    bundles: dict[str, Any] = {}

    # P0 fix #4: open the ARC cache up front so each archive can
    # record an eviction in arc_ghosts. Best-effort: a missing or
    # corrupt ARC cache must not break the tier migration. We close
    # the cache in the `finally` so a long migration still gets a
    # clean shutdown.
    arc_cache = None
    if not dry_run:
        try:
            db_path = memory_dir / "memory.db"
            if db_path.exists():
                from arc_cache import ARCCache

                arc_cache = ARCCache(db_path)
        except Exception:
            arc_cache = None
    try:
        for md_file in memory_dir.rglob("*.md"):
            if md_file.name == "MEMORY.md":
                continue
            try:
                rel = md_file.relative_to(memory_dir)
            except ValueError:
                stats["skipped"] += 1
                continue
            if rel.parts[0] in ("archive", "global"):
                stats["skipped"] += 1
                stats["skip_reasons"]["archive_or_global"] += 1
                continue
            # Safety check: binary file detection (null bytes)
            try:
                with open(md_file, "rb") as f:
                    chunk = f.read(1024)
                    if b"\x00" in chunk:
                        stats["skipped"] += 1
                        stats["skip_reasons"]["binary"] += 1
                        continue
            except OSError as exc:
                logger.debug("tier_migration: cannot stat %s: %s", md_file, exc)
                stats["skipped"] += 1
                continue
            try:
                content = md_file.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError):
                stats["skipped"] += 1
                continue
            metadata, body = parse_frontmatter(content)
            if is_pinned(metadata):
                stats["pinned_protected"] += 1
                continue
            # Already archived?
            if metadata.get("archived"):
                stats["skipped"] += 1
                stats["skip_reasons"]["already_archived"] += 1
                continue
            note_date = get_note_date(metadata, md_file)
            age_days = (today - note_date).days
            if age_days <= 90:
                stats["skipped"] += 1
                stats["skip_reasons"]["not_cold"] += 1
                continue
            # Build bundle entry from original body
            tags = metadata.get("tags", [])
            tag_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
            entry = (
                f"# {md_file.stem}\n\n"
                f"Tags: {tag_str}\n"
                f"{body}\n\n"
                f"---\n"
                f"Source: {rel}\n"
                f"Archived: {today.isoformat()}\n"
                f"Age: {age_days} days\n"
            )
            bundle_key = rel.parts[0] if len(rel.parts) > 1 else "misc"
            bundles.setdefault(bundle_key, []).append(entry)
            # Replace original with stub
            if not dry_run:
                stub = (
                    f"---\n"
                    f"archived: true\n"
                    f"created: {metadata.get('created', today.isoformat())}\n"
                    f"tags: {tag_str}\n"
                    f"---\n\n"
                    f"(archived stub)\n"
                )
                md_file.write_text(stub, encoding="utf-8")
            # P0 fix #4: record this eviction in the ARC ghost list so
            # the next compute_eviction_pressure() call sees it.
            # The stem-based id mirrors the `id` column convention
            # used by other memory subsystems (category/file_stem).
            if arc_cache is not None:
                try:
                    memory_id = (
                        str(rel.with_suffix("")).replace("\\", "/").replace("/", "/")
                    )
                    # Use the bundle_key as the tier label (sessions,
                    # lessons, decisions, etc.) so the ARC stats can
                    # show which categories are getting archived.
                    arc_cache.record_eviction(memory_id, bundle_key)
                    stats["arc_evictions_recorded"] += 1
                except Exception:
                    # ARC recording is best-effort; never break the
                    # eviction run on a telemetry error.
                    pass
            stats["archived"] += 1
        # Write gzip bundles
        if not dry_run:
            for bundle_key, entries in bundles.items():
                bundle_file = (
                    archive_dir / f"{bundle_key}_{today.strftime('%Y%m%d')}.md.gz"
                )
                gz_payload = gzip.compress(("\n".join(entries) + "\n").encode("utf-8"))
                atomic_write(bundle_file, gz_payload)
        # Recompute eviction pressure so the next memory_arc_stats
        # call reflects the just-recorded evictions.
        if arc_cache is not None and stats["arc_evictions_recorded"] > 0:
            try:
                arc_cache.compute_eviction_pressure()
            except Exception:
                pass
    finally:
        if arc_cache is not None:
            try:
                arc_cache.close()
            except Exception:
                pass
    return stats


def run_tier_migration(memory_dir: Path, dry_run: bool = False):
    """Run full tier migration: consolidate warm, archive cold."""
    print(f"Running tier migration on {memory_dir}... (dry_run={dry_run})")

    consolidate_consolidated_sessions(memory_dir, dry_run)
    stats = archive_cold_files(memory_dir, dry_run)

    print(f"=== Tier Migration Report {'[DRY RUN]' if dry_run else ''} ===")
    print(f"  Hot  (<7 days):     N/A (unchanged)")
    print(f"  Warm (7-90 days):   Consolidated to lessons/")
    print(
        f"  Cold (>90 days):    {stats['archived']} archived, {stats['skipped']} skipped"
    )
    print(f"  Pinned (protected): {stats['pinned_protected']}")
    if stats.get("arc_evictions_recorded", 0) > 0:
        print(
            f"  ARC evictions:      {stats['arc_evictions_recorded']} (recorded in arc_ghosts)"
        )


def prune_superseded(
    memory_dir: Path, older_than_days: int = 30, dry_run: bool = False
):
    """Prune old superseded notes to gzip bundles, replacing with stubs."""
    import gzip

    today = datetime.date.today()
    archive_dir = memory_dir / "archive"
    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)
    stats: dict[str, Any] = {
        "pruned": 0,
        "skipped": 0,
        "pinned_protected": 0,
        "skip_reasons": {"not_old_enough": 0, "not_superseded": 0, "already_pruned": 0},
    }
    bundles: dict[str, Any] = {}
    for md_file in memory_dir.rglob("*.md"):
        if md_file.name == "MEMORY.md":
            continue
        try:
            rel = md_file.relative_to(memory_dir)
        except ValueError:
            stats["skipped"] += 1
            continue
        if rel.parts[0] in ("archive", "global"):
            stats["skipped"] += 1
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            stats["skipped"] += 1
            continue
        metadata, body = parse_frontmatter(content)
        if is_pinned(metadata):
            stats["skipped"] += 1
            stats["pinned_protected"] += 1
            stats["skip_reasons"]["not_superseded"] += 1
            continue
        # Already pruned?
        if metadata.get("pruned"):
            stats["skipped"] += 1
            stats["skip_reasons"]["already_pruned"] += 1
            continue
        # Must have valid_to and superseded_by
        valid_to_str = metadata.get("valid_to")
        superseded_by = metadata.get("superseded_by")
        if not valid_to_str or not superseded_by:
            stats["skipped"] += 1
            stats["skip_reasons"]["not_superseded"] += 1
            continue
        try:
            valid_to = datetime.date.fromisoformat(str(valid_to_str))
        except (ValueError, TypeError):
            stats["skipped"] += 1
            stats["skip_reasons"]["not_superseded"] += 1
            continue
        age_days = (today - valid_to).days
        if age_days <= older_than_days:
            stats["skipped"] += 1
            stats["skip_reasons"]["not_old_enough"] += 1
            continue
        # Prune: archive body, replace with stub
        tags = metadata.get("tags", [])
        tag_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
        entry = (
            f"# {md_file.stem}\n\n"
            f"Tags: {tag_str}\n"
            f"{body}\n\n"
            f"---\n"
            f"Source: {rel}\n"
            f"Pruned: {today.isoformat()}\n"
            f"Superseded by: {superseded_by}\n"
        )
        bundle_key = f"{rel.parts[0]}_pruned" if len(rel.parts) > 1 else "misc_pruned"
        bundles.setdefault(bundle_key, []).append(entry)
        if not dry_run:
            stub = (
                f"---\n"
                f"pruned: true\n"
                f"pruned_to: archive/{bundle_key}_{today.strftime('%Y%m%d')}.md.gz\n"
                f"pruned_at: {today.isoformat()}\n"
                f"created: {metadata.get('created', today.isoformat())}\n"
                f"tags: {tag_str}\n"
                f"---\n\n"
                f"(pruned stub)\n"
            )
            md_file.write_text(stub, encoding="utf-8")
        stats["pruned"] += 1
    # Write gzip bundles
    if not dry_run:
        for bundle_key, entries in bundles.items():
            bundle_file = archive_dir / f"{bundle_key}_{today.strftime('%Y%m%d')}.md.gz"
            gz_payload = gzip.compress(("\n".join(entries) + "\n").encode("utf-8"))
            atomic_write(bundle_file, gz_payload)
    return stats


def migrate_to_cold(memory_dir: Path, dry_run: bool = False):
    """Migrate cold-tier files (>90 days) to gzip archive bundles.

    Alias for ``archive_cold_files`` to provide a verb-form name
    that pairs naturally with the lifecycle stages:
    ``consolidate_warm_sessions`` / ``migrate_to_cold``.
    """
    return archive_cold_files(memory_dir, dry_run=dry_run)


def assign_tier(importance: int, pinned: bool, last_accessed, created_at) -> str:
    """Assign a hot/warm/cold tier for DB-backed memories.

    M25 fix: this is the DB-column tier model (3 buckets: hot/warm/cold)
    used by the `tier` column in the memories table. It is distinct
    from the file-system tier model at the top of this module (warm = 7
    to 90 days, cold = 90+ days) which governs gzip-on-archive of
    .md files. The two systems coexist because they operate on different
    artifacts (DB row vs .md file) with different goals (search ranking
    vs storage cost).

    DB-tier rules:
      - hot:  pinned OR importance >= 4 OR accessed within 7 days
      - warm: importance >= 3 OR accessed within 30 days
      - cold: everything else
    """
    from datetime import datetime, timezone

    if pinned or importance >= 4:
        return "hot"
    now = datetime.now(timezone.utc)
    ref = last_accessed or created_at
    if ref:
        try:
            ref_ts = datetime.fromisoformat(str(ref))
            if ref_ts.tzinfo is None:
                ref_ts = ref_ts.replace(tzinfo=timezone.utc)
            age_days = (now - ref_ts).total_seconds() / 86400.0
        except (ValueError, TypeError):
            age_days = 999
    else:
        age_days = 999
    if age_days <= 7:
        return "hot"
    if age_days <= 30 or importance >= 3:
        return "warm"
    return "cold"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Tier migration for agentic-memory")
    parser.add_argument(
        "path", nargs="?", help="Path to memory directory (default: current project)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()
    if args.path:
        memory_dir = Path(args.path)
    else:
        _, memory_dir, _ = get_memory_paths()
    run_tier_migration(memory_dir, args.dry_run)


# P0-9 fix (2026-06-23): backward-compat aliases for the renamed
# functions. The old ``hot/warm/cold`` names referred to file-system
# stages; the new ``fresh/consolidated/archived`` names are the
# preferred API. These aliases keep existing callers working.
consolidate_warm_sessions = consolidate_consolidated_sessions
migrate_to_cold = archive_cold_files
