"""User Profiling for agentic-memory.

Tracks what topics, categories, and tags the user frequently accesses.
Builds a preference profile for personalized search ranking.

Opt-in via MEMORY_USER_PROFILE=1.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from config import resolve_db_path
from infra.db_write_queue import sqlite_write_queue
from infra.memory_common import GLOBAL_MEM_DIR

__all__ = [
    "PROFILE_ENABLED",  # noqa: F822 — dynamically resolved via __getattr__
    "record_access",
    "get_user_profile",
    "personalize_results",
    "profile_stats",
]

# PROFILE_ENABLED is dynamically resolved via __getattr__

_ACCESS_WINDOW_DAYS = 90
_MAX_PROFILE_SIZE = 50
_RECENCY_HALF_LIFE_DAYS = 30  # days for exponential decay of access weight
try:
    from config import get_config as _get_up_cfg

    _ACCESS_WINDOW_DAYS = int(getattr(_get_up_cfg(), "user_profile_window_days", 90))
    _MAX_PROFILE_SIZE = int(getattr(_get_up_cfg(), "user_profile_max_size", 50))
    _RECENCY_HALF_LIFE_DAYS = int(
        getattr(_get_up_cfg(), "user_profile_recency_half_life_days", 30)
    )
except Exception:
    pass


def _decay_weight(days_since_access: float) -> float:
    """Exponential decay weight based on days since last access."""
    return 2 ** (-days_since_access / _RECENCY_HALF_LIFE_DAYS)


def record_access(
    note_id: str,
    source: str = "search",
    category: str | None = None,
    tags: list[str] | None = None,
    db_path: str | None = None,
) -> bool:
    """Record that a note was accessed by the user.

    Args:
        note_id: the note that was accessed
        source: how it was accessed (search, list, explicit_read)
        category: note category (for topic tracking)
        tags: note tags (for topic tracking)
        db_path: optional path to memory.db

    Returns:
        True if recorded successfully
    """
    import sys

    if not sys.modules[__name__].PROFILE_ENABLED:
        return False

    if db_path is not None:
        local_mem = resolve_db_path(db_path).parent
        global_mem = Path(GLOBAL_MEM_DIR)
    else:
        try:
            from infra._lazy_imports import get_memory_paths

            _, local_mem, global_mem = get_memory_paths()
        except ImportError:
            return False
    db = db_path if db_path is not None else str(local_mem / "memory.db")

    try:
        conn = sqlite_write_queue.start_session(Path(db))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_profile_access_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id TEXT NOT NULL,
                    source TEXT DEFAULT 'search',
                    category TEXT,
                    tags TEXT,
                    accessed_at REAL NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO user_profile_access_log (note_id, source, category, tags, accessed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (note_id, source, category, json.dumps(tags or []), time.time()),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as _e:
        import logging

        logging.getLogger(__name__).debug("user_profile.record_access: error: %s", _e)
        return False


def get_user_profile(
    db_path: str | None = None, window_days: int = _ACCESS_WINDOW_DAYS
) -> dict:
    """Build a user preference profile from access history.

    Returns:
        dict with:
        - top_categories: list of (category, score) sorted by access frequency
        - top_tags: list of (tag, score) sorted by access frequency
        - top_notes: list of (note_id, access_count) sorted by access count
        - total_accesses: total number of access events in window
        - active_days: number of distinct days with accesses
    """
    from infra._lazy_imports import get_config

    if not get_config().user_profile:
        return {"enabled": False}

    if db_path is not None:
        local_mem = resolve_db_path(db_path).parent
        global_mem = Path(GLOBAL_MEM_DIR)
    else:
        try:
            from infra._lazy_imports import get_memory_paths

            _, local_mem, global_mem = get_memory_paths()
        except ImportError:
            return {"enabled": True, "error": "memory_common not found"}
    db = db_path if db_path is not None else str(local_mem / "memory.db")

    try:
        conn = sqlite_write_queue.start_session(Path(db))
        try:
            # Ensure table exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_profile_access_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id TEXT NOT NULL,
                    source TEXT DEFAULT 'search',
                    category TEXT,
                    tags TEXT,
                    accessed_at REAL NOT NULL
                )
            """)

            cutoff = time.time() - (window_days * 86400)
            rows = conn.execute(
                "SELECT note_id, category, tags, accessed_at "
                "FROM user_profile_access_log WHERE accessed_at > ?",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return {
                "enabled": True,
                "top_categories": [],
                "top_tags": [],
                "top_notes": [],
                "total_accesses": 0,
                "active_days": 0,
            }

        now = time.time()
        cat_scores: Counter[str] = Counter()
        tag_scores: Counter[str] = Counter()
        note_counts: Counter[str] = Counter()
        active_days: set[str] = set()

        for note_id, category, tags_json, accessed_at in rows:
            days_ago = (now - accessed_at) / 86400
            weight = _decay_weight(days_ago)

            if category:
                cat_scores[category] += weight  # type: ignore
            try:
                tags = json.loads(tags_json) if tags_json else []
                for tag in tags:
                    tag_scores[tag] += weight  # type: ignore
            except (json.JSONDecodeError, TypeError):
                pass

            note_counts[note_id] += 1
            day_str = time.strftime("%Y-%m-%d", time.localtime(accessed_at))
            active_days.add(day_str)

        return {
            "enabled": True,
            "top_categories": cat_scores.most_common(_MAX_PROFILE_SIZE),
            "top_tags": tag_scores.most_common(_MAX_PROFILE_SIZE),
            "top_notes": note_counts.most_common(_MAX_PROFILE_SIZE),
            "total_accesses": len(rows),
            "active_days": len(active_days),
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)}


def personalize_results(
    results: list[dict], profile: dict | None = None, boost_factor: float = 1.5
) -> list[dict]:
    """Re-rank search results based on user profile.

    Boosts results whose category or tags match the user's frequent access patterns.

    Args:
        results: search results with optional "category" and "tags" fields
        profile: user profile dict (from get_user_profile). If None, fetched automatically.
        boost_factor: how much to boost matching results (1.0 = no boost)

    Returns:
        re-sorted results list
    """
    import sys

    if not sys.modules[__name__].PROFILE_ENABLED or not results:
        return results

    if profile is None:
        profile = get_user_profile()
    if not profile.get("enabled") or profile.get("error"):
        return results

    # Build lookup sets from profile
    cat_pref = {cat: score for cat, score in profile.get("top_categories", [])}
    tag_pref = {tag: score for tag, score in profile.get("top_tags", [])}

    if not cat_pref and not tag_pref:
        return results

    max_cat = max(cat_pref.values()) if cat_pref else 1
    max_tag = max(tag_pref.values()) if tag_pref else 1

    for r in results:
        base_score = r.get("score", 0) or 0
        boost = 0.0

        cat = r.get("category", "")
        if cat and cat in cat_pref:
            boost += (cat_pref[cat] / max_cat) * (boost_factor - 1.0)

        tags = r.get("tags", [])
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

        for tag in tags:
            if tag in tag_pref:
                boost += (tag_pref[tag] / max_tag) * (boost_factor - 1.0) * 0.5

        r["score"] = base_score * (1.0 + min(boost, boost_factor - 1.0))

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results


def profile_stats(db_path: str | None = None) -> dict:
    """Return user profiling statistics."""
    from infra._lazy_imports import get_config

    if not get_config().user_profile:
        return {"enabled": False}

    if db_path is not None:
        local_mem = resolve_db_path(db_path).parent
        global_mem = Path(GLOBAL_MEM_DIR)
    else:
        try:
            from infra._lazy_imports import get_memory_paths

            _, local_mem, global_mem = get_memory_paths()
        except ImportError:
            return {"enabled": True, "error": "memory_common not found"}
    db = db_path if db_path is not None else str(local_mem / "memory.db")

    try:
        conn = sqlite_write_queue.start_session(Path(db))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_profile_access_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id TEXT NOT NULL,
                    source TEXT DEFAULT 'search',
                    category TEXT,
                    tags TEXT,
                    accessed_at REAL NOT NULL
                )
            """)
            total = conn.execute(
                "SELECT COUNT(*) FROM user_profile_access_log"
            ).fetchone()[0]
            recent = conn.execute(
                "SELECT COUNT(*) FROM user_profile_access_log WHERE accessed_at > ?",
                (time.time() - 86400,),
            ).fetchone()[0]
            unique_notes = conn.execute(
                "SELECT COUNT(DISTINCT note_id) FROM user_profile_access_log"
            ).fetchone()[0]
            return {
                "enabled": True,
                "total_access_events": total,
                "access_events_last_24h": recent,
                "unique_notes_accessed": unique_notes,
            }
        finally:
            conn.close()
    except Exception as _e:
        import logging

        logging.getLogger(__name__).debug("user_profile.profile_stats: error: %s", _e)
        return {"enabled": True, "error": "stats unavailable"}


from infra.memory_common import make_lazy_getattr

__getattr__ = make_lazy_getattr({"PROFILE_ENABLED": "user_profile"})
