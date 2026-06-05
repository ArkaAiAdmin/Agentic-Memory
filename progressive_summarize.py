#!/usr/bin/env python3
"""Progressive summarization for frequently retrieved memory notes.

Compresses notes through levels based on access frequency:
  Level 0: Raw (original content)
  Level 1: Highlighted (key sentences bolded)
  Level 2: Summarized (first paragraph + bullet points)
  Level 3: Atomic (first sentence only)

Thresholds:
  access_count >= 10  -> level 3 (Atomic)
  access_count >=  7  -> level 2 (Summarized)
  access_count >=  5  -> level 1 (Highlighted)
  access_count <   5  -> level 0 (Raw)
"""
import sys
import sqlite3
import re
from pathlib import Path

# Reuse shared utilities — no duplication
sys.path.insert(0, str(Path(__file__).parent))
from memory_common import find_project_root, parse_frontmatter

_FRONTMATTER_RE = re.compile(
    r'^(---\s*\r?\n.*?\r?\n---\s*(?:\r?\n|$))', re.DOTALL
)


def summarize_content(content, level):
    """Progressively summarize content based on compression level.

    Preserves the original frontmatter text verbatim (no re-serialization).
    """
    metadata, body = parse_frontmatter(content)

    # Extract raw frontmatter block so we can keep it intact
    fm_match = _FRONTMATTER_RE.match(content)
    fm_raw = (fm_match.group(1) if fm_match else "").rstrip('\n')

    if level == 0:
        return content

    paragraphs = body.split('\n\n')

    if level == 1:
        # Highlighted: bold the first sentence of each paragraph
        highlighted = []
        for p in paragraphs:
            sentences = re.split(r'(?<=[.!?])\s+', p.strip())
            if sentences:
                sentences[0] = f'**{sentences[0]}**'
            highlighted.append(' '.join(sentences))
        return fm_raw + '\n\n' + '\n\n'.join(highlighted)

    elif level == 2:
        # Summarized: first paragraph + any bullet-point paragraphs
        if not paragraphs:
            return content
        summary = paragraphs[0]
        for p in paragraphs[1:]:
            stripped = p.strip()
            if stripped.startswith('-') or stripped.startswith('*'):
                summary += '\n\n' + p
        return fm_raw + '\n\n' + summary

    elif level == 3:
        # Atomic: first sentence only
        sentences = re.split(r'(?<=[.!?])\s+', body)
        if sentences:
            return fm_raw + '\n\n' + sentences[0]
        return content

    return content


def run_progressive_summarize(memory_dir, dry_run=False):
    memory_dir = Path(memory_dir)
    db_path = memory_dir / 'memory.db'

    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        return

    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA busy_timeout = 30000;")

    # Find notes with high access count that could be compressed
    rows = db.execute("""
        SELECT id, source_file, access_count, content
        FROM memories
        WHERE access_count >= 3
        ORDER BY access_count DESC
    """).fetchall()

    if not rows:
        print("No notes with access_count >= 3 found.")
        db.close()
        return

    print(f"Found {len(rows)} candidates for progressive summarization:")
    print("=" * 80)

    for row in rows:
        note_id, source_file, access_count, db_content = row
        file_path = memory_dir / source_file

        if not file_path.exists():
            print(f"  [{note_id}] file missing: {source_file}, skipping")
            continue

        # Determine target compression level
        if access_count >= 10:
            target_level = 3  # Atomic
        elif access_count >= 7:
            target_level = 2  # Summarized
        elif access_count >= 5:
            target_level = 1  # Highlighted
        else:
            target_level = 0  # Keep raw

        # Read actual file content and detect current compression level
        original_content = file_path.read_text()
        current_metadata, _ = parse_frontmatter(original_content)
        current_level = int(current_metadata.get('compression_level', 0))

        if target_level > current_level:
            print(f"  [{note_id}] access_count={access_count}, compress {current_level} -> {target_level}")
            print(f"    Source: {source_file}")

            if not dry_run:
                new_content = summarize_content(original_content, target_level)

                # Update compression_level field in the raw frontmatter text
                if 'compression_level:' in new_content:
                    new_content = re.sub(
                        r'compression_level:\s*\d+',
                        f'compression_level: {target_level}',
                        new_content,
                    )
                else:
                    # Insert before closing ---
                    new_content = new_content.replace(
                        '---\n',
                        f'compression_level: {target_level}\n---\n',
                        1,
                    )

                import os
                temp_path = file_path.with_suffix('.md.tmp')
                temp_path.write_text(new_content, encoding='utf-8')
                os.replace(str(temp_path), str(file_path))
                # Sync db content to match compressed file
                db.execute(
                    "UPDATE memories SET content = ?, updated_at = datetime('now') WHERE id = ?",
                    (new_content, note_id),
                )
                print(f"    -> Compressed and saved")
            else:
                print(f"    -> Dry run, not modified")
        else:
            print(f"  [{note_id}] access_count={access_count}, already at level {current_level}")

    db.commit()
    db.close()


if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith('--')]

    if args:
        memory_dir = args[0]
    else:
        root = find_project_root(Path.cwd())
        memory_dir = str(root / 'memory')

    run_progressive_summarize(memory_dir, dry_run)
