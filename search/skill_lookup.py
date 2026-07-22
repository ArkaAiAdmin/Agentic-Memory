"""Skill-first lookup matching query terms."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Cache for skill-first lookups to prevent double-incrementing hit_count
# Bounded LRU via OrderedDict; evicts oldest entries past MAX_SKILL_CACHE.
# Thread-safe: all cache reads and writes are protected by _skill_cache_lock.
_SKILL_CACHE_MAX = 512
_skill_cache: dict[tuple, dict] = {}
_skill_cache_order: list[tuple] = []
_skill_cache_lock = threading.Lock()


def clear_skill_caches() -> None:
    """Clear skill lookup cache."""
    with _skill_cache_lock:
        _skill_cache.clear()
        _skill_cache_order.clear()


def _skill_first_lookup(db_path: Path, terms: list[str], limit: int, tenant_id: str = "default") -> dict | None:
    """Look up skills in memory_skills table matching the query terms."""
    import os
    st_ino = 0
    if db_path and os.path.exists(str(db_path)):
        try:
            st_ino = os.stat(str(db_path)).st_ino
        except OSError:
            pass
    cache_key = (str(db_path), st_ino, tenant_id, tuple(sorted(terms)), limit)
    with _skill_cache_lock:
        if cache_key in _skill_cache:
            return _skill_cache[cache_key]


    try:
        from infra._lazy_imports import connection_pool, safe_close_db

        db = connection_pool.get(str(db_path), timeout=10.0, tenant_id=tenant_id)
    except sqlite3.Error as exc:
        logger.warning("_skill_first_lookup: connection_pool.get failed: %s", exc)
        return None
    try:
        # Check if memory_skills table exists
        try:
            table_check = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_skills'"
            ).fetchone()
        except sqlite3.Error:
            return None
        if not table_check:
            return None

        # Search for skills matching any of the terms
        like_clauses = []
        params = []
        for term in terms:
            like_clauses.append(
                "(name LIKE ? OR topic LIKE ? OR description LIKE ? OR triggers LIKE ?)"
            )
            wild = f"%{term}%"
            params.extend([wild, wild, wild, wild])

        where = " OR ".join(like_clauses)
        try:
            rows = db.execute(
                f"SELECT id, name, source_memory_id, topic, description, triggers, steps, hit_count "
                f"FROM memory_skills WHERE {where} "
                f"ORDER BY hit_count DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        except sqlite3.Error:
            return None

        if not rows:
            return None

        # Batch increment hit_count for matched skills (H3 fix: single UPDATE with IN clause)
        now_ts = time.time()
        skill_ids = [row[0] for row in rows]
        placeholders = ",".join("?" * len(skill_ids))
        try:
            db.execute(
                f"UPDATE memory_skills SET hit_count = hit_count + 1, last_used_at = ? WHERE id IN ({placeholders})",
                [now_ts] + skill_ids,
            )
            db.commit()
        except sqlite3.Error as e:
            logger.warning("_skill_first_lookup: hit_count update failed: %s", e)

        # Format results
        results = []
        for row in rows:
            results.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "source_memory_id": row[2],
                    "topic": row[3],
                    "description": row[4],
                    "is_skill": True,
                    "score": 1.0,
                }
            )

        # Build output text
        output_lines = [f"Skill match: {row[1]}" for row in rows]
        output = "\n".join(output_lines)

        result = {
            "results": results,
            "count": len(results),
            "output": output,
        }
        with _skill_cache_lock:
            _skill_cache[cache_key] = result
            _skill_cache_order.append(cache_key)
            if len(_skill_cache) > _SKILL_CACHE_MAX:
                _oldest = _skill_cache_order.pop(0)
                _skill_cache.pop(_oldest, None)
        return result
    finally:
        try:
            safe_close_db(db)
        except Exception as e:
            logger.warning("_skill_first_lookup: safe_close_db failed: %s", e)
