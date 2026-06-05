#!/usr/bin/env python3
import sys
import os
import sqlite3
import json
import re
from pathlib import Path
sys.path.insert(0, str(Path.home() / '.config' / 'agentic-memory'))
from memory_common import find_project_root
def search_memories(query, limit=5, custom_db_path=None, include_global=True, min_local_results=3, silent=False):
    # 1. Resolve project root and database path
    cwd = Path(os.getcwd())
    project_root = find_project_root(cwd)
    if custom_db_path:
        db_file = Path(custom_db_path)
    else:
        db_file = project_root / 'memory' / 'memory.db'
    # Global memory database path - follow symlink from local memory/global
    local_global_link = project_root / 'memory' / 'global'
    if local_global_link.is_symlink():
        global_db = local_global_link.resolve() / 'memory.db'
    else:
        global_db = Path.home() / '.config' / 'agentic-memory' / 'memory.db'
    if not db_file.exists():
        if not silent:
            print(f"Error: Memory database {db_file} does not exist. Run rebuild_index.py first.")
        return []
    all_results = []
    sources_searched = []
    local_results = _search_single_db(db_file, query, limit * 2, "local")
    all_results.extend(local_results)
    if local_results:
        sources_searched.append("local")
    if include_global and global_db.exists() and len(local_results) < min_local_results:
        global_results = _search_single_db(global_db, query, limit, "global")
        all_results.extend(global_results)
        if global_results:
            sources_searched.append("global")
    seen_ids = set()
    deduped = []
    for r in all_results:
        if r[0] not in seen_ids:
            seen_ids.add(r[0])
            deduped.append(r)
    # 5. Detect available columns for graceful fallback on older DBs
    has_fitness = False
    try:
        # Check local DB first, then global
        for db_path in [db_file, global_db]:
            if db_path.exists():
                db_check = sqlite3.connect(str(db_path), timeout=30.0)
                cols = {row[1] for row in db_check.execute("PRAGMA table_info(memories)").fetchall()}
                db_check.close()
                if 'fitness_score' in cols:
                    has_fitness = True
                    break
    except Exception:
        pass
    # 6. Compute re-ranked scores if columns available
    if has_fitness and deduped and len(deduped[0]) >= 9:
        scored = []
        for r in deduped:
            note_id, content, source_file, tags_json, created, rank, fitness, importance, pinned, source_db = r
            bm25_score = -rank
            fitness_score = fitness if fitness is not None else 1.0
            importance_val = importance if importance is not None else 3
            importance_normalized = importance_val / 5.0
            pinned_bonus = 1.0 if pinned else 0.0
            final_score = (0.5 * bm25_score) + (0.2 * fitness_score) + (0.2 * importance_normalized) + (0.1 * pinned_bonus)
            scored.append((note_id, content, source_file, tags_json, created, rank, final_score, source_db))
        scored.sort(key=lambda x: x[6], reverse=True)
        results_to_display = scored[:limit]
    else:
        if deduped:
            results_to_display = [(r[0], r[1], r[2], r[3], r[4], r[5], -r[5], r[9]) for r in deduped[:limit]]
        else:
            results_to_display = []
    # 7. Display results
    source_str = " + ".join(sources_searched) if sources_searched else "none"
    print(f"\nSearch results for: '{query}' (Top {len(results_to_display)} from {source_str})")
    print("=" * 80)
    for i, r in enumerate(results_to_display, 1):
        note_id, content, source_file, tags_json, created, rank, final_score, source_db = r
        tags = json.loads(tags_json)
        tags_str = ", ".join(tags) if tags else "none"
        source_label = f"[{source_db}]" if source_db else ""
        print(f"[{i}] {note_id} (Score: {final_score:.2f}) {source_label}")
        print(f"    Source: memory/{source_file}")
        print(f"    Tags: {tags_str}")
        print(f"    Created: {created}")
        print(f"    Content:\n    {content.strip()}")
        print()
    print("=" * 80)
    # 8. Increment access_count for every displayed result (in their respective DBs)
    for note_id, *_rest in results_to_display:
        # Find which DB this note came from
        for db_path in [db_file, global_db]:
            if db_path.exists():
                try:
                    db_upd = sqlite3.connect(str(db_path), timeout=30.0)
                    cursor = db_upd.execute('SELECT 1 FROM memories WHERE id = ?', (note_id,))
                    if cursor.fetchone():
                        db_upd.execute('UPDATE memories SET access_count = access_count + 1 WHERE id = ?', (note_id,))
                        db_upd.commit()
                        break
                    db_upd.close()
                except Exception:
                    pass
    return results_to_display
def _search_single_db(db_path: Path, query: str, limit: int, source_label: str):
    """Search a single database and return results with source label."""
    try:
        db = sqlite3.connect(str(db_path), timeout=30.0)
        db.execute("PRAGMA busy_timeout = 30000;")
        # Check for fitness columns
        has_fitness = False
        try:
            cols = {row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall()}
            has_fitness = 'fitness_score' in cols
        except Exception:
            pass
        # Sanitize FTS5 query - preserve code symbols (C++, Hilt@, v2.0, etc.)
        # Split on whitespace but preserve punctuation within tokens
        words = re.findall(r'[\w\+\@\#\.\-]+', query)
        if not words:
            db.close()
            return []
        fts_query = " ".join(f'"{w}"' for w in words)
        if has_fitness:
            results = db.execute(
                """SELECT m.id, m.content, m.source_file, m.tags, m.created_at, fts.rank,
                          m.fitness_score, m.importance, m.pinned
                   FROM memories_fts fts
                   JOIN memories m ON m.rowid = fts.rowid
                   WHERE memories_fts MATCH ?
                   ORDER BY fts.rank
                   LIMIT ?""",
                (fts_query, limit)
            ).fetchall()
        else:
            results = db.execute(
                """SELECT m.id, m.content, m.source_file, m.tags, m.created_at, fts.rank
                   FROM memories_fts fts
                   JOIN memories m ON m.rowid = fts.rowid
                   WHERE memories_fts MATCH ?
                   ORDER BY fts.rank
                   LIMIT ?""",
                (fts_query, limit)
            ).fetchall()
        db.close()
        # Add source label to each result
        if has_fitness:
            return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], source_label) for r in results]
        else:
            return [(r[0], r[1], r[2], r[3], r[4], r[5], None, None, None, source_label) for r in results]
    except Exception as e:
        print(f"Warning: Error searching {source_label} DB: {e}")
        return []
if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: search_memory.py <query> [limit] [--no-global] [db_path]")
        sys.exit(1)
    query = sys.argv[1]
    limit = 5
    include_global = True
    db_path = None
    for arg in sys.argv[2:]:
        if arg == '--no-global':
            include_global = False
        elif not arg.startswith('--'):
            if arg.isdigit():
                limit = int(arg)
            else:
                db_path = arg
    search_memories(query, limit=limit, custom_db_path=db_path, include_global=include_global, silent=False)