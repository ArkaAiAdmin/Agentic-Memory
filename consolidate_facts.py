#!/usr/bin/env python3
import sqlite3
import datetime
import os
import sys
import hashlib
import json
from pathlib import Path
try:
    import fcntl
except ImportError:
    fcntl = None
sys.path.insert(0, str(Path.home() / '.config' / 'agentic-memory'))
from memory_common import find_project_root
from contradiction_detector import detect_contradictions


def compute_content_hash(content: str) -> str:
    """Compute SHA256 hash of content for duplicate detection."""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def similarity_hash(content: str, ngram_size: int = 3) -> set:
    """Generate n-gram fingerprints for fuzzy duplicate detection."""
    content = content.lower()
    ngrams = set()
    for i in range(len(content) - ngram_size + 1):
        ngrams.add(content[i:i+ngram_size])
    return ngrams


def jaccard_similarity(set1: set, set2: set) -> float:
    """Compute Jaccard similarity between two sets."""
    if not set1 or not set2:
        return 0.0
    intersection = len(set1 & set2)
    union = len(set1 | set2)
    return intersection / union if union > 0 else 0.0


def consolidate_memory_facts():
    cwd = Path(os.getcwd())
    project_root = find_project_root(cwd)
    local_mem = project_root / 'memory'
    db_path = local_mem / 'memory.db'

    print(f"=== Running System 2 Memory Consolidation: {project_root} ===")

    if not db_path.exists():
        print(f"Error: Database {db_path} does not exist. Run rebuild_index.py first.")
        sys.exit(1)

    # Process lock using flock
    lock_file = None
    if fcntl:
        try:
            lock_path = local_mem / '.consolidate.lock'
            lock_file = open(lock_path, 'w')
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another consolidation is already running. Waiting for it to finish...")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except Exception as e:
            print(f"Warning: Could not acquire process lock: {e}")
            lock_file = None

    try:
        db = sqlite3.connect(str(db_path), timeout=30.0)
        db.execute("PRAGMA busy_timeout = 30000;")

        # 1. Scan for duplicate/redundant topics by tag overlaps
        cursor = db.cursor()
        cursor.execute("SELECT id, content, tags, source_file FROM memories WHERE repo_id IS NOT NULL")
        memories = cursor.fetchall()

        issues = []

        # 2. Check for duplicate tags
        tag_map = {}
        for mid, content, tags_json, source in memories:
            try:
                tags = json.loads(tags_json)
            except (json.JSONDecodeError, TypeError):
                tags = []
            if isinstance(tags, list):
                for tag in tags:
                    tag_map.setdefault(tag, []).append((mid, source))

        # Highlight categories with too many files (potential candidate for merge)
        for tag, items in tag_map.items():
            if len(items) > 5:
                paths = ", ".join([f"memory/{item[1]}" for item in items])
                issues.append(f"- **High Tag Density**: Tag '{tag}' is referenced by {len(items)} notes: {paths}. Suggest merging or linking them hierarchically.")

        # 3. Content-based duplicate detection using hash and similarity
        content_hashes = {}
        content_fingerprints = {}
        duplicate_candidates = []

        for mid, content, tags_json, source in memories:
            # Exact duplicate detection via hash
            content_hash = compute_content_hash(content.strip())
            if content_hash in content_hashes:
                duplicate_candidates.append((content_hashes[content_hash], mid, "exact", 1.0))
            else:
                content_hashes[content_hash] = mid

            # Fuzzy duplicate detection via n-gram similarity
            fingerprint = similarity_hash(content)
            content_fingerprints[mid] = fingerprint
        # Efficient fuzzy duplicate detection using length-based bucketing
        # Only compare fingerprints of similar lengths to reduce comparisons
        mids = list(content_fingerprints.keys())
        # Group by fingerprint size (approximate content length)
        length_buckets = {}
        for mid in mids:
            fp_len = len(content_fingerprints[mid])
            bucket_key = fp_len // 100  # Bucket by approximate length
            length_buckets.setdefault(bucket_key, []).append(mid)
        duplicate_candidates_fuzzy = []
        for bucket in length_buckets.values():
            if len(bucket) < 2:
                continue
            # Only compare within same/adjacent buckets
            for i in range(len(bucket)):
                for j in range(i + 1, min(i + 50, len(bucket))):  # Limit comparisons per item
                    sim = jaccard_similarity(content_fingerprints[bucket[i]], content_fingerprints[bucket[j]])
                    if sim > 0.8:
                        duplicate_candidates.append((bucket[i], bucket[j], "fuzzy", sim))

        if duplicate_candidates:
            dup_report = []
            for mid1, mid2, dtype, sim in duplicate_candidates[:20]:
                if dtype == "exact":
                    dup_report.append(f"  - **Exact Duplicate**: `{mid1}` <-> `{mid2}` (identical content)")
                else:
                    dup_report.append(f"  - **Fuzzy Duplicate ({sim:.0%})**: `{mid1}` <-> `{mid2}`")
            issues.append("- **Duplicate Content Detection**:\n" + "\n".join(dup_report))

        # 4. Contradiction detection using contradiction_detector.py
        print("  Scanning for contradictions...")
        local_mem = Path(db_path).parent
        contradiction_results = detect_contradictions(str(local_mem))
        if contradiction_results:
            contra_report = []
            for c in contradiction_results[:10]:
                # contradiction_detector returns 'source' and 'target' keys
                source_id = c.get('source', c.get('note1', 'unknown'))
                target_id = c.get('target', c.get('note2', 'unknown'))
                # Get claim content from database
                cursor.execute("SELECT content FROM memories WHERE id = ?", (source_id,))
                row1 = cursor.fetchone()
                claim1 = row1[0][:100] if row1 else 'N/A'
                cursor.execute("SELECT content FROM memories WHERE id = ?", (target_id,))
                row2 = cursor.fetchone()
                claim2 = row2[0][:100] if row2 else 'N/A'
                contra_report.append(
                    f"  - **{c['type'].upper()}**: `{source_id}` vs `{target_id}`\n"
                    f"    Claim 1: {claim1}...\n"
                    f"    Claim 2: {claim2}..."
                )
            issues.append("- **Contradictions Detected**:\n" + "\n".join(contra_report))

        # 5. Scan for stale session logs (>30 days old)
        cursor.execute("SELECT id, source_file, updated_at FROM memories WHERE id LIKE 'sessions/%'")
        sessions = cursor.fetchall()

        today = datetime.date.today()
        stale_sessions = []
        for mid, source, updated in sessions:
            try:
                # Handle both date-only and datetime formats
                updated_str = str(updated)
                if 'T' in updated_str:
                    updated_date = datetime.datetime.fromisoformat(updated_str).date()
                else:
                    updated_date = datetime.date.fromisoformat(updated_str)
                age_days = (today - updated_date).days
                if age_days > 30:
                    stale_sessions.append(f"  - `memory/{source}` (updated {age_days} days ago)")
            except (ValueError, TypeError):
                pass

        if stale_sessions:
            issues.append("- **Stale Session Logs**: The following session summaries are older than 30 days and should be archived:\n" + "\n".join(stale_sessions))

        # 6. Check for orphaned memories (notes with no incoming or outgoing backlinks)
        cursor.execute("SELECT id, source_file FROM memories")
        all_mids = {row[0]: row[1] for row in cursor.fetchall()}

        cursor.execute("SELECT source_id, target_id FROM backlinks")
        links = cursor.fetchall()

        linked_ids = set()
        for src, tgt in links:
            linked_ids.add(src)
            linked_ids.add(tgt)

        orphans = []
        for mid, source in all_mids.items():
            # Exclude global files and manual indexes
            if mid not in linked_ids and not mid.startswith('global/') and not mid.endswith('MEMORY'):
                orphans.append(f"  - `memory/{source}`")

        if orphans:
            issues.append("- **Orphaned Memories**: The following notes have no references or incoming backlinks. Consider linking them or merging them:\n" + "\n".join(orphans))

        # 7. Check for superseded memories that should be archived
        cursor.execute("SELECT id, source_file, supersedes FROM memories WHERE supersedes IS NOT NULL")
        superseded = cursor.fetchall()
        if superseded:
            super_report = []
            for mid, source, supersedes in superseded:
                super_report.append(f"  - `{mid}` (supersedes `{supersedes}`) - consider archiving old version")
            issues.append("- **Superseded Memories**: The following notes have newer versions:\n" + "\n".join(super_report))

    finally:
        if 'db' in locals():
            db.close()
        if lock_file and fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    # 8. Output Compaction Report
    if issues:
        report_path = local_mem / 'sessions' / 'compaction-proposal.md'
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_content = f"""---
created: {datetime.datetime.now().isoformat()}
tags: [compaction, proposal, auto-generated]
pinned: false
---

# Memory Compaction Proposal

*Generated on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}*

This report identifies potential memory consolidation opportunities.

{chr(10).join(issues)}

---
*Review these suggestions and apply changes manually or via memory_compact tool.*
"""
            temp_path = report_path.with_suffix('.md.tmp')
            temp_path.write_text(report_content, encoding='utf-8')
            os.replace(str(temp_path), str(report_path))
            print(f"Compaction proposal written to: {report_path}")
        except Exception as e:
            print(f"Error writing compaction report: {e}")
    else:
        print("No memory compaction actions required. Database is clean and consolidated.")


if __name__ == '__main__':
    consolidate_memory_facts()