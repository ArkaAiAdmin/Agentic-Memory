#!/usr/bin/env python3
"""Phase 4A: Orphan index repair cron job.

Finds memories that exist in the DB but lack embeddings or vec_keys
(indicating permanently-failed background indexing tasks) and
re-enqueues the appropriate background tasks for them.

Also cleans up permanently-failed tasks older than 7 days from
the task_queue table.

Runs every 6 hours. The 1-hour grace period avoids re-enqueuing tasks
for memories that were just saved and whose background tasks are still
pending.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path for imports.
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

logger = logging.getLogger(__name__)


def find_unindexed_memories(conn) -> list[str]:
    """Find memory IDs that exist in memories but lack embeddings.

    Only considers memories older than 1 hour to avoid racing with
    in-flight background tasks for recently-saved memories.
    """
    try:
        return [row[0] for row in conn.execute(
            "SELECT m.id FROM memories m "
            "LEFT JOIN memory_embeddings e ON e.memory_id = m.id "
            "WHERE e.memory_id IS NULL "
            "AND m.deleted_at IS NULL "
            "AND m.created_at < datetime('now', '-1 hour')"
        ).fetchall()]
    except Exception as exc:
        logger.warning("find_unindexed_memories query failed: %s", exc)
        return []


def find_unvectorized_memories(conn) -> list[str]:
    """Find memory IDs that exist but lack vec_keys.

    Only considers memories older than 1 hour.
    """
    try:
        return [row[0] for row in conn.execute(
            "SELECT m.id FROM memories m "
            "LEFT JOIN memory_vec_keys v ON v.memory_id = m.id "
            "WHERE v.memory_id IS NULL "
            "AND m.deleted_at IS NULL "
            "AND m.created_at < datetime('now', '-1 hour')"
        ).fetchall()]
    except Exception as exc:
        logger.warning("find_unvectorized_memories query failed: %s", exc)
        return []


def cleanup_permanently_failed_tasks(conn, days: int = 7) -> int:
    """Remove permanently-failed tasks older than `days` days.

    Returns the number of rows deleted.
    """
    try:
        cur = conn.execute(
            "DELETE FROM task_queue "
            "WHERE status = 'failed' "
            "AND attempts >= max_attempts "
            "AND created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        return cur.rowcount
    except Exception as exc:
        logger.warning("cleanup_permanently_failed_tasks failed: %s", exc)
        return 0


def repair_unindexed(conn) -> dict:
    """Main repair logic. Returns a summary dict."""
    from background.background_queue import init_task_queue, enqueue_task

    init_task_queue(conn)

    unindexed = set(find_unindexed_memories(conn))
    unvectorized = set(find_unvectorized_memories(conn))
    all_needing_repair = unindexed | unvectorized

    enqueued = 0
    for note_id in all_needing_repair:
        try:
            if note_id in unindexed:
                enqueue_task(conn, "embedding_index", {"note_id": note_id})
            if note_id in unindexed:
                enqueue_task(conn, "kg_and_fact_index", {"note_id": note_id})
            enqueue_task(conn, "semantic_backlinks", {"note_id": note_id})
            enqueued += 1
        except Exception as exc:
            logger.warning(
                "repair_unindexed: failed to enqueue tasks for %s: %s",
                note_id, exc,
            )

    cleaned = cleanup_permanently_failed_tasks(conn)

    summary = {
        "unindexed_found": len(unindexed),
        "unvectorized_found": len(unvectorized),
        "total_repaired": enqueued,
        "stale_tasks_cleaned": cleaned,
    }
    logger.info("repair_unindexed complete: %s", summary)
    return summary


def main():
    """Entry point when run as a standalone cron script."""
    logging.basicConfig(level=logging.INFO)

    # Flock protection: ensure only one instance runs at a time.
    try:
        from _flock import acquire_lock_or_exit
    except ImportError:
        # Fallback if _flock is not importable from cwd
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _flock import acquire_lock_or_exit
    acquire_lock_or_exit("cron_repair_unindexed")

    try:
        from infra._lazy_imports import open_db
        from infra.infrastructure import resolve_active_memory_dir
    except ImportError as exc:
        logger.error("Cannot import required modules: %s", exc)
        sys.exit(1)

    env_db = os.environ.get("MEMORY_DB_PATH")
    db_path = Path(env_db) if env_db else resolve_active_memory_dir() / "memory.db"
    with open_db(db_path) as conn:
        result = repair_unindexed(conn)

    print(f"Repair complete: {result}")


if __name__ == "__main__":
    main()
