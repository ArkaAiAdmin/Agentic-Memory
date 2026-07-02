#!/usr/bin/env python3
import os
import sys
import re
import sqlite3
import logging
from pathlib import Path
import json
import datetime
import hashlib
import math
import time
from typing import Optional

__all__ = ["rebuild_index", "_rebuild_index_body"]
import unicodedata
from infra.memory_common import safe_close_db

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

from infra.memory_common import (
    parse_frontmatter,
    atomic_write,
    run_db_migrations,
)  # noqa: E402


def _normalize_unicode(text: Optional[str]) -> Optional[str]:
    """NFKC normalize a string for FTS5 indexing. Idempotent.

    Accepts None and returns None; the function is called on
    arbitrary inputs from disk content and the codebase convention
    treats None as "no value, leave alone".
    """
    if text is None:
        return text
    return unicodedata.normalize("NFKC", text)


def _regenerate_memory_md(source: Path, db_path):
    """Regenerate MEMORY.md index from database contents."""
    db_path = Path(db_path)
    mem_dir = source if source.name == "memory" else source / "memory"
    mem_md = mem_dir / "MEMORY.md"

    if not db_path.exists():
        return

    db = sqlite3.connect(str(db_path), timeout=5)
    db.execute("PRAGMA foreign_keys=ON")
    rows = db.execute(
        "SELECT id, source_file, tags, pinned FROM memories ORDER BY id"
    ).fetchall()
    safe_close_db(db)

    from collections import defaultdict

    categories = defaultdict(list)
    for mid, source_file, tags, pinned in rows:
        cat = mid.split("/")[0] if "/" in mid else "other"
        slug = mid.split("/")[-1] if "/" in mid else mid
        categories[cat].append((mid, source_file, slug, pinned))

    headers = {
        "projects": "## Active Projects",
        "decisions": "## Architecture Decisions (ADRs)",
        "lessons": "## Hard-Won Lessons",
        "preferences": "## User Preferences",
        "quirks": "## Quirks & Known Issues",
        "sessions": "## Session Logs",
        "docs": "## Documentation",
        "other": "## Other",
    }

    lines = [
        "---",
        "created: 2026-01-01T00:00:00",
        "updated: 2026-01-01T00:00:00",
        "observed_at: 2026-01-01T00:00:00",
        "tags: [index, memory-system]",
        "pinned: true",
        "importance: 5",
        "decay: none",
        "related: []",
        "---",
        "",
        "# Agentic Memory Index",
        "",
    ]

    for cat in [
        "projects",
        "decisions",
        "lessons",
        "preferences",
        "quirks",
        "sessions",
        "docs",
        "other",
    ]:
        if cat in categories:
            lines.append(headers.get(cat, f"## {cat.title()}"))
            for mid, source_file, slug, pinned in categories[cat]:
                title = slug.replace("-", " ").title()
                pin = " 📌" if pinned else ""
                lines.append(f"- [[{source_file}]] — {title}{pin}")
            lines.append("")

    atomic_write(mem_md, "\n".join(lines), encoding="utf-8")


def rebuild_index(source_dir, db_path):
    source = Path(source_dir).resolve()

    lock_file = None
    if fcntl:
        try:
            lock_path = source / ".rebuild.lock"
            lock_file = open(lock_path, "w")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info(
                "Another index rebuild is already running. Waiting for it to finish..."
            )
            if lock_file is not None:
                deadline = time.monotonic() + 600  # 10-minute safety cap
                while True:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise TimeoutError(
                                "Index rebuild lock held for >10 minutes; aborting. "
                                "If you believe this is stale, remove "
                                ".rebuild.lock and retry."
                            )
                        time.sleep(0.5)
        except Exception as e:
            logger.warning("Could not acquire process lock: %s", e)
            if lock_file:
                lock_file.close()
            lock_file = None

    # C3 fix: wrap the entire body in try/finally so the lock is always released,
    # even if an exception fires mid-rebuild.
    try:
        _rebuild_index_body(source_dir, db_path, source, lock_file)
    finally:
        if lock_file and fcntl:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_file.close()
            except Exception:
                pass


