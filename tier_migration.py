#!/usr/bin/env python3
"""
Hot/Warm/Cold data lifecycle management for agentic-memory.

Temperature tiers:
  Hot   (<7 days):  Full-content files in sessions/, indexed at full resolution.
  Warm  (7-90 days): Session logs consolidated into lessons/ summaries.
  Cold  (>90 days): Archived to archive/ as compressed bundles, excluded from FTS5.

Pinned files (pinned: true in frontmatter) are never migrated or archived.
"""
import os
import re
import sys
import json
import shutil
import datetime
from pathlib import Path
sys.path.insert(0, str(Path.home() / '.config' / 'agentic-memory'))
from memory_common import parse_frontmatter, find_project_root


def get_note_date(metadata: dict, file_path: Path) -> datetime.date:
    """Return the note's date from frontmatter 'created' field, falling back to mtime."""
    created = metadata.get('created')
    if created:
        try:
            # Handle both date-only and datetime formats
            exp_str = str(created)
            if 'T' in exp_str:
                return datetime.datetime.fromisoformat(exp_str).date()
            else:
                return datetime.date.fromisoformat(exp_str)
        except (ValueError, TypeError):
            pass
    try:
        return datetime.date.fromtimestamp(file_path.stat().st_mtime)
    except (OSError, ValueError):
        return datetime.date.today()


def is_pinned(metadata: dict) -> bool:
    """Check if a note is pinned and should never be migrated."""
    return bool(metadata.get('pinned', False))


def consolidate_warm_sessions(memory_dir: Path, dry_run: bool = False):
    """Consolidate session logs (7-90 days old) into lesson summaries."""
    today = datetime.date.today()
    sessions_dir = memory_dir / 'sessions'
    lessons_dir = memory_dir / 'lessons'
    lessons_dir.mkdir(parents=True, exist_ok=True)

    if not sessions_dir.exists():
        return

    consolidated_count = 0
    for session_file in sessions_dir.glob('*.md'):
        try:
            content = session_file.read_text(encoding='utf-8', errors='ignore')
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
tags: {json.dumps(metadata.get('tags', []))}
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
                temp_file = lesson_file.with_suffix('.md.tmp')
                temp_file.write_text(lesson_content, encoding='utf-8')
                os.replace(str(temp_file), str(lesson_file))
                session_file.unlink()
                consolidated_count += 1
            except Exception as e:
                print(f"  Warning: Failed to consolidate {session_file}: {e}")
        else:
            consolidated_count += 1

    if consolidated_count > 0:
        print(f"  Consolidated {consolidated_count} session logs into lessons/ (dry_run={dry_run})")


def archive_cold_files(memory_dir: Path, dry_run: bool = False):
    """Archive cold files (>90 days) to archive/ bundles."""
    today = datetime.date.today()
    archive_dir = memory_dir / 'archive'
    archive_dir.mkdir(parents=True, exist_ok=True)
    stats = {'archived': 0, 'skipped': 0, 'pinned_protected': 0}
    files_to_delete = []
    bundles = {}
    for md_file in memory_dir.rglob('*.md'):
        if md_file.name == 'MEMORY.md':
            continue
        try:
            rel = md_file.relative_to(memory_dir)
        except ValueError:
            stats['skipped'] += 1
            continue
        if rel.parts[0] in ('archive', 'global'):
            stats['skipped'] += 1
            continue

        # Safety check: file size limit (>10MB)
        try:
            file_size = md_file.stat().st_size
            if file_size > 10 * 1024 * 1024:
                print(f"  Skipping {rel}: file too large ({file_size} bytes)")
                stats['skipped'] += 1
                continue
        except OSError:
            stats['skipped'] += 1
            continue

        # Safety check: binary file detection (null bytes)
        try:
            with open(md_file, 'rb') as f:
                chunk = f.read(1024)
                if b'\x00' in chunk:
                    print(f"  Skipping {rel}: binary file detected")
                    stats['skipped'] += 1
                    continue
        except OSError:
            stats['skipped'] += 1
            continue

        try:
            content = md_file.read_text(encoding='utf-8', errors='ignore')
        except (OSError, UnicodeDecodeError):
            stats['skipped'] += 1
            continue

        metadata, body = parse_frontmatter(content)
        if is_pinned(metadata):
            stats['pinned_protected'] += 1
            continue

        note_date = get_note_date(metadata, md_file)
        age_days = (today - note_date).days

        if age_days <= 90:
            stats['skipped'] += 1
            continue

        # Extract body lines, skip frontmatter
        body_lines = body.splitlines()
        truncated = body_lines[:20]
        if len(body_lines) > 20:
            truncated.append(f'... [{len(body_lines) - 20} more lines]')

        tags = metadata.get('tags', [])
        tag_str = ', '.join(tags) if isinstance(tags, list) else str(tags)

        entry = (
            f"# {md_file.stem}\n\n"
            f"Tags: {tag_str}\n"
            f"{chr(10).join(truncated)}\n\n"
            f"---\n"
            f"Source: {rel}\n"
            f"Archived: {today.isoformat()}\n"
            f"Age: {age_days} days\n"
        )

        bundle_key = rel.parts[0] if len(rel.parts) > 1 else 'misc'
        bundles.setdefault(bundle_key, []).append(entry)
        # Track files to delete only after successful archive write
        if not dry_run:
            files_to_delete.append(md_file)
        stats['archived'] += 1
    # Write archive bundles FIRST, then delete originals (transactional safety)
    if not dry_run:
        # Write all bundles
        for bundle_key, entries in bundles.items():
            bundle_file = archive_dir / f"{bundle_key}.md"
            existing = bundle_file.read_text(encoding='utf-8', errors='ignore') if bundle_file.exists() else ""
            temp_file = bundle_file.with_suffix('.md.tmp')
            temp_file.write_text(existing + "\n".join(entries) + "\n", encoding='utf-8')
            os.replace(str(temp_file), str(bundle_file))
        # Verify bundles written successfully, then delete originals
        for md_file in files_to_delete:
            try:
                md_file.unlink()
            except OSError as e:
                print(f"  Warning: Failed to delete archived file {md_file}: {e}")
    return stats


def run_tier_migration(memory_dir: Path, dry_run: bool = False):
    """Run full tier migration: consolidate warm, archive cold."""
    print(f"Running tier migration on {memory_dir}... (dry_run={dry_run})")
    
    consolidate_warm_sessions(memory_dir, dry_run)
    stats = archive_cold_files(memory_dir, dry_run)
    
    print(f"=== Tier Migration Report {'[DRY RUN]' if dry_run else ''} ===")
    print(f"  Hot  (<7 days):     N/A (unchanged)")
    print(f"  Warm (7-90 days):   Consolidated to lessons/")
    print(f"  Cold (>90 days):    {stats['archived']} archived, {stats['skipped']} skipped")
    print(f"  Pinned (protected): {stats['pinned_protected']}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Tier migration for agentic-memory')
    parser.add_argument('path', nargs='?', help='Path to memory directory (default: current project)')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')
    args = parser.parse_args()
    if args.path:
        memory_dir = Path(args.path)
    else:
        cwd = Path.cwd()
        project_root = find_project_root(cwd)
        memory_dir = project_root / 'memory'
    run_tier_migration(memory_dir, args.dry_run)
