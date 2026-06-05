#!/usr/bin/env python3
import os
import sys
import re
import sqlite3
from pathlib import Path
import json
import datetime
import math

try:
    import fcntl
except ImportError:
    fcntl = None

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
    for path in [start_path] + list(start_path.parents):
        if (path / 'memory').is_dir() or (path / '.git').exists() or (path / 'CLAUDE.md').exists():
            return path
    return start_path

def _regenerate_memory_md(source: Path, db_path):
    """Regenerate MEMORY.md index from database contents."""
    db_path = Path(db_path)
    mem_dir = source if source.name == 'memory' else source / 'memory'
    mem_md = mem_dir / 'MEMORY.md'
    
    if not db_path.exists():
        return
    
    db = sqlite3.connect(str(db_path), timeout=5)
    rows = db.execute('SELECT id, source_file, tags, pinned FROM memories ORDER BY id').fetchall()
    db.close()
    
    from collections import defaultdict
    categories = defaultdict(list)
    for mid, source_file, tags, pinned in rows:
        cat = mid.split('/')[0] if '/' in mid else 'other'
        slug = mid.split('/')[-1] if '/' in mid else mid
        categories[cat].append((mid, source_file, slug, pinned))
    
    headers = {
        'projects': '## Active Projects',
        'decisions': '## Architecture Decisions (ADRs)',
        'lessons': '## Hard-Won Lessons',
        'preferences': '## User Preferences',
        'quirks': '## Quirks & Known Issues',
        'sessions': '## Session Logs',
        'docs': '## Documentation',
        'other': '## Other',
    }
    
    lines = [
        '---',
        'created: 2026-01-01T00:00:00',
        'updated: 2026-01-01T00:00:00',
        'observed_at: 2026-01-01T00:00:00',
        'tags: [index, memory-system]',
        'pinned: true',
        'importance: 5',
        'decay: none',
        'related: []',
        '---',
        '',
        '# Agentic Memory Index',
        '',
    ]
    
    for cat in ['projects', 'decisions', 'lessons', 'preferences', 'quirks', 'sessions', 'docs', 'other']:
        if cat in categories:
            lines.append(headers.get(cat, f'## {cat.title()}'))
            for mid, source_file, slug, pinned in categories[cat]:
                title = slug.replace('-', ' ').title()
                pin = ' 📌' if pinned else ''
                lines.append(f'- [[{source_file}]] — {title}{pin}')
            lines.append('')
    
    mem_md.write_text('\n'.join(lines), encoding='utf-8')


def rebuild_index(source_dir, db_path):
    source = Path(source_dir).resolve()
    
    # Process lock using flock
    lock_file = None
    if fcntl:
        try:
            lock_path = source / '.rebuild.lock'
            lock_file = open(lock_path, 'w')
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another index rebuild is already running. Waiting for it to finish...")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except Exception as e:
            print(f"Warning: Could not acquire process lock: {e}")
            lock_file = None

    print(f"Scanning markdown files in: {source}...")
    if not source.exists():
        print(f"Error: Source directory {source} does not exist.")
        if lock_file and fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        sys.exit(1)
        
    # Preserving Dynamic Stats from existing DB
    stats_map = {}
    file_mtimes_map = {}
    cached_notes_map = {}
    db_file_path = Path(db_path)
    if db_file_path.exists():
        try:
            old_db = sqlite3.connect(str(db_file_path), timeout=5.0)
            cursor = old_db.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'")
            if cursor.fetchone():
                cursor.execute("SELECT id, access_count, success_score FROM memories")
                for row in cursor.fetchall():
                    stats_map[row[0]] = (row[1], row[2])
                # Load full cached records for incremental indexing
                cursor.execute("PRAGMA table_info(memories)")
                memories_cols = {row[1] for row in cursor.fetchall()}
                required_cols = ['id', 'content', 'source_file', 'tags', 'created_at', 'updated_at', 'observed_at', 'pinned', 'importance', 'decay', 'score', 'supersedes', 'repo_id', 'access_count', 'success_score', 'fitness_score', 'consolidation_state']
                # Validate columns against allowlist to prevent SQL injection
                available_cols = [c for c in required_cols if c in memories_cols]
                if 'source_file' in available_cols:
                    # Safe: columns validated against required_cols allowlist
                    col_query = ", ".join(available_cols)
                    cursor.execute(f"SELECT {col_query} FROM memories")
                    for row in cursor.fetchall():
                        note_data = dict(zip(available_cols, row))
                        cached_notes_map[note_data['source_file']] = note_data
            # Load file_mtimes for incremental indexing (handle both old and new column names)
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_mtimes'")
            if cursor.fetchone():
                cursor.execute("PRAGMA table_info(file_mtimes)")
                cols = {row[1] for row in cursor.fetchall()}
                # Validate columns against allowlist to prevent SQL injection
                allowed_file_mtimes_cols = {'path', 'file_path', 'mtime', 'content_hash'}
                path_col = 'path' if 'path' in cols else ('file_path' if 'file_path' in cols else None)
                if path_col and path_col in allowed_file_mtimes_cols:
                    has_hash = 'content_hash' in cols and 'content_hash' in allowed_file_mtimes_cols
                    hash_select = ", content_hash" if has_hash else ""
                    # Safe: columns validated against allowlist
                    cursor.execute(f"SELECT {path_col}, mtime {hash_select} FROM file_mtimes")
                    for row in cursor.fetchall():
                        file_mtimes_map[row[0]] = {
                            'mtime': row[1],
                            'content_hash': row[2] if has_hash else ""
                        }
            if file_mtimes_map:
                print(f"  Loaded {len(file_mtimes_map)} file mtimes for incremental indexing.")
            old_db.close()
        except Exception as e:
            print(f"  Warning: Could not load stats from old database: {e}")
    # First Pass: Scan directory
    raw_notes = {}
    cached_notes_to_keep = {}
    processed_paths = set()
    processed_dirs = set()
    def scan_dir(directory):
        try:
            dir_path = Path(directory).resolve()
            if dir_path in processed_dirs:
                return  # Prevent circular directory symlinks infinite loop
            processed_dirs.add(dir_path)
            
            entries = list(os.scandir(directory))
        except OSError as e:
            print(f"  Warning: Cannot scan directory {directory}: {e}")
            return

        for entry in entries:
            try:
                entry_path = Path(entry.path)
                try:
                    resolved = entry_path.resolve()
                except OSError as e:
                    print(f"  Warning: Cannot resolve path {entry_path}: {e}")
                    continue
                if resolved in processed_paths:
                    continue
                try:
                    is_directory = entry.is_dir()
                except OSError:
                    is_directory = False
                try:
                    is_file = entry.is_file()
                except OSError:
                    is_file = False
                if is_directory:
                    # Skip global symlink directory to avoid indexing global memories during local rebuild
                    if entry.name == 'global' and Path(entry.path).is_symlink():
                        print(f"  Skipping global symlink directory: {entry.path}")
                        continue
                    if entry.name not in ('.git', 'venv', '.venv', 'env', 'node_modules', '__pycache__', '.pytest_cache'):
                        scan_dir(entry.path)
                elif is_file and entry.name.endswith('.md'):
                    if entry.name in ('MEMORY.md', 'setup_memory.sh') or 'setup_memory' in entry.name:
                        continue
                    # Incremental indexing: check if file has changed
                    rel_path = str(entry_path.relative_to(source))
                    try:
                        current_mtime = entry_path.stat().st_mtime
                        if (rel_path in file_mtimes_map and 
                            current_mtime == file_mtimes_map[rel_path]['mtime'] and 
                            rel_path in cached_notes_map):
                            # File hasn't changed, skip processing and preserve cached note
                            cached_notes_to_keep[rel_path] = cached_notes_map[rel_path]
                            continue
                    except OSError:
                        pass
                    # Safe size check (limit to 10MB)
                    try:
                        file_size = entry_path.stat().st_size
                        if file_size > 10 * 1024 * 1024:
                            print(f"  Warning: Skipping {entry_path} because it is too large ({file_size} bytes).")
                            continue
                    except OSError as e:
                        print(f"  Warning: Could not stat {entry_path}: {e}")
                        continue
                    # Safe binary check
                    try:
                        with open(entry_path, 'rb') as f:
                            chunk = f.read(1024)
                            if b'\x00' in chunk:
                                print(f"  Warning: Skipping binary file {entry_path}")
                                continue
                    except OSError as e:
                        print(f"  Warning: Could not perform binary check on {entry_path}: {e}")
                        continue
                    try:
                        content = entry_path.read_text(encoding='utf-8', errors='strict')
                        raw_notes[entry_path] = content
                    except UnicodeDecodeError:
                        print(f"  Warning: Skipping non-UTF-8 file {entry_path} (contains invalid byte sequences)")
                        continue
                    except Exception as e:
                        print(f"  Warning: Could not read {entry_path}: {e}")
                        continue
            except Exception as e:
                print(f"  Warning: Error processing entry {entry.name} in {directory}: {e}")
    scan_dir(source)
    
    # Process metadata and build note database dictionary
    all_notes = {}
    superseded_by = {}
    decayed_notes = []
    expired_notes = []
    
    today_date = datetime.date.today()
    repo_name = source.name
    if repo_name == 'memory' and source.parent:
        repo_name = source.parent.name
    
    for path, content in raw_notes.items():
        try:
            rel_path = path.relative_to(source)
        except ValueError:
            rel_path = path.name
            
        note_id = str(rel_path.with_suffix('')).lower().replace('\\', '/')
        metadata, body = parse_frontmatter(content)
        
        # Parse Dates
        created = metadata.get('created', today_date.isoformat())
        updated = metadata.get('updated', today_date.isoformat())
        observed_at = metadata.get('observed_at', created)
        # Normalize to ISO datetime format (YYYY-MM-DDTHH:MM:SS)
        # Use explicit variables instead of locals() mutation
        def normalize_dt(val):
            s = str(val)
            if len(s) == 10 and s[4] == '-':
                return s + 'T00:00:00'
            return s
        created = normalize_dt(created)
        updated = normalize_dt(updated)
        observed_at = normalize_dt(observed_at)
        # Safe Expiry check (TTL)
        expires = metadata.get('expires')
        if expires:
            try:
                exp_date = datetime.date.fromisoformat(str(expires))
                if exp_date <= today_date:
                    expired_notes.append((note_id, str(rel_path), str(expires)))
                    continue
            except (ValueError, TypeError):
                print(f"  Warning: Invalid expires date format '{expires}' in {rel_path}. Expected YYYY-MM-DD.")
        # Namespace scoping (repo_id is NULL for global folders, repo_name for local)
        if str(rel_path).startswith('global/'):
            repo_id = None
        else:
            repo_id = repo_name
        # Get preserved stats
        old_access, old_success = stats_map.get(note_id, (1, 0.0))
        # Decay calculation
        decay_setting = str(metadata.get('decay', 'none')).lower()
        decay_rates = {'none': 0.0, 'standard': 0.01, 'fast': 0.1}
        decay_rate = decay_rates.get(decay_setting, 0.0)
        
        try:
            updated_date = datetime.date.fromisoformat(str(updated)[:10])
        except (ValueError, TypeError):
            updated_date = today_date
            
        days_since_update = (today_date - updated_date).days
        decay_score = math.exp(-decay_rate * days_since_update)
        
        # Memetic fitness score calculation:
        # F = w_r * recency + w_f * log(1 + access) + w_s * success_score
        w_r, w_f, w_s = 0.4, 0.3, 0.3
        fitness_score = (w_r * decay_score) + (w_f * math.log1p(old_access)) + (w_s * old_success)
        
        # Evict decayed notes (score < 0.25)
        if fitness_score < 0.25 and decay_setting != 'none' and not metadata.get('pinned', False):
            decayed_notes.append((note_id, str(rel_path), fitness_score))
            continue
            
        try:
            importance = int(metadata.get('importance', 3))
        except (ValueError, TypeError):
            importance = 3
            
        supersedes = metadata.get('supersedes')
        if supersedes:
            target_id = str(supersedes).replace('.md', '').lower().replace('\\', '/')
            superseded_by[target_id] = note_id
            
        all_notes[note_id] = {
            "path": path,
            "rel_path": rel_path,
            "body": body,
            "metadata": metadata,
            "created": str(created),
            "updated": str(updated),
            "pinned": 1 if metadata.get('pinned', False) else 0,
            "importance": importance,
            "decay": decay_setting,
            "score": decay_score,
            "supersedes": str(supersedes) if supersedes else None,
            "repo_id": repo_id,
            "access_count": old_access,
            "success_score": old_success,
            "fitness_score": fitness_score,
            "chars": len(content)
        }
    # Merge cached notes that didn't change
    for rel_path, cached_info in cached_notes_to_keep.items():
        note_id = cached_info['id']
        # Check supersedes link
        supersedes = cached_info.get('supersedes')
        if supersedes:
            target_id = str(supersedes).replace('.md', '').lower().replace('\\', '/')
            superseded_by[target_id] = note_id
        metadata = {
            'tags': json.loads(cached_info.get('tags', '[]')) if isinstance(cached_info.get('tags'), str) else (cached_info.get('tags') or []),
            'pinned': bool(cached_info.get('pinned', 0)),
            'importance': cached_info.get('importance', 3),
            'decay': cached_info.get('decay', 'none'),
            'supersedes': supersedes,
            'consolidation_state': cached_info.get('consolidation_state', 'working')
        }
        all_notes[note_id] = {
            "path": source / rel_path,
            "rel_path": Path(rel_path),
            "body": cached_info['content'],
            "metadata": metadata,
            "created": cached_info.get('created_at'),
            "updated": cached_info.get('updated_at'),
            "pinned": 1 if cached_info.get('pinned', 0) else 0,
            "importance": cached_info.get('importance', 3),
            "decay": cached_info.get('decay', 'none'),
            "score": cached_info.get('score', 1.0),
            "supersedes": supersedes,
            "repo_id": cached_info.get('repo_id'),
            "access_count": cached_info.get('access_count', 1),
            "success_score": cached_info.get('success_score', 0.0),
            "fitness_score": cached_info.get('fitness_score', 1.0),
            "chars": len(cached_info['content'])
        }
    # Compile to temporary DB
    tmp_db_path = db_file_path.with_suffix('.db.tmp')
    db = sqlite3.connect(str(tmp_db_path), timeout=30.0)
    db.execute("PRAGMA busy_timeout = 30000;")
    db.execute("PRAGMA journal_mode=DELETE;")
    
    db.execute("DROP TABLE IF EXISTS memories_fts;")
    db.execute("DROP TABLE IF EXISTS memories;")
    db.execute("DROP TABLE IF EXISTS backlinks;")
    
    db.execute("""
    CREATE TABLE memories (
        id            TEXT PRIMARY KEY,
        content       TEXT NOT NULL,
        source_file   TEXT NOT NULL,
        tags          TEXT DEFAULT '[]',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        observed_at   TEXT NOT NULL,
        pinned        INTEGER DEFAULT 0,
        importance    INTEGER DEFAULT 3,
        decay         TEXT DEFAULT 'none',
        score         REAL DEFAULT 1.0,
        supersedes    TEXT,
        repo_id       TEXT,
        access_count  INTEGER DEFAULT 1,
        success_score REAL DEFAULT 0.0,
        fitness_score REAL DEFAULT 1.0,
        conflict_policy TEXT DEFAULT 'supersede',
        version_vector TEXT DEFAULT '{}',
        logical_clock INTEGER DEFAULT 0,
        consolidation_state TEXT DEFAULT 'working'
    );
    """)
    
    db.execute("""
    CREATE VIRTUAL TABLE memories_fts USING fts5(
        content,
        tags,
        content=memories,
        content_rowid=rowid,
        tokenize='unicode61'
    );
    """)
    db.execute("""
    CREATE TABLE backlinks (
        source_id TEXT,
        target_id TEXT,
        PRIMARY KEY (source_id, target_id)
    );
    """)
    db.execute("""
    CREATE TABLE file_mtimes (
        path TEXT PRIMARY KEY,
        mtime REAL NOT NULL,
        content_hash TEXT NOT NULL
    );
    """)
    db.execute("""
    CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
      INSERT INTO memories_fts(rowid, content, tags)
      VALUES (new.rowid, new.content, new.tags);
    END;
    """)
    db.execute("""
    CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
      INSERT INTO memories_fts(memories_fts, rowid, content, tags)
      VALUES ('delete', old.rowid, old.content, old.tags);
    END;
    """)
    db.execute("""
    CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
      INSERT INTO memories_fts(memories_fts, rowid, content, tags)
      VALUES ('delete', old.rowid, old.content, old.tags);
      INSERT INTO memories_fts(rowid, content, tags)
      VALUES (new.rowid, new.content, new.tags);
    END;
    """)
    # Performance indexes for common query patterns
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_repo_id ON memories(repo_id);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_pinned ON memories(pinned);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_consolidation_state ON memories(consolidation_state);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_observed_at ON memories(observed_at);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_fitness_score ON memories(fitness_score);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_source_file ON memories(source_file);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_backlinks_target_id ON backlinks(target_id);")
    count = 0
    total_chars = 0
    compaction_candidates = []
    
    for nid, rpath, exp in expired_notes:
        print(f"  Skipping expired memory: {rpath} (Expired: {exp})")
        compaction_candidates.append(f"memory/{rpath} [EXPIRED: delete file]")
        
    for nid, rpath, fit_score in decayed_notes:
        print(f"  Skipping decayed memory: {rpath} (Fitness: {fit_score:.2f})")
        compaction_candidates.append(f"memory/{rpath} [DECAYED: move to archive/]")
        
    # Write notes
    active_notes = {}
    for nid, note in all_notes.items():
        if nid in superseded_by:
            by_note = superseded_by[nid]
            print(f"  Skipping superseded memory: {note['rel_path']} (Superseded by {by_note})")
            compaction_candidates.append(f"memory/{note['rel_path']} [SUPERSEDED: move to archive/ or delete]")
            continue
            
        tags_json = json.dumps(note['metadata'].get('tags', []))
        
        db.execute(
            """INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at, pinned, importance, decay, score, supersedes, repo_id, access_count, success_score, fitness_score, conflict_policy, version_vector, logical_clock, consolidation_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (nid, note['body'].strip(), str(note['rel_path']), tags_json, note['created'], note['updated'], note.get('observed_at', note['created']),
             note['pinned'], note['importance'], note['decay'], note['score'], note['supersedes'], note['repo_id'],
             note['access_count'], note['success_score'], note['fitness_score'], 'supersede', '{}', 0, note.get('consolidation_state', 'working'))
        )
        # Update file_mtimes table
        import time
        rel_path_str = str(note['rel_path'])
        if rel_path_str in file_mtimes_map:
            mtime = file_mtimes_map[rel_path_str]['mtime']
            content_hash = file_mtimes_map[rel_path_str]['content_hash']
        else:
            # Handle case where cached note's file no longer exists
            try:
                mtime = note['rel_path'].stat().st_mtime if note['rel_path'].exists() else time.time()
            except (OSError, AttributeError):
                mtime = time.time()
            content_hash = str(hash(note['body'].strip()))
        db.execute(
            """INSERT INTO file_mtimes (path, mtime, content_hash) VALUES (?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, content_hash = excluded.content_hash""",
            (rel_path_str, mtime, content_hash)
        )
        active_notes[nid] = note
        total_chars += note['chars']
        count += 1
    # Compile backlinks
    for nid, note in active_notes.items():
        links = re.findall(r'\[\[(.*?)\]\]', note['body'])
        for link in links:
            target = link.split('|')[0].strip()
            target_id = target.replace('.md', '').lower().replace('\\', '/')
            
            resolved_id = None
            if target_id in active_notes:
                resolved_id = target_id
            else:
                parent_folder = str(note['rel_path'].parent).replace('\\', '/')
                if parent_folder != '.':
                    candidate = f"{parent_folder}/{target_id}".strip('/')
                    if candidate in active_notes:
                        resolved_id = candidate
                        
            if resolved_id:
                db.execute(
                    "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
                    (nid, resolved_id)
                )
                
    db.commit()
    db.close()
    
    # Cleanup target WAL sidecars
    for suffix in ['-wal', '-shm']:
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            try: sidecar.unlink()
            except OSError: pass
                
    # Atomic replace
    try:
        os.replace(str(tmp_db_path), str(db_path))
        try:
            target_db = sqlite3.connect(str(db_path), timeout=5.0)
            target_db.execute("PRAGMA journal_mode=WAL;")
            target_db.close()
        except Exception as e:
            print(f"Warning: Could not enable WAL mode: {e}")
    except Exception as e:
        if tmp_db_path.exists():
            try: tmp_db_path.unlink()
            except OSError: pass
        if lock_file and fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        raise e
        
    est_tokens = total_chars // 4
    print(f"Successfully indexed {count} memories in SQLite database: {db_path}")
    print(f"Memory corpus size: ~{est_tokens} tokens ({total_chars} characters).")
    
    # Old sessions search
    for nid, note in active_notes.items():
        if 'sessions/' in nid:
            try:
                up_date = datetime.date.fromisoformat(note['updated'][:10])
                if (today_date - up_date).days > 30:
                    compaction_candidates.append(f"memory/{note['rel_path']} [SESSION OLDER THAN 30 DAYS: move to sessions/archive/]")
            except:
                pass
                
    # Regenerate MEMORY.md index from DB
    try:
        _regenerate_memory_md(source, db_path)
    except Exception as e:
        print(f"  Warning: Failed to regenerate MEMORY.md: {e}")
    # Print compaction
    if est_tokens > 50000 or compaction_candidates:
        print("\n" + "=" * 80)
        if est_tokens > 50000:
            print(f"WARNING: Active memory corpus is ~{est_tokens} tokens, exceeding the 50,000-token budget.")
        print("INTERACTIVE COMPACTION SUGGESTIONS:")
        for idx, candidate in enumerate(compaction_candidates[:10], 1):
            print(f"  {idx}. {candidate}")
        if len(compaction_candidates) > 10:
            print(f"  ... and {len(compaction_candidates) - 10} more candidates.")
        print("=" * 80)
        
    if lock_file and fcntl:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

if __name__ == '__main__':
    source_dir = 'memory'
    db_path = 'memory/memory.db'
    if len(sys.argv) > 1:
        source_dir = sys.argv[1]
    if len(sys.argv) > 2:
        db_path = sys.argv[2]
        
    rebuild_index(source_dir, db_path)
