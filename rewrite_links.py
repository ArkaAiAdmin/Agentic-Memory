#!/usr/bin/env python3
import os
import re
import sqlite3
import sys
from pathlib import Path
try:
    import fcntl
except ImportError:
    fcntl = None
sys.path.insert(0, str(Path.home() / '.config' / 'agentic-memory'))
from memory_common import find_project_root


def rewrite_wikilinks(dry_run: bool = False):
    cwd = Path(os.getcwd())
    project_root = find_project_root(cwd)
    local_mem = project_root / 'memory'
    db_path = local_mem / 'memory.db'
    print(f"=== Running Link Rewriter: {project_root} {'(DRY RUN)' if dry_run else ''} ===")
    if not db_path.exists():
        print("Error: Database not found. Run rebuild_index.py first to build the global note maps.")
        return
    # Get all active note mappings from database
    db = sqlite3.connect(str(db_path))
    cursor = db.cursor()
    cursor.execute("SELECT id, source_file FROM memories")
    note_maps = {row[0]: row[1] for row in cursor.fetchall()}
    db.close()
    # Process all markdown files
    notes_files = list(local_mem.glob('**/*.md'))
    modified_count = 0
    changes_preview = []
    for note_file in notes_files:
        if note_file.name == 'MEMORY.md' or 'setup_memory' in note_file.name:
            continue
        try:
            content = note_file.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            print(f"  Warning: Cannot read {note_file}: {e}")
            continue
        # Split by code blocks to avoid replacing links inside code blocks
        parts = content.split('```')
        modified = False
        # Traverse only odd parts (non-code blocks)
        for idx in range(0, len(parts), 2):
            text = parts[idx]
            def replace_link(match):
                nonlocal modified
                link = match.group(1)
                target = link.split('|')[0].strip()
                display = link.split('|')[1].strip() if '|' in link else target
                target_id = target.replace('.md', '').lower().replace('\\', '/')
                if target_id in note_maps:
                    new_source = note_maps[target_id]
                    modified = True
                    if display != target:
                        return f'[[{new_source}|{display}]]'
                    else:
                        return f'[[{new_source}]]'
                return match.group(0)
            new_text = re.sub(r'\[\[(.*?)\]\]', replace_link, text)
            if new_text != text:
                parts[idx] = new_text
                modified = True
        if modified:
            new_content = '```'.join(parts)
            if dry_run:
                # Show preview of changes
                rel_path = note_file.relative_to(project_root)
                changes_preview.append(f"Would modify: {rel_path}")
            else:
                try:
                    # Atomic write: write to temp file then os.replace
                    temp_path = note_file.with_suffix('.md.tmp')
                    temp_path.write_text(new_content, encoding='utf-8')
                    os.replace(str(temp_path), str(note_file))
                    modified_count += 1
                except Exception as e:
                    print(f"  Warning: Failed to save rewrites to {note_file}: {e}")
    if dry_run:
        if changes_preview:
            print(f"\n[DRY RUN] {len(changes_preview)} files would be modified:")
            for change in changes_preview[:20]:  # Show first 20
                print(f"  {change}")
            if len(changes_preview) > 20:
                print(f"  ... and {len(changes_preview) - 20} more files")
        else:
            print("\n[DRY RUN] No changes needed - all links are already correct.")
    else:
        print(f"Link rewriting pass complete. Modified {modified_count} files.")


if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv
    rewrite_wikilinks(dry_run=dry_run)