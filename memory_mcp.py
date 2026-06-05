#!/usr/bin/env python3
import sys
import os
import json
from datetime import datetime
import sqlite3
import subprocess
import re
import math
from pathlib import Path
import time
import shutil
from collections import OrderedDict
from mcp.server.fastmcp import FastMCP
# Initialize FastMCP Server
mcp = FastMCP("AgenticMemory")
def parse_frontmatter(content):
    content_stripped = content.lstrip()
    match = re.match(r'^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)(.*)', content_stripped, re.DOTALL)
    if not match:
        return {}, content
    yaml_text = match.group(1)
    body = match.group(2)
    metadata = {}
    for line in yaml_text.splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith('#'):
            continue
        if ':' not in stripped_line:
            continue
        key, val = stripped_line.split(':', 1)
        key = key.strip()
        val = val.strip()
        if val.startswith('[') and val.endswith(']'):
            items = [item.strip().strip('"').strip("'") for item in val[1:-1].split(',') if item.strip()]
            metadata[key] = items
        elif val.lower() in ('true', '1', 'yes', 'on'):
            metadata[key] = True
        elif val.lower() in ('false', '0', 'no', 'off'):
            metadata[key] = False
        else:
            metadata[key] = val.strip('"').strip("'")
    return metadata, body
def find_project_root(start_path: Path) -> Path:
    """
    Traverse upwards from start_path to find the project root.
    Looks for indicators like a 'memory' directory, '.git' directory, or 'CLAUDE.md'.
    Falls back to start_path if none are found.
    """
    for path in [start_path] + list(start_path.parents):
        if (path / 'memory').is_dir() or (path / '.git').exists() or (path / 'CLAUDE.md').exists():
            return path
    return start_path
def get_memory_paths():
    cwd = Path(os.getcwd())
    project_root = find_project_root(cwd)
    local_mem = project_root / 'memory'
    global_mem = Path.home() / '.config' / 'agentic-memory'
    return project_root, local_mem, global_mem
def add_link_to_memory_md_content(content: str, category: str, title_slug: str) -> str:
    target_link = f"[[{category}/{title_slug}.md]]"
    if target_link in content:
        return content  # Link already indexed
        
    lines = content.splitlines()
    category_keywords = {
        "projects": ["active projects", "projects"],
        "decisions": ["architecture decisions", "adr", "decisions"],
        "lessons": ["hard-won lessons", "lessons", "learnings"],
        "preferences": ["user preferences", "preferences", "settings"]
    }
    keywords = category_keywords.get(category.lower(), [category.lower()])
    
    header_idx = -1
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('#'):
            header_text = stripped.lstrip('#').strip().lower()
            if any(kw in header_text for kw in keywords):
                header_idx = idx
                break
                
    rel_link = f"- [[{category}/{title_slug}.md]] — {title_slug.replace('-', ' ').title()} context."
    if header_idx != -1:
        lines.insert(header_idx + 1, rel_link)
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"## {category.title()}")
        lines.append(rel_link)
        
    return "\n".join(lines) + "\n"

def update_memory_md_locked(index_file_path: Path, category: str, title_slug: str):
    try:
        import fcntl
    except ImportError:
        fcntl = None

    mode = 'r+' if index_file_path.exists() else 'a+'
    with open(index_file_path, mode, encoding='utf-8') as f:
        if fcntl:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            content = f.read()
            new_content = add_link_to_memory_md_content(content, category, title_slug)
            if new_content != content:
                f.seek(0)
                f.truncate()
                f.write(new_content)
                f.flush()
                try: os.fsync(f.fileno())
                except OSError: pass
        finally:
            if fcntl:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

# Search cache with LRU eviction
_search_cache = OrderedDict()
_SEARCH_CACHE_MAX = 50

def _make_cache_key(db_path: Path, query: str, limit: int, rerank: bool, boost_pinned: bool, recency_weight: float) -> str:
    """Create cache key including DB path and all ranking parameters."""
    return f"{db_path}:{query}:{limit}:{rerank}:{boost_pinned}:{recency_weight}"

def _get_cached_search(key: str):
    if key in _search_cache:
        _search_cache.move_to_end(key)
        return _search_cache[key]
    return None

def _cache_search_result(key: str, result: str):
    _search_cache[key] = result
    _search_cache.move_to_end(key)
    if len(_search_cache) > _SEARCH_CACHE_MAX:
        _search_cache.popitem(last=False)