def _rebuild_index_body(source_dir, db_path, source, lock_file):
    # All the body of the original rebuild_index goes here, minus the final
    # lock release (now handled by the outer try/finally).

    logger.info("Scanning markdown files in: %s ...", source)
    if not source.exists():
        logger.error("Source directory %s does not exist.", source)
        # C3 fix: lock is released by outer try/finally.
        sys.exit(1)

    stats_map = {}
    file_mtimes_map = {}
    cached_notes_map = {}
    db_file_path = Path(db_path)
    if db_file_path.exists():
        try:
            old_db = sqlite3.connect(str(db_file_path), timeout=5.0)
            old_db.execute("PRAGMA foreign_keys=ON")
            cursor = old_db.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
            )
            if cursor.fetchone():
                cursor.execute("SELECT id, access_count, success_score FROM memories")
                for row in cursor.fetchall():
                    old_id = row[0]
                    stats_map[old_id] = (row[1], row[2])
                    # Tolerate id format drift between old (.md suffix) and new (no suffix).
                    alt = old_id[:-3] if old_id.endswith(".md") else old_id + ".md"
                    stats_map[alt] = (row[1], row[2])
                # Load full cached records for incremental indexing
                cursor.execute("PRAGMA table_info(memories)")
                memories_cols = {row[1] for row in cursor.fetchall()}
                required_cols = [
                    "id",
                    "content",
                    "source_file",
                    "tags",
                    "created_at",
                    "updated_at",
                    "observed_at",
                    "pinned",
                    "importance",
                    "decay",
                    "score",
                    "supersedes",
                    "repo_id",
                    "access_count",
                    "success_score",
                    "fitness_score",
                    "consolidation_state",
                ]
                # Validate columns against allowlist to prevent SQL injection
                available_cols = [c for c in required_cols if c in memories_cols]
                if "source_file" in available_cols:
                    # Safe: columns validated against required_cols allowlist
                    col_query = ", ".join(available_cols)
                    cursor.execute(f"SELECT {col_query} FROM memories")
                    for row in cursor.fetchall():
                        note_data = dict(zip(available_cols, row))
                        cached_notes_map[note_data["source_file"]] = note_data
            # Load file_mtimes for incremental indexing (handle both old and new column names)
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='file_mtimes'"
            )
            if cursor.fetchone():
                cursor.execute("PRAGMA table_info(file_mtimes)")
                cols = {row[1] for row in cursor.fetchall()}
                # Validate columns against allowlist to prevent SQL injection
                allowed_file_mtimes_cols = {
                    "path",
                    "file_path",
                    "mtime",
                    "content_hash",
                }
                path_col = (
                    "path"
                    if "path" in cols
                    else ("file_path" if "file_path" in cols else None)
                )
                if path_col and path_col in allowed_file_mtimes_cols:
                    has_hash = (
                        "content_hash" in cols
                        and "content_hash" in allowed_file_mtimes_cols
                    )
                    hash_select = ", content_hash" if has_hash else ""
                    # Safe: columns validated against required_cols allowlist
                    cursor.execute(
                        f"SELECT {path_col}, mtime {hash_select} FROM file_mtimes"
                    )
                    for row in cursor.fetchall():
                        file_mtimes_map[row[0]] = {
                            "mtime": row[1],
                            "content_hash": row[2] if has_hash else "",
                        }
            if file_mtimes_map:
                logger.info(
                    "Loaded %d file mtimes for incremental indexing.",
                    len(file_mtimes_map),
                )
            safe_close_db(old_db)
        except Exception as e:
            logger.warning("Could not load stats from old database: %s", e)
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
            logger.warning("Cannot scan directory %s: %s", directory, e)
            return

        for entry in entries:
            try:
                entry_path = Path(entry.path)
                try:
                    resolved = entry_path.resolve()
                except OSError as e:
                    logger.warning("Cannot resolve path %s: %s", entry_path, e)
                    continue
                if resolved in processed_paths:
                    continue
                try:
                    is_directory = entry.is_dir()
                except OSError as exc:
                    logger.debug(
                        "rebuild_index: cannot check is_dir for %s: %s", entry.path, exc
                    )
                    is_directory = False
                try:
                    is_file = entry.is_file()
                except OSError as exc:
                    logger.debug(
                        "rebuild_index: cannot check is_file for %s: %s",
                        entry.path,
                        exc,
                    )
                    is_file = False
                if is_directory:
                    # Skip global symlink directory to avoid indexing global memories during local rebuild
                    if entry.name == "global" and Path(entry.path).is_symlink():
                        logger.info("Skipping global symlink directory: %s", entry.path)
                        continue
                    if entry.name not in (
                        ".git",
                        "venv",
                        ".venv",
                        "env",
                        "node_modules",
                        "__pycache__",
                        ".pytest_cache",
                        "archive",
                    ):
                        scan_dir(entry.path)
                elif is_file and entry.name.endswith(".md"):
                    if (
                        entry.name in ("MEMORY.md", "setup_memory.sh")
                        or "setup_memory" in entry.name
                    ):
                        continue
                    # F8: handle symlinks whose TARGET is outside the source dir.
                    # `entry_path.relative_to(source)` would silently succeed for a
                    # symlink whose link path is inside source, even if the target
                    # is elsewhere. We must check the *resolved* path.
                    try:
                        rel_path = str(entry_path.relative_to(source))
                    except ValueError as e:
                        logger.warning(
                            "Skipping %s (outside source dir: %s)", entry_path, e
                        )
                        continue
                    try:
                        source_resolved = source.resolve()
                        resolved.relative_to(source_resolved)
                    except ValueError as e:
                        logger.warning(
                            "Skipping %s (symlink target outside source: %s)",
                            entry_path,
                            e,
                        )
                        continue
                    try:
                        current_mtime = entry_path.stat().st_mtime
                        if (
                            rel_path in file_mtimes_map
                            and current_mtime == file_mtimes_map[rel_path]["mtime"]
                            and rel_path in cached_notes_map
                        ):
                            # File hasn't changed, skip processing and preserve cached note
                            cached_notes_to_keep[rel_path] = cached_notes_map[rel_path]
                            continue
                    except OSError as exc:
                        logger.debug(
                            "rebuild_index: cannot stat %s: %s", entry_path, exc
                        )
                    # Safe size check (limit to 10MB)
                    try:
                        file_size = entry_path.stat().st_size
                        if file_size > 10 * 1024 * 1024:
                            logger.warning(
                                "Skipping %s because it is too large (%d bytes).",
                                entry_path,
                                file_size,
                            )
                            continue
                    except OSError as e:
                        logger.warning("Could not stat %s: %s", entry_path, e)
                        continue
                    # Safe binary check
                    try:
                        with open(entry_path, "rb") as f:
                            chunk = f.read(1024)
                            if b"\x00" in chunk:
                                logger.warning("Skipping binary file %s", entry_path)
                                continue
                    except OSError as e:
                        logger.warning(
                            "Could not perform binary check on %s: %s", entry_path, e
                        )
                        continue
                    try:
                        content = entry_path.read_text(
                            encoding="utf-8", errors="strict"
                        )
                        raw_notes[entry_path] = content
                    except UnicodeDecodeError:
                        logger.warning(
                            "Skipping non-UTF-8 file %s (contains invalid byte sequences)",
                            entry_path,
                        )
                        continue
                    except Exception as e:
                        logger.warning("Could not read %s: %s", entry_path, e)
                        continue
            except Exception as e:
                logger.warning(
                    "Error processing entry %s in %s: %s", entry.name, directory, e
                )

    scan_dir(source)

    all_notes = {}
    superseded_by = {}
    decayed_notes = []
    expired_notes = []

    today_date = datetime.date.today()
    repo_name = source.name
    if repo_name == "memory" and source.parent:
        repo_name = source.parent.name

    for path, content in raw_notes.items():
        try:
            rel_path = path.relative_to(source)
        except ValueError:
            rel_path = path.name

        note_id = str(rel_path.with_suffix("")).lower().replace("\\", "/")
        metadata, body = parse_frontmatter(content)

        # F9: fall back to file mtime when frontmatter has no `created` field.
        # Previously this fell back to today, which made un-frontmattered files
        # appear "fresh" and prevented them from ever going cold.
        try:
            file_mtime_date = datetime.date.fromtimestamp(
                path.stat().st_mtime
            ).isoformat()
        except OSError as exc:
            logger.debug("rebuild_index: cannot stat %s: %s", path, exc)
            file_mtime_date = today_date.isoformat()
        created = metadata.get("created", file_mtime_date)
        updated = metadata.get("updated", created)
        observed_at = metadata.get("observed_at", created)

        def normalize_dt(val):
            s = str(val)
            if len(s) == 10 and s[4] == "-":
                return s + "T00:00:00"
            return s

        created = normalize_dt(created)
        updated = normalize_dt(updated)
        observed_at = normalize_dt(observed_at)
        # Safe Expiry check (TTL)
        expires = metadata.get("expires")
        if expires:
            try:
                exp_date = datetime.date.fromisoformat(str(expires))
                if exp_date <= today_date:
                    expired_notes.append((note_id, str(rel_path), str(expires)))
                    continue
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid expires date format '%s' in %s. Expected YYYY-MM-DD.",
                    expires,
                    rel_path,
                )
        # Namespace scoping (repo_id is NULL for global folders, repo_name for local)
        if str(rel_path).startswith("global/"):
            repo_id = None
        else:
            repo_id = repo_name
        # Get preserved stats
        old_access, old_success = stats_map.get(note_id, (1, 0.0))
        # Decay calculation
        decay_setting = str(metadata.get("decay", "none")).lower()
        decay_rates = {"none": 0.0, "standard": 0.01, "fast": 0.1}
        decay_rate = decay_rates.get(decay_setting, 0.0)

        try:
            updated_date = datetime.date.fromisoformat(str(updated)[:10])
        except (ValueError, TypeError):
            updated_date = today_date

        days_since_update = (today_date - updated_date).days
        decay_score = math.exp(-decay_rate * days_since_update)

        # Memetic fitness score calculation:
        # F = w_r * recency + w_f * log(1 + access) + w_s * success_score
        # Note: log term is capped at log1p(100) to bound the contribution
        # of very-high-access notes (matches save_pipeline._recalculate_fitness_scores).
        w_r, w_f, w_s = 0.4, 0.3, 0.3
        fitness_score = (
            (w_r * decay_score)
            + (w_f * min(math.log1p(old_access), math.log1p(100)))
            + (w_s * old_success)
        )

        # Evict decayed notes (score < 0.25)
        if (
            fitness_score < 0.25
            and decay_setting != "none"
            and not metadata.get("pinned", False)
        ):
            decayed_notes.append((note_id, str(rel_path), fitness_score))
            continue

        try:
            importance = int(metadata.get("importance", 3))
        except (ValueError, TypeError):
            importance = 3

        supersedes = metadata.get("supersedes")
        if supersedes:
            target_id = str(supersedes).replace(".md", "").lower().replace("\\", "/")
            superseded_by[target_id] = note_id

        # C8: temporal validity — parse from frontmatter, default valid_from to
        # created_at. valid_to defaults to NULL (still valid); superseded_by is
        # filled in a second pass after all_notes is fully populated.
        valid_from_meta = metadata.get("valid_from")
        if valid_from_meta:
            valid_from = normalize_dt(valid_from_meta)
        else:
            valid_from = str(created)
        valid_to_meta = metadata.get("valid_to")
        valid_to = normalize_dt(valid_to_meta) if valid_to_meta else None

        all_notes[note_id] = {
            "path": path,
            "_debug_id": note_id,
            "rel_path": rel_path,
            "body": body,
            "metadata": metadata,
            "created": str(created),
            "updated": str(updated),
            "pinned": 1 if metadata.get("pinned", False) else 0,
            "importance": importance,
            "decay": decay_setting,
            "score": decay_score,
            "supersedes": str(supersedes) if supersedes else None,
            "repo_id": repo_id,
            "access_count": old_access,
            "success_score": old_success,
            "fitness_score": fitness_score,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "superseded_by": None,
            "last_accessed": metadata.get("last_accessed") or str(updated),
            "chars": len(content),
        }
    # Merge cached notes that didn't change
    for rel_path, cached_info in cached_notes_to_keep.items():
        note_id = cached_info["id"]
        supersedes = cached_info.get("supersedes")
        if supersedes:
            target_id = str(supersedes).replace(".md", "").lower().replace("\\", "/")
            superseded_by[target_id] = note_id
        metadata = {
            "tags": json.loads(cached_info.get("tags", "[]"))
            if isinstance(cached_info.get("tags"), str)
            else (cached_info.get("tags") or []),
            "pinned": bool(cached_info.get("pinned", 0)),
            "importance": cached_info.get("importance", 3),
            "decay": cached_info.get("decay", "none"),
            "supersedes": supersedes,
            "consolidation_state": cached_info.get("consolidation_state", "working"),
        }
        all_notes[note_id] = {
            "path": source / rel_path,
            "rel_path": Path(rel_path),
            "body": cached_info["content"],
            "metadata": metadata,
            "created": cached_info.get("created_at"),
            "updated": cached_info.get("updated_at"),
            "pinned": 1 if cached_info.get("pinned", 0) else 0,
            "importance": cached_info.get("importance", 3),
            "decay": cached_info.get("decay", "none"),
            "score": cached_info.get("score", 1.0),
            "supersedes": supersedes,
            "repo_id": cached_info.get("repo_id"),
            "access_count": cached_info.get("access_count", 1),
            "success_score": cached_info.get("success_score", 0.0),
            "fitness_score": cached_info.get("fitness_score", 1.0),
            "valid_from": cached_info.get("valid_from")
            or cached_info.get("created_at"),
            "valid_to": cached_info.get("valid_to"),
            "superseded_by": cached_info.get("superseded_by"),
            "last_accessed": cached_info.get("last_accessed")
            or cached_info.get("updated_at"),
            "chars": len(cached_info["content"]),
        }
    tmp_db_path = db_file_path.with_suffix(".db.tmp")
    db = sqlite3.connect(str(tmp_db_path), timeout=30.0)
    db.execute("PRAGMA foreign_keys=ON")
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
        consolidation_state TEXT DEFAULT 'working',
        valid_from    TEXT,
        valid_to      TEXT,
        superseded_by TEXT,
        last_accessed TEXT,
        deleted_at    TEXT,
        deleted_by    TEXT,
        context_prefix TEXT,
        category      TEXT,
        tier          TEXT,
        importance_score REAL,
        metadata      TEXT
    );
    """)
    # Re-create the embedding cache table on full rebuild: the previous
    # rows' memory_ids may not survive the DROP/CREATE of memories above
    # (and we DROP memories_fts in the same step), so any cached vectors
    # are now stale. We re-encode in bulk below (best-effort — if the
    # model isn't installed, the table is empty and the search path
    # falls back to on-the-fly encoding for misses).
    db.execute("DROP TABLE IF EXISTS memory_embeddings;")
    db.execute(
        """
        CREATE TABLE memory_embeddings (
            memory_id       TEXT PRIMARY KEY,
            content_hash    TEXT NOT NULL,
            embedding       BLOB NOT NULL,
            model_revision  TEXT NOT NULL,
            dim             INTEGER NOT NULL,
            updated_at      REAL NOT NULL,
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        );
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_hash ON memory_embeddings(content_hash);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_revision ON memory_embeddings(model_revision);"
    )

    db.execute("""
    CREATE VIRTUAL TABLE memories_fts USING fts5(
        id,
        content,
        tags,
        category,
        tokenize='porter unicode61'
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
      INSERT INTO memories_fts(rowid, id, content, tags, category)
      VALUES (new.rowid, new.id, new.content, new.tags, new.category);
    END;
    """)
    db.execute("""
    CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
      DELETE FROM memories_fts WHERE rowid = old.rowid;
    END;
    """)
    db.execute("""
    CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
      DELETE FROM memories_fts WHERE rowid = old.rowid;
      INSERT INTO memories_fts(rowid, id, content, tags, category)
      VALUES (new.rowid, new.id, new.content, new.tags, new.category);
    END;
    """)
    # Performance indexes for common query patterns
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_repo_id ON memories(repo_id);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_pinned ON memories(pinned);")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_consolidation_state ON memories(consolidation_state);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_observed_at ON memories(observed_at);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_fitness_score ON memories(fitness_score);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_source_file ON memories(source_file);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_backlinks_target_id ON backlinks(target_id);"
    )
    # C8: indexes for temporal validity filtering
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_valid_to ON memories(valid_to);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_valid_from ON memories(valid_from);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_superseded_by ON memories(superseded_by);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_last_accessed ON memories(last_accessed);"
    )
    # Run schema migrations so any new tables (e.g. memory_chunks) are created
    run_db_migrations(db)
    count = 0
    total_chars = 0
    compaction_candidates = []

    for nid, rpath, exp in expired_notes:
        logger.info("Skipping expired memory: %s (Expired: %s)", rpath, exp)
        compaction_candidates.append(f"memory/{rpath} [EXPIRED: delete file]")

    for nid, rpath, fit_score in decayed_notes:
        logger.info("Skipping decayed memory: %s (Fitness: %.2f)", rpath, fit_score)
        compaction_candidates.append(f"memory/{rpath} [DECAYED: move to archive/]")

    # C8: second pass — fill in `valid_to` and `superseded_by` on notes that
    # are the target of a supersession link. Use the new note's created_at
    # as the cutoff so search can filter by valid_to IS NULL.
    for old_nid, new_nid in superseded_by.items():
        if old_nid in all_notes and new_nid in all_notes:
            all_notes[old_nid]["valid_to"] = all_notes[new_nid]["created"]
            all_notes[old_nid]["superseded_by"] = new_nid
            logger.info(
                "Marking superseded: %s -> valid_to=%s (by %s)",
                old_nid,
                all_notes[new_nid]["created"],
                new_nid,
            )

    # Write notes
    active_notes = {}
    for nid, note in all_notes.items():
        # C8: no longer skip superseded notes — they stay in the DB with
        # valid_to set so search can filter them out. The compaction
        # suggestion list still flags them for archive.
        if nid in superseded_by:
            by_note = superseded_by[nid]
            compaction_candidates.append(
                f"memory/{note['rel_path']} [SUPERSEDED by {by_note}: archive candidate]"
            )

        tags_json = json.dumps(note["metadata"].get("tags", []))

        try:
            db.execute(
                """INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at, pinned, importance, decay, score, supersedes, repo_id, access_count, success_score, fitness_score, conflict_policy, version_vector, logical_clock, consolidation_state, valid_from, valid_to, superseded_by, last_accessed, category)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    nid,
                    _normalize_unicode(note["body"].strip()),
                    str(note["rel_path"]),
                    _normalize_unicode(tags_json),
                    note["created"],
                    note["updated"],
                    note.get("observed_at", note["created"]),
                    note["pinned"],
                    note["importance"],
                    note["decay"],
                    note["score"],
                    note["supersedes"],
                    note["repo_id"],
                    note["access_count"],
                    note["success_score"],
                    note["fitness_score"],
                    "supersede",
                    "{}",
                    0,
                    # C7 fix: check both the note dict and its nested metadata dict
                    # for consolidation_state, since metadata may have it cached.
                    note.get("consolidation_state")
                    or note["metadata"].get("consolidation_state", "working"),
                    note.get("valid_from"),
                    note.get("valid_to"),
                    note.get("superseded_by"),
                    note.get("last_accessed") or note["updated"],
                    str(note["rel_path"].parent)
                    if note["rel_path"].parent != Path(".")
                    else None,
                ),
            )
        except Exception:
            safe_close_db(db)
            if tmp_db_path.exists():
                try:
                    tmp_db_path.unlink()
                except OSError as exc:
                    logger.debug("rebuild_index: cannot unlink tmp db: %s", exc)
            raise
        # Update file_mtimes table
        import time

        rel_path_str = str(note["rel_path"])
        if rel_path_str in file_mtimes_map:
            mtime = file_mtimes_map[rel_path_str]["mtime"]
            content_hash = file_mtimes_map[rel_path_str]["content_hash"]
        else:
            # Handle case where cached note's file no longer exists
            try:
                mtime = (
                    note["rel_path"].stat().st_mtime
                    if note["rel_path"].exists()
                    else time.time()
                )
            except (OSError, AttributeError):
                mtime = time.time()
            content_hash = hashlib.md5(note["body"].strip().encode()).hexdigest()
        db.execute(
            """INSERT INTO file_mtimes (path, mtime, content_hash) VALUES (?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, content_hash = excluded.content_hash""",
            (rel_path_str, mtime, content_hash),
        )
        active_notes[nid] = note
        total_chars += note["chars"]
        count += 1
    # Compile backlinks
    for nid, note in active_notes.items():
        links = re.findall(r"\[\[(.*?)\]\]", note["body"])
        for link in links:
            target = link.split("|")[0].strip()
            target_id = target.replace(".md", "").lower().replace("\\", "/")

            resolved_id = None
            if target_id in active_notes:
                resolved_id = target_id
            else:
                parent_folder = str(note["rel_path"].parent).replace("\\", "/")
                if parent_folder != ".":
                    candidate = f"{parent_folder}/{target_id}".strip("/")
                    if candidate in active_notes:
                        resolved_id = candidate

            if resolved_id:
                db.execute(
                    "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
                    (nid, resolved_id),
                )

    # Batch-fill the embedding cache for all active notes. Best-effort:
    # if model2vec isn't installed, this silently no-ops and the cache
    # stays empty. The search path will then encode on-the-fly and
    # save-back per row, which is the same end state just slower.
    try:
        from infra._lazy_imports import get_embedding_search

        es = get_embedding_search()
        if es.model is not None:
            items = [(nid, note["body"]) for nid, note in active_notes.items()]
            written = es.index_embeddings_batch(db, items)
            logger.info("Embedding cache: wrote %d rows", written)
    except Exception as e:
        logger.warning("Embedding cache fill skipped: %s", e)

    # Populate FTS virtual tables that use external content.
    # memory_chunks_fts references memory_chunks as its content table;
    # rebuild_index creates the virtual table but does not auto-populate it.
    try:
        has_chunks = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_chunks'"
        ).fetchone()
        has_fts = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_chunks_fts'"
        ).fetchone()
        if has_chunks and has_fts:
            existing = db.execute("SELECT COUNT(*) FROM memory_chunks_fts").fetchone()[
                0
            ]
            if existing == 0:
                rows = db.execute("SELECT id, content FROM memory_chunks").fetchall()
                for _cid, _content in rows:
                    if _content:
                        db.execute(
                            "INSERT INTO memory_chunks_fts(rowid, content) VALUES (?, ?)",
                            (_cid, _content),
                        )
                logger.info("Populated memory_chunks_fts with %d rows", len(rows))
    except Exception as e:
        logger.warning("Could not populate memory_chunks_fts: %s", e)

    db.commit()
    safe_close_db(db)

    # Cleanup target WAL sidecars before swapping
    try:
        for suffix in ("-wal", "-shm"):
            p = str(db_path) + suffix
            if os.path.exists(p):
                os.unlink(p)
    except Exception:
        pass
    try:
        # Sprint 2 Task 2: this site intentionally uses os.replace,
        # not atomic_write. atomic_write writes `content` to a temp
        # file then renames it; here we already have a fully-built
        # temp DB at `tmp_db_path` (created earlier in this function
        # and populated row-by-row), so the atomic step is just the
        # rename. os.replace is that atomic rename, same as what
        # atomic_write calls under the hood. Reading the whole DB
        # back into memory just to hand it to atomic_write would be
        # wasteful and would not make the swap any safer.
        os.replace(str(tmp_db_path), str(db_path))
        # Evict stale pool connection: os.replace swapped the file
        # underneath, so any cached connection points to the old data.
        try:
            from infra._lazy_imports import connection_pool

            connection_pool.close(str(db_path))
        except Exception:
            pass
        try:
            target_db = sqlite3.connect(str(db_path), timeout=5.0)
            target_db.execute("PRAGMA foreign_keys=ON")
            target_db.execute("PRAGMA journal_mode=WAL;")
            safe_close_db(target_db)
        except Exception as e:
            logger.warning("Could not enable WAL mode: %s", e)
    except Exception:
        if tmp_db_path.exists():
            try:
                tmp_db_path.unlink()
            except OSError as exc:
                logger.debug("rebuild_index: cannot unlink tmp db: %s", exc)
        # C3 fix: lock release handled by outer try/finally.
        raise

    est_tokens = total_chars // 4
    # CLI: keep print for user-facing output
    print(f"Successfully indexed {count} memories in SQLite database: {db_path}")
    print(f"Memory corpus size: ~{est_tokens} tokens ({total_chars} characters).")

    for nid, note in active_notes.items():
        if "sessions/" in nid:
            try:
                up_date = datetime.date.fromisoformat(note["updated"][:10])
                if (today_date - up_date).days > 30:
                    compaction_candidates.append(
                        f"memory/{note['rel_path']} [SESSION OLDER THAN 30 DAYS: move to sessions/archive/]"
                    )
            except Exception:
                pass

    try:
        _regenerate_memory_md(source, db_path)
    except Exception as e:
        logger.warning("Failed to regenerate MEMORY.md: %s", e)
    if est_tokens > 50000 or compaction_candidates:
        # CLI: keep print for user-facing output
        print("\n" + "=" * 80)
        if est_tokens > 50000:
            print(
                f"WARNING: Active memory corpus is ~{est_tokens} tokens, exceeding the 50,000-token budget."
            )
        print("INTERACTIVE COMPACTION SUGGESTIONS:")
        for idx, candidate in enumerate(compaction_candidates[:10], 1):
            print(f"  {idx}. {candidate}")
        if len(compaction_candidates) > 10:
            print(f"  ... and {len(compaction_candidates) - 10} more candidates.")
        print("=" * 80)

    # C3 fix: lock release handled by outer try/finally.


if __name__ == "__main__":
    if len(sys.argv) > 1:
        source_dir = sys.argv[1]
    else:
        from infra.memory_config import get_memory_paths

        _, local_mem, _ = get_memory_paths()
        source_dir = str(local_mem)
    if len(sys.argv) > 2:
        db_path = sys.argv[2]
    else:
        db_path = str(Path(source_dir) / "memory.db")

    rebuild_index(source_dir, db_path)
