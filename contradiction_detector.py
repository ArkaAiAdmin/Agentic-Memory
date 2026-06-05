#!/usr/bin/env python3
import sys
import json
import sqlite3
import re
from pathlib import Path

def find_project_root(start_path):
    for path in [start_path] + list(start_path.parents):
        if (path / 'memory').is_dir() or (path / '.git').exists() or (path / 'CLAUDE.md').exists():
            return path
    return start_path

def classify_operation(new_content, existing_content):
    """Classify the operation as ADD/UPDATE/DELETE/NOOP."""
    if not existing_content:
        return 'ADD', 'No existing memory found'

    if new_content.strip() == existing_content.strip():
        return 'NOOP', 'Content is identical'

    # Check if new content is a deletion marker
    if new_content.strip().startswith('[DELETED]') or new_content.strip() == '':
        return 'DELETE', 'Content marked as deleted'

    # Check for contradiction indicators
    contradiction_signals = [
        ('contradicts', 'Explicit contradiction marker'),
        ('supersedes', 'Supersedes existing memory'),
        ('incorrect', 'Corrects existing information'),
        ('wrong', 'Corrects existing information'),
        ('actually', 'Corrects existing information'),
        ('instead of', 'Replaces existing information'),
        ('no longer', 'Invalidates existing information'),
        ('deprecated', 'Deprecates existing information'),
    ]

    new_lower = new_content.lower()
    for signal, reason in contradiction_signals:
        if signal in new_lower:
            return 'UPDATE', f'Contradiction signal: {reason}'

    # Check if content significantly overlaps
    new_words = set(new_content.lower().split())
    old_words = set(existing_content.lower().split())
    if len(new_words) > 0 and len(old_words) > 0:
        overlap = len(new_words & old_words) / max(len(new_words), len(old_words))
        if overlap > 0.8:
            return 'UPDATE', f'High overlap ({overlap:.0%}), likely update'
        elif overlap > 0.5:
            return 'UPDATE', f'Moderate overlap ({overlap:.0%}), possible update'

    return 'ADD', 'New distinct information'

def detect_contradictions(memory_dir):
    """Scan memory for contradictions."""
    memory_dir = Path(memory_dir)
    db_path = memory_dir / 'memory.db'

    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        return

    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA busy_timeout = 30000;")

    # Get all memories
    rows = db.execute("SELECT id, content, source_file FROM memories").fetchall()

    contradictions = []

    # Check for explicit contradictions via supersedes
    for row in rows:
        nid, content, source = row
        # Check if this note supersedes another
        if 'supersedes' in content.lower():
            # Find the target
            match = re.search(r'supersedes?\s+[\[\[]?([^\]\n]+)', content, re.IGNORECASE)
            if match:
                target = match.group(1).strip().strip('[]')
                # Search for target
                target_rows = db.execute("SELECT id, content FROM memories WHERE id LIKE ?", (f'%{target}%',)).fetchall()
                for target_id, target_content in target_rows:
                    contradictions.append({
                        'source': nid,
                        'target': target_id,
                        'type': 'supersedes',
                        'source_file': source
                    })

    # Check for factual conflicts (heuristic: same topic, different claims)
    for i, row1 in enumerate(rows):
        nid1, content1, source1 = row1
        for row2 in rows[i+1:]:
            nid2, content2, source2 = row2

            # Check for negation patterns
            negation_pairs = [
                ('is true', 'is false'),
                ('works', 'does not work'),
                ('supported', 'not supported'),
                ('enabled', 'disabled'),
                ('available', 'unavailable'),
            ]

            c1_lower = content1.lower()
            c2_lower = content2.lower()

            for pos, neg in negation_pairs:
                if (pos in c1_lower and neg in c2_lower) or (neg in c1_lower and pos in c2_lower):
                    contradictions.append({
                        'source': nid1,
                        'target': nid2,
                        'type': 'factual_conflict',
                        'source_file': source1
                    })

    db.close()
    return contradictions

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: contradiction_detector.py <memory_dir> [new_content_file]")
        print("  No second arg: scan for contradictions")
        print("  With second arg: classify write operation")
        sys.exit(1)

    memory_dir = sys.argv[1]

    if len(sys.argv) > 2:
        # Classify mode
        new_content = Path(sys.argv[2]).read_text()
        root = find_project_root(Path.cwd())
        db_path = root / 'memory' / 'memory.db'
        db = sqlite3.connect(str(db_path))
        existing = db.execute("SELECT content FROM memories LIMIT 1").fetchone()
        existing_content = existing[0] if existing else ''
        db.close()
        operation, reason = classify_operation(new_content, existing_content)
        print(f"Operation: {operation}")
        print(f"Reason: {reason}")
    else:
        # Scan mode
        contradictions = detect_contradictions(memory_dir)
        if contradictions:
            print(f"Found {len(contradictions)} contradictions:")
            for c in contradictions:
                print(f"  {c['source']} -> {c['target']} ({c['type']})")
        else:
            print("No contradictions found.")