def search_memories(db_path: Path, query: str, limit: int = 5, include_global: bool = True, 
                     rerank: bool = True, boost_pinned: bool = True, recency_weight: float = 0.1) -> dict:
    """
    Search memories in the local database with optional global fallback.
    Returns dict with 'results' (list), 'count' (int), 'output' (str).
    """
    if not db_path.exists():
        return {"results": [], "count": 0, "output": f"Error: Memory database not found in current directory ({db_path}). Run memory_rebuild tool first."}
    # Sanitize FTS5 query - preserve code symbols
    words = re.findall(r'[\w\+\@\#\.\-]+', query)
    if not words:
        return {"results": [], "count": 0, "output": f"No memories matched the query: '{query}'"}
    fts_query = " ".join(f'"{w}"' for w in words)
    # Check cache with all ranking params
    cache_key = _make_cache_key(db_path, fts_query, limit, rerank, boost_pinned, recency_weight)
    cached = _get_cached_search(cache_key)
    if cached is not None:
        return cached
    try:
        # Connect with 30s busy timeout
        db = sqlite3.connect(str(db_path), timeout=30.0)
        db.execute("PRAGMA busy_timeout = 30000;")
        # Check for fitness_score, importance, pinned columns
        has_fitness = False
        try:
            cols = {row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall()}
            has_fitness = 'fitness_score' in cols
        except Exception:
            pass
        if has_fitness:
            results = db.execute(
                """SELECT m.id, m.content, m.source_file, m.tags, m.created_at, fts.rank,
                         m.fitness_score, m.importance, m.pinned
                  FROM memories_fts fts
                  JOIN memories m ON m.rowid = fts.rowid
                  WHERE memories_fts MATCH ?
                  ORDER BY fts.rank
                  LIMIT ?""",
                (fts_query, limit * 3 if rerank else limit)
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
        if not results:
            db.close()
            result = {"results": [], "count": 0, "output": f"No memories matched the query: '{query}'"}
            _cache_search_result(cache_key, result)
            return result
        # Re-rank if fitness columns available and rerank enabled
        if has_fitness and rerank:
            scored = []
            for r in results:
                note_id, content, source_file, tags_json, created, rank, fitness, importance, pinned = r
                bm25_score = -rank
                fitness_score = fitness if fitness is not None else 1.0
                importance_val = importance if importance is not None else 3
                importance_normalized = importance_val / 5.0
                pinned_bonus = 1.0 if (pinned and boost_pinned) else 0.0
                final_score = (0.5 * bm25_score) + (0.2 * fitness_score) + (0.2 * importance_normalized) + (0.1 * pinned_bonus)
                scored.append((note_id, content, source_file, tags_json, created, rank, final_score, fitness_score, importance_val, pinned))
            scored.sort(key=lambda x: x[6], reverse=True)
            results_to_display = scored[:limit]
        else:
            results_to_display = [(r[0], r[1], r[2], r[3], r[4], r[5], -r[5], None, None, None) for r in results]
        output = [f"Search results for: '{query}' (Re-ranked)" if rerank else f"Search results for: '{query}'"]
        result_items = []
        for i, r in enumerate(results_to_display, 1):
            note_id, content, source_file, tags_json, created, rank, final_score, fitness_score, importance_val, pinned = r
            tags = json.loads(tags_json) if tags_json else []
            tags_str = ", ".join(tags) if tags else "none"
            # Query backlinks
            backlink_rows = db.execute("SELECT source_id FROM backlinks WHERE target_id = ?", (note_id,)).fetchall()
            backlinks = [row[0] for row in backlink_rows]
            backlinks_str = ", ".join(f"[[{b}]]" for b in backlinks) if backlinks else "none"
            score_info = f"(Relevance: {final_score:.2f})" if rerank else f"(Rank: {-rank:.2f})"
            if fitness_score is not None:
                score_info += f" | Fitness: {fitness_score:.2f} | Importance: {importance_val} | Pinned: {'yes' if pinned else 'no'}"
            result_items.append({
                "id": note_id,
                "source_file": source_file,
                "tags": tags,
                "created": created,
                "rank": rank,
                "final_score": final_score,
                "fitness_score": fitness_score,
                "importance": importance_val,
                "pinned": pinned,
                "backlinks": backlinks
            })
            output.append(
                f"[{i}] {note_id} {score_info}\n"
                f"    Source: memory/{source_file}\n"
                f"    Tags: {tags_str}\n"
                f"    Backlinks: {backlinks_str}\n"
                f"    Created: {created}\n"
                f"    Content:\n    {content.strip()}"
            )
        db.close()
        result_str = "\n\n".join(output)
        result = {"results": result_items, "count": len(result_items), "output": result_str}
        _cache_search_result(cache_key, result)
        return result
    except Exception as e:
        return {"results": [], "count": 0, "output": f"Search Error: {e}"}

def _update_memory_index_incremental(db_path: Path, category: str, title_slug: str, content: str, tags: list, pinned: bool, now_iso: str, is_global: bool):
    """
    Incrementally update the SQLite index with a single memory entry.
    Uses atomic upsert to avoid full rebuild.
    """
    import fcntl
    import json
    import sqlite3
    if not db_path.exists():
        return
    source_file = f"{category}/{title_slug}.md"
    note_id = source_file
    tags_json = json.dumps(tags)
    # Process lock using flock (separate lock file, not DB connection)
    lock_file = None
    try:
        if fcntl:
            lock_path = db_path.parent / '.rebuild.lock'
            lock_file = open(lock_path, 'w')
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    except Exception as e:
        print(f"Warning: Could not acquire lock for incremental update: {e}")
        if lock_file:
            lock_file.close()
        lock_file = None
    try:
        db = sqlite3.connect(str(db_path), timeout=30.0)
        db.execute("PRAGMA busy_timeout = 30000;")
        # Upsert into memories table
        cursor = db.execute(
            """INSERT INTO memories (id, source_file, content, tags, created_at, updated_at, observed_at, fitness_score, importance, pinned, repo_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, 3, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   content = excluded.content,
                   tags = excluded.tags,
                   updated_at = excluded.updated_at,
                   observed_at = excluded.observed_at,
                   pinned = excluded.pinned,
                   fitness_score = excluded.fitness_score,
                   importance = excluded.importance""",
            (note_id, source_file, content, tags_json, now_iso, now_iso, now_iso, 1 if pinned else 0, None if is_global else db_path.parent.parent.name)
        )
        # Update file_mtimes table (handle both 'path' and 'file_path' column names)
        try:
            fm_cols = {row[1] for row in db.execute("PRAGMA table_info(file_mtimes)").fetchall()}
            fm_path_col = 'path' if 'path' in fm_cols else ('file_path' if 'file_path' in fm_cols else None)
            if fm_path_col:
                db.execute(
                    f"INSERT INTO file_mtimes ({fm_path_col}, mtime, content_hash) VALUES (?, strftime('%s', 'now'), '')"
                    f" ON CONFLICT({fm_path_col}) DO UPDATE SET mtime = excluded.mtime",
                    (source_file,)
                )
        except Exception:
            pass
        # Update backlinks from content
        import re
        links = re.findall(r'\[\[(.*?)\]\]', content)
        for link in links:
            target = link.split('|')[0].strip()
            target_id = target.replace('.md', '').lower().replace('\\', '/')
            db.execute(
                "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
                (note_id, target_id)
            )
        db.commit()
    except Exception as e:
        print(f"Error in incremental update: {e}")
    finally:
        if lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        db.close()

def _recalculate_fitness_scores(db_path: Path, memory_ids: list):
    """
    Incrementally recalculate fitness scores for specific memories.
    Avoids full index rebuild by updating only the affected rows.
    """
    import sqlite3
    import math
    from datetime import date
    if not db_path.exists():
        return
    try:
        db = sqlite3.connect(str(db_path), timeout=30.0)
        db.execute("PRAGMA busy_timeout = 30000;")
        today = date.today()
        w_r, w_f, w_s = 0.4, 0.3, 0.3  # recency, frequency, success weights
        for mid in memory_ids:
            # Get current stats for this memory
            row = db.execute(
                """SELECT access_count, success_score, updated_at, decay, pinned 
                   FROM memories WHERE id = ?""",
                (mid,)
            ).fetchone()
            if not row:
                continue
            access_count, success_score, updated_at, decay_setting, pinned = row
            access_count = access_count or 1
            success_score = success_score or 0.0
            decay_setting = str(decay_setting or 'none').lower()
            # Calculate decay score
            decay_rates = {'none': 0.0, 'standard': 0.01, 'fast': 0.1}
            decay_rate = decay_rates.get(decay_setting, 0.0)
            try:
                updated_str = str(updated_at)
                if 'T' in updated_str:
                    updated_date = date.fromisoformat(updated_str[:10])
                else:
                    updated_date = date.fromisoformat(updated_str)
            except (ValueError, TypeError):
                updated_date = today
            days_since_update = (today - updated_date).days
            decay_score = math.exp(-decay_rate * days_since_update)
            # Memetic fitness score calculation
            fitness_score = (w_r * decay_score) + (w_f * math.log1p(access_count)) + (w_s * success_score)
            # Update fitness_score
            db.execute(
                "UPDATE memories SET fitness_score = ? WHERE id = ?",
                (fitness_score, mid)
            )
        db.commit()
        db.close()
    except Exception as e:
        print(f"Error recalculating fitness scores: {e}")

@mcp.tool()
def memory_search(query: str, limit: int = 5, rerank: bool = True, boost_pinned: bool = True, recency_weight: float = 0.1, include_global: bool = True) -> str:
    """
    Search local memory database with optional global fallback and re-ranking.
    Args:
        query: Search query string
        limit: Maximum number of results to return
        rerank: Apply re-ranking with fitness/importance/pinned scores
        boost_pinned: Give bonus to pinned memories in ranking
        recency_weight: Weight for recency in ranking (0.0-1.0)
        include_global: Fall back to global memory if local results < 3
    """
    _, local_mem, global_mem = get_memory_paths()
    db_path = local_mem / 'memory.db'
    # Search local first
    local_results = search_memories(db_path, query, limit, include_global=False, 
                                     rerank=rerank, boost_pinned=boost_pinned, recency_weight=recency_weight)
    # Check if we need global fallback
    if include_global and local_results["count"] < 3:
        global_db = global_mem / 'memory.db'
        if global_db.exists():
            global_results = search_memories(global_db, query, limit, include_global=False,
                                              rerank=rerank, boost_pinned=boost_pinned, recency_weight=recency_weight)
            if global_results["count"] > 0:
                return f"{local_results['output']}\n\n---\nGLOBAL MEMORY RESULTS:\n{global_results['output']}"
    return local_results["output"]

@mcp.tool()
def memory_save(content: str, category: str, title_slug: str, tags: list = None, pinned: bool = False, is_global: bool = False) -> str:
    """
    Save a new memory file into the memory system and rebuild the index.
    Arguments:
      content: The core body text of the memory.
      category: The folder category (e.g. 'projects', 'lessons', 'preferences').
      title_slug: The filename slug (e.g. 'hilt-bindings' -> creates 'hilt-bindings.md').
      tags: List of strings for YAML tags metadata.
      pinned: Set to true if this should be pinned in prompt templates.
      is_global: If true, saves to global user config instead of repository memory.
    """
    # Enforce 50KB content limit
    if len(content) > 50000:
        return f"Error: Content too large ({len(content)} bytes). Maximum is 50KB."

    cwd, local_mem, global_mem = get_memory_paths()
    target_base = global_mem if is_global else local_mem
    
    # Auto-initialize local memory if missing
    if not target_base.exists():
        if not is_global:
            try:
                target_base.mkdir(parents=True, exist_ok=True)
                memory_md = target_base / 'MEMORY.md'
                if not memory_md.exists():
                    temp_md = memory_md.with_suffix('.md.tmp')
                    temp_md.write_text("# Agentic Memory Index\n\n## Active Projects\n\n## Architecture Decisions (ADRs)\n\n## Hard-Won Lessons\n\n## User Preferences\n", encoding='utf-8')
                    os.replace(str(temp_md), str(memory_md))
            except Exception as e:
                return f"Error: Target memory directory {target_base} does not exist and could not be created: {e}"
        else:
            return f"Error: Target memory directory {target_base} does not exist."
            
    # Resolve paths to protect against Directory Traversal attacks
    try:
        target_base_resolved = target_base.resolve()
        category_dir = (target_base_resolved / category).resolve()
        if not category_dir.is_relative_to(target_base_resolved):
            return "Error: Directory traversal detected in category."
            
        file_path = (category_dir / f"{title_slug}.md").resolve()
        if not file_path.is_relative_to(category_dir):
            return "Error: Directory traversal detected in title_slug."
    except Exception as e:
        return f"Error validating paths: {e}"
        
    category_dir.mkdir(parents=True, exist_ok=True)
    
    import datetime
    now_iso = datetime.datetime.now().isoformat()
    # Normalize tags input safely (handles string or list)
    if isinstance(tags, str):
        tags_list = [t.strip() for t in re.split(r'[,; ]+', tags) if t.strip()]
    elif isinstance(tags, list):
        tags_list = [str(t).strip() for t in tags if t]
    else:
        tags_list = []
    tags_str = ", ".join(tags_list)
    pinned_str = "true" if pinned else "false"
    markdown_content = f"""---
created: {now_iso}
updated: {now_iso}
observed_at: {now_iso}
tags: [{tags_str}]
pinned: {pinned_str}
related: []

# {title_slug.replace('-', ' ').title()}

{content.strip()}
"""
    
    try:
        temp_file = file_path.with_suffix('.md.tmp')
        temp_file.write_text(markdown_content, encoding='utf-8')
        os.replace(str(temp_file), str(file_path))
        # Update MEMORY.md index with advisory lock
        index_file = target_base / 'MEMORY.md'
        if index_file.exists():
            update_memory_md_locked(index_file, category, title_slug)
        # Incremental SQLite index update (single memory upsert)
        db_path = global_mem / 'memory.db' if is_global else local_mem / 'memory.db'
        _update_memory_index_incremental(db_path, category, title_slug, content, tags_list, pinned, now_iso, is_global)
        # Invalidate search cache on write
        _search_cache.clear()
        return f"Successfully saved memory: memory/{category}/{title_slug}.md (Index updated incrementally)."
    except Exception as e:
        return f"Error saving memory: {e}"

@mcp.tool()
def memory_rebuild() -> str:
    """
    Manually trigger an index compilation to rebuild memory.db from scratch.
    """
    cwd, local_mem, global_mem = get_memory_paths()
    rebuild_script = global_mem / 'rebuild_index.py'
    if not rebuild_script.exists():
        return f"Error: Global rebuild script not found at {rebuild_script}"
    try:
        # When running from global config dir, source=root and DB=root/memory.db
        if local_mem == global_mem:
            source_dir = global_mem
            db_path = global_mem / 'memory.db'
        else:
            source_dir = local_mem
            db_path = local_mem / 'memory.db'
        result = subprocess.run(
            [sys.executable, str(rebuild_script), str(source_dir), str(db_path)],
            capture_output=True, text=True, check=True
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        # Invalidate search cache on rebuild
        _search_cache.clear()
        return f"Memory index rebuilt successfully.\n{output}" if output else "Memory index rebuilt successfully."
    except Exception as e:
        return f"Failed to rebuild index: {e}"

@mcp.tool()
def memory_reinforce(memory_ids: list, success: bool) -> str:
    """
    Reinforce memory success scores based on outcome.
    Adds a positive (+1.0) reward if successful, or negative (-1.0) penalty if failed.
    """
    cwd, local_mem, global_mem = get_memory_paths()
    db_path = local_mem / 'memory.db'
    if not db_path.exists():
        return f"Error: Memory database not found at {db_path}."
        
    delta = 1.0 if success else -1.0
    try:
        db = sqlite3.connect(str(db_path), timeout=30.0)
        db.execute("PRAGMA busy_timeout = 30000;")
        
        updated = 0
        for mid in memory_ids:
            cursor = db.cursor()
            cursor.execute("SELECT id, success_score FROM memories WHERE id=?", (mid,))
            row = cursor.fetchone()
            if row:
                old_score = row[1] or 0.0
                new_score = max(-3.0, min(5.0, old_score + delta))
                db.execute("UPDATE memories SET success_score = ? WHERE id = ?", (new_score, mid))
                updated += 1
        db.commit()
        db.close()
        # Incremental fitness score recalculation instead of full rebuild
        _recalculate_fitness_scores(local_mem / 'memory.db', memory_ids)
        # Invalidate search cache on write
        _search_cache.clear()
        return f"Successfully reinforced {updated} memories with outcome success={success} (Fitness scores recalculated)."
    except Exception as e:
        return f"Error reinforcing outcomes: {e}"

@mcp.tool()
def memory_compile_skill(lesson_slug: str, skill_name: str, primary_triggers: list, secondary_triggers: list = None) -> str:
    """
    Compile a lesson note into a validated executable agent skill rule file in ~/.agents/skills/.
    Arguments:
      lesson_slug: The filename slug of the source lesson (e.g. 'api-pitfalls' or 'jsonl-reconstruction').
      skill_name: The directory name of the target skill (e.g. 'jsonl-reconstructor').
      primary_triggers: List of strings for primary keywords that trigger this skill.
      secondary_triggers: List of strings for secondary keywords.
    """
    cwd, local_mem, global_mem = get_memory_paths()
    lesson_file = local_mem / 'lessons' / f"{lesson_slug}.md"
    
    # Try global folder if local not found
    if not lesson_file.exists():
        lesson_file = global_mem / 'lessons' / f"{lesson_slug}.md"
        
    if not lesson_file.exists():
        return f"Error: Lesson memory note '{lesson_slug}' does not exist."
        
    try:
        content = lesson_file.read_text(encoding='utf-8')
        metadata, body = parse_frontmatter(content)
        
        # Extract code blocks
        code_blocks = re.findall(r'```(\w+)\r?\n(.*?)\r?\n```', body, re.DOTALL)
        
        # Validate Python code syntax
        for lang, code in code_blocks:
            if lang.lower() == 'python' or lang.lower() == 'py':
                try:
                    compile(code, "<string>", "exec")
                except SyntaxError as se:
                    return f"Validation Error: Python syntax error in lesson code blocks: {se}"
                    
        # Construct YAML metadata frontmatter
        skill_metadata = {
            "name": skill_name,
            "description": metadata.get('description', f"Executable skill compiled from lesson: {lesson_slug}"),
            "when_to_use": f"Use when working with topics related to: {', '.join(primary_triggers)}",
            "disable-model-invocation": True
        }
        
        # Build trigger block
        trigger_block = {
            "triggers": {
                "keywords": {
                    "primary": primary_triggers,
                    "secondary": secondary_triggers if secondary_triggers else []
                }
            }
        }
        skill_metadata.update(trigger_block)
        
        # Format skill markdown
        import yaml
        yaml_header = yaml.dump(skill_metadata, sort_keys=False)
        
        skill_content = f"""---
{yaml_header.strip()}
---

# Skill: {skill_name.replace('-', ' ').title()}

{body.strip()}
"""
        # Save to ~/.agents/skills/
        skills_dir = Path.home() / '.agents' / 'skills' / skill_name
        skills_dir.mkdir(parents=True, exist_ok=True)
        
        skill_file_path = skills_dir / 'SKILL.md'
        temp_file = skill_file_path.with_suffix('.md.tmp')
        temp_file.write_text(skill_content, encoding='utf-8')
        os.replace(str(temp_file), str(skill_file_path))
        
        # Update global skills catalog
        recompile_skills_catalog()
        
        return f"Successfully compiled and validated skill: {skill_name} at ~/.agents/skills/{skill_name}/SKILL.md (Skills index updated)."
    except Exception as e:
        return f"Error compiling skill: {e}"

@mcp.tool()
def memory_audit() -> str:
    """
    Audit memory system health using SRMA-inspired metrics.
    Returns health scores, drifted memories, and recommendations.
    """
    cwd, local_mem, global_mem = get_memory_paths()
    db_path = local_mem / 'memory.db'
    if not db_path.exists():
        return f"Error: Memory database not found at {db_path}."

    try:
        db = sqlite3.connect(str(db_path), timeout=30.0)
        db.row_factory = sqlite3.Row

        rows = db.execute(
            "SELECT id, content, created_at, updated_at, access_count, pinned "
            "FROM memories"
        ).fetchall()
        db.close()

        if not rows:
            return "No memories found to audit."

        now = datetime.now()
        metrics = []

        for row in rows:
            content = row['content'] or ""
            access_count = row['access_count'] or 0
            created_at = row['created_at']
            updated_at = row['updated_at']

            try:
                created = datetime.fromisoformat(created_at)
                updated = datetime.fromisoformat(updated_at)
            except (ValueError, TypeError):
                created = now
                updated = now

            days_since_creation = max(1.0, (now - created).total_seconds() / 86400)
            days_since_updated = max(0.0, (now - updated).total_seconds() / 86400)

            rho = access_count / days_since_creation
            psi = days_since_updated / max(1, access_count)
            omega = len(content) / max(1, access_count)

            metrics.append({
                "id": row['id'],
                "pinned": row['pinned'],
                "access_count": access_count,
                "rho": rho,
                "psi": psi,
                "omega": omega,
                "content_preview": content[:80].replace('\n', ' ').replace('\r', ''),
            })

        n = len(metrics)

        # Normalize each metric to [0, 1] across the population
        max_rho = max(m["rho"] for m in metrics) or 1.0
        max_psi = max(m["psi"] for m in metrics) or 1.0
        max_omega = max(m["omega"] for m in metrics) or 1.0

        # Overall health: average of (normalized_rho + (1-normalized_psi) + (1-normalized_omega)) / 3
        health_scores = []
        for m in metrics:
            n_rho = m["rho"] / max_rho
            n_psi = m["psi"] / max_psi
            n_omega = m["omega"] / max_omega
            health_scores.append((n_rho + (1 - n_psi) + (1 - n_omega)) / 3)

        overall_health = sum(health_scores) / n if n else 0

        # Top 5 most drifted (highest Psi)
        drifted = sorted(metrics, key=lambda m: m["psi"], reverse=True)[:5]
        # Top 5 most efficient (lowest Omega)
        efficient = sorted(metrics, key=lambda m: m["omega"])[:5]
        # Never accessed
        never_accessed = [m for m in metrics if m["access_count"] == 0]

        lines = [
            "=== Memory Audit Report ===",
            f"Total memories: {n}",
            f"Overall health score: {overall_health:.3f} (0=worst, 1=best)",
            "",
            "--- Top 5 Most Drifted (candidates for archival) ---",
        ]
        for m in drifted:
            lines.append(f"  [{m['id']}] psi={m['psi']:.1f}  accesses={m['access_count']}  {m['content_preview']}")

        lines.append("")
        lines.append("--- Top 5 Most Efficient (core knowledge) ---")
        for m in efficient:
            lines.append(f"  [{m['id']}] omega={m['omega']:.1f}  accesses={m['access_count']}  {m['content_preview']}")

        lines.append("")
        lines.append(f"--- Never Accessed ({len(never_accessed)} memories, candidates for deletion) ---")
        for m in never_accessed:
            lines.append(f"  [{m['id']}] {m['content_preview']}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error during audit: {e}"

@mcp.tool()
def memory_consolidate() -> str:
    """
    Run System 2 offline consolidation to identify contradictions,
    duplicates, and compaction candidates.
    """
    _, _, global_mem = get_memory_paths()
    script = global_mem / 'consolidate_facts.py'
    if not script.exists():
        return f"Error: consolidate_facts.py not found at {script}"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120)
    return result.stdout or result.stderr

@mcp.tool()
def memory_rewrite_links() -> str:
    """
    Normalize all wikilinks in memory files to consistent format.
    Code-block safe. Dry-run by default.
    """
    _, _, global_mem = get_memory_paths()
    script = global_mem / 'rewrite_links.py'
    if not script.exists():
        return f"Error: rewrite_links.py not found at {script}"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    return result.stdout or result.stderr

@mcp.tool()
def memory_detect_contradictions() -> str:
    """
    Scan memory for contradictions and conflicts.
    Returns list of contradictory memory pairs.
    """
    _, local_mem, global_mem = get_memory_paths()
    script = global_mem / 'contradiction_detector.py'
    if not script.exists():
        return f"Error: contradiction_detector.py not found at {script}"
    result = subprocess.run(
        [sys.executable, str(script), str(local_mem)],
        capture_output=True, text=True, timeout=30
    )
    return result.stdout or result.stderr

@mcp.tool()
def memory_semantic_search(query: str, limit: int = 5) -> str:
    """Semantic search using embeddings alongside FTS5."""
    _, _, global_mem = get_memory_paths()
    script = global_mem / 'embedding_search.py'
    if not script.exists():
        return f"Error: embedding_search.py not found at {script}"
    result = subprocess.run(
        [sys.executable, str(script), query, str(limit)],
        capture_output=True, text=True, timeout=30
    )
    return result.stdout or result.stderr


@mcp.tool()
def memory_compact() -> str:
    """
    Run full memory compaction: tier migration, fact consolidation, and index rebuild.
    """
    import subprocess
    results = []

    _, _, global_mem = get_memory_paths()

    # 1. Tier migration
    tier_script = global_mem / 'tier_migration.py'
    if tier_script.exists():
        r = subprocess.run([sys.executable, str(tier_script)], capture_output=True, text=True, timeout=60)
        results.append(f"Tier Migration:\n{r.stdout}")

    # 2. Fact consolidation
    consolidate_script = global_mem / 'consolidate_facts.py'
    if consolidate_script.exists():
        r = subprocess.run([sys.executable, str(consolidate_script)], capture_output=True, text=True, timeout=120)
        results.append(f"Fact Consolidation:\n{r.stdout[:500]}")
    # 3. Index rebuild
    rebuild_script = global_mem / 'rebuild_index.py'
    if rebuild_script.exists():
        r = subprocess.run([sys.executable, str(rebuild_script)], capture_output=True, text=True, timeout=60)
        results.append(f"Index Rebuild:\n{r.stdout}")
    # 4. Session auto-archive (move sessions >14 days old to archive)
    project_root, _, _ = get_memory_paths()
    sessions_dir = project_root / 'memory' / 'sessions'
    archive_dir = project_root / 'memory' / 'archive' / 'sessions'
    if sessions_dir.exists():
        archive_dir.mkdir(parents=True, exist_ok=True)
        for f in sessions_dir.glob('*.md'):
            if f.stat().st_mtime < (time.time() - 14 * 86400):
                shutil.move(str(f), str(archive_dir / f.name))
                results.append(f"Archived session: {f.name}")
    return '\n\n'.join(results)

@mcp.tool()
def memory_arc_stats() -> str:
    """
    Get ARC cache statistics: ghost entries, eviction pressure, hit rate.
    Uses Adaptive Replacement Cache to self-tune eviction policy.
    """
    import subprocess
    cwd, local_mem, global_mem = get_memory_paths()
    script = global_mem / 'arc_cache.py'
    if not script.exists():
        return "Error: arc_cache.py not found in global config."
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout or result.stderr

@mcp.tool()
def memory_review_schedule() -> str:
    """
    Get spaced repetition review schedule.
    Shows memories due for review based on SM-2 algorithm.
    """
    import subprocess
    cwd, local_mem, global_mem = get_memory_paths()
    script = global_mem / 'spaced_repetition.py'
    if not script.exists():
        return "Error: spaced_repetition.py not found."
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout or result.stderr

def recompile_skills_catalog():
    try:
        skills_dir = Path.home() / '.agents' / 'skills'
        dest_file = Path.home() / '.config' / 'agentic-memory' / 'preferences' / 'installed-skills.md'
        
        if not skills_dir.exists() or not dest_file.parent.exists():
            return
            
        skills_data = []
        for item in sorted(skills_dir.iterdir()):
            if item.is_dir():
                skill_md = item / 'SKILL.md'
                if skill_md.exists():
                    content = skill_md.read_text(encoding='utf-8', errors='ignore')
                    match = re.match(r'^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)', content, re.DOTALL)
                    meta = {}
                    if match:
                        for line in match.group(1).splitlines():
                            if ':' in line:
                                k, v = line.split(':', 1)
                                meta[k.strip()] = v.strip().strip('"').strip("'")
                    name = meta.get('name', item.name)
                    desc = meta.get('description', 'No description.')
                    when = meta.get('when_to_use', 'Use when requested.')
                    skills_data.append({"name": name, "description": desc, "when": when})
                    
        md_lines = [
            "---",
            "created: 2026-06-04",
            "updated: 2026-06-04",
            "tags: [reference, skills, tools]",
            "pinned: true",
            "related: []",
            "---",
            "",
            "# Installed Agent Skills Index",
            "",
            "This document lists all the custom agent skills installed on this machine at `~/.agents/skills/`. You can load the full instructions for any skill by reading `skill://<skill-name>` (e.g. `read skill://tdd` or `read skill://improve-codebase-architecture`).",
            "",
            "## Skills Directory",
            "",
            "| Skill | Trigger / When to Use | Description |",
            "| :--- | :--- | :--- |"
        ]
        for skill in skills_data:
            when_esc = skill['when'].replace('|', '\\|').replace('\n', ' ')
            desc_esc = skill['description'].replace('|', '\\|').replace('\n', ' ')
            md_lines.append(f"| `skill://{skill['name']}` | {when_esc} | {desc_esc} |")
            
        temp_file = dest_file.with_suffix('.md.tmp')
        temp_file.write_text("\n".join(md_lines) + "\n", encoding='utf-8')
        os.replace(str(temp_file), str(dest_file))
    except Exception as e:
        print(f"Warning: Failed to recompile skills catalog: {e}")

if __name__ == '__main__':
    # Run stdio server
    mcp.run()
