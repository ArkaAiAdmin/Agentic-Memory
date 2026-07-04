"""Memory Sharing Pool for agentic-memory.

Implements a **shared memory pool** within a single SQLite database,
allowing multiple agent_ids to share selected memories via a
``shared_memories`` table. The actual cross-machine / cross-process
multi-agent sync (peer-to-peer HTTP, CRDT version vectors) lives in
``sync_server.py`` / ``sync_client.py`` — this module is the
*in-process* sharing layer that those flows read from and write to.

**Important naming note:** this module was historically called
``multi_agent`` but the name was misleading. True multi-agent
coordination requires the sync subsystem; this module only manages
a shared table within one database. Renamed to ``memory_sharing``
(2026-06-20). The ``MEMORY_MULTI_AGENT`` env var and the
``features.multi_agent`` config key are preserved for backward
compatibility.

Opt-in via ``MEMORY_MULTI_AGENT=1``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection
import time
from pathlib import Path

logger = logging.getLogger(__name__)
from typing import Any

from config import resolve_db_path

from infra.db_write_queue import sqlite_write_queue

__all__ = [
    "MULTI_AGENT_ENABLED",  # noqa: F822 — dynamically resolved via __getattr__
    "share_memory",
    "list_shared_memories",
    "import_shared_memory",
    "shared_pool_stats",
    "auto_share_high_value",
    "list_share_candidates",
]

# Tunables for auto-share (P2 #1). These are *defaults* — callers may
# pass their own thresholds to ``auto_share_high_value``. Constants are
# module-level so the cron wrapper and the MCP tool agree on the
# definition of "share-worthy".
_AUTO_SHARE_MIN_IMPORTANCE = 4
_AUTO_SHARE_MIN_FITNESS = 0.6
_AUTO_SHARE_MAX_PER_CYCLE = 25

# MULTI_AGENT_ENABLED is dynamically resolved via __getattr__
# _MAX_SHARED_POOL_SIZE is dynamically resolved via __getattr__ from config
# _SHARED_POOL_TTL_DAYS is dynamically resolved via __getattr__
_SHARED_TABLE = "shared_memories"


def _purge_expired_shared(conn: AnyConnection) -> None:
    """Delete TTL-expired entries from the shared pool."""
    import sys

    this_mod = sys.modules[__name__]
    if this_mod._SHARED_POOL_TTL_DAYS > 0:
        cutoff = time.time() - (this_mod._SHARED_POOL_TTL_DAYS * 86400)
        conn.execute(
            f"DELETE FROM {_SHARED_TABLE} WHERE shared_at < ?",
            (cutoff,),
        )


def _ensure_shared_table(conn: AnyConnection) -> None:
    """Create shared_memories table if it doesn't exist."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {_SHARED_TABLE} (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT,
            tags TEXT,
            shared_at REAL NOT NULL,
            source_note_id TEXT,
            metadata TEXT
        )
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_shared_agent ON {_SHARED_TABLE}(agent_id)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_shared_category ON {_SHARED_TABLE}(category)"
    )


def share_memory(note_id: str, agent_id: str, db_path: str | None = None) -> dict:
    """Share a memory from one agent's workspace to the shared pool.

    Args:
        note_id: the note to share
        agent_id: identifier of the agent sharing this memory
        db_path: optional path to memory.db

    Returns:
        dict with status and shared_id
    """
    import sys

    if not sys.modules[__name__].MULTI_AGENT_ENABLED:
        return {"enabled": False}

    if db_path is not None:
        local_mem = resolve_db_path(db_path).parent
    else:
        try:
            from infra._lazy_imports import get_memory_paths

            _, local_mem, _ = get_memory_paths()
        except ImportError:
            return {"enabled": True, "error": "memory_common not found"}
    db = db_path if db_path is not None else str(local_mem / "memory.db")

    try:
        conn = sqlite_write_queue.start_session(Path(db))
        try:
            _ensure_shared_table(conn)

            # B12 fix: BEGIN IMMEDIATE before the SELECT so the existence
            # check and the subsequent write are in the same write
            # transaction.  Otherwise a concurrent unshare could remove
            # the row between our SELECT and INSERT.
            conn.execute("BEGIN IMMEDIATE")
            # Read the source note
            row = conn.execute(
                "SELECT content, category, tags, metadata FROM memories "
                "WHERE id = ? AND deleted_at IS NULL",
                (note_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                return {"enabled": True, "error": f"note {note_id} not found"}

            content, category, tags_json, meta_json = row

            _purge_expired_shared(conn)
            try:
                count_row = conn.execute(
                    f"SELECT COUNT(*) FROM {_SHARED_TABLE}"
                ).fetchone()
                count = int(count_row[0]) if count_row is not None else 0
                this_mod = sys.modules[__name__]
                if count >= this_mod._MAX_SHARED_POOL_SIZE:
                    to_evict = count - this_mod._MAX_SHARED_POOL_SIZE + 1
                    conn.execute(
                        f"DELETE FROM {_SHARED_TABLE} WHERE id IN "
                        f"(SELECT s.id FROM {_SHARED_TABLE} s "
                        f"LEFT JOIN memories m ON s.source_note_id = m.id "
                        f"ORDER BY COALESCE(m.importance, 0) ASC, s.shared_at ASC "
                        f"LIMIT ?)",
                        (to_evict,),
                    )

                shared_id = f"shared:{agent_id}:{note_id}"
                conn.execute(
                    f"INSERT INTO {_SHARED_TABLE} "
                    f"(id, agent_id, content, category, tags, shared_at, source_note_id, metadata) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    f"ON CONFLICT(id) DO UPDATE SET "
                    f"content=excluded.content, category=excluded.category, "
                    f"tags=excluded.tags, shared_at=excluded.shared_at, "
                    f"source_note_id=excluded.source_note_id, metadata=excluded.metadata",
                    (
                        shared_id,
                        agent_id,
                        content,
                        category,
                        tags_json,
                        time.time(),
                        note_id,
                        meta_json,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return {"enabled": True, "shared_id": shared_id, "agent_id": agent_id}
        finally:
            conn.close()
    except Exception as e:
        return {"enabled": True, "error": str(e)}


def list_shared_memories(
    agent_id: str | None = None,
    category: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> list[dict] | dict:
    """List memories in the shared pool.

    Args:
        agent_id: filter by sharing agent
        category: filter by category
        limit: max results
        db_path: optional path to memory.db

    Returns:
        list of shared memory dicts
    """
    import sys

    if not sys.modules[__name__].MULTI_AGENT_ENABLED:
        return []

    if db_path is not None:
        db = db_path
    else:
        try:
            from infra._lazy_imports import get_memory_paths

            _, local_mem, _ = get_memory_paths()
            db = str(local_mem / "memory.db")
        except ImportError:
            return {"enabled": True, "error": "memory_common not found"}

    try:
        from infra.db import open_db
        with open_db(Path(db), pooled=True, write=True) as conn:
            _ensure_shared_table(conn)

            query = f"SELECT id, agent_id, content, category, tags, shared_at, source_note_id FROM {_SHARED_TABLE}"
            params: list = []
            conditions = []
            if agent_id:
                conditions.append("agent_id = ?")
                params.append(agent_id)
            if category:
                conditions.append("category = ?")
                params.append(category)
            this_mod = sys.modules[__name__]
            if this_mod._SHARED_POOL_TTL_DAYS > 0:
                cutoff = time.time() - (this_mod._SHARED_POOL_TTL_DAYS * 86400)
                conditions.append("shared_at >= ?")
                params.append(cutoff)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY shared_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [
                {
                    "id": r[0],
                    "agent_id": r[1],
                    "content": r[2][:200],
                    "category": r[3],
                    "tags": r[4],
                    "shared_at": r[5],
                    "source_note_id": r[6],
                }
                for r in rows
            ]
    except Exception:
        logger.warning("Failed to get pending shared memories")
        return []


def _resolve_import_db_path(db_path: str | None) -> str | dict:
    """Resolve the db_path argument to a usable path or an error dict.

    Returns the path string on success, or a dict like
    ``{"enabled": True, "error": "..."}`` if the path can't be
    resolved. The caller should check ``isinstance(result, dict)`` to
    decide whether to return the error or proceed.

    Extracted 2026-06-22 from import_shared_memory().
    """
    if db_path is not None:
        return db_path
    try:
        from infra._lazy_imports import get_memory_paths

        _, local_mem, _ = get_memory_paths()
        return str(local_mem / "memory.db")
    except ImportError:
        return {"enabled": True, "error": "memory_common not found"}


def _scan_shared_content(content: str, source_agent: str, shared_id: str) -> dict:
    """B7 fix: run the prompt-injection scan on peer-supplied content
    before indexing. Returns the scan result OR a rejection dict if
    the content is high-risk.

    The function decides:
      * risk_score >= 0.5  → hard-reject (return rejection dict)
      * risk_score > 0     → quarantined ("untrusted" tier)
      * risk_score == 0    → trusted ("warm" tier)

    Extracted 2026-06-22 from import_shared_memory().
    """
    from infra._lazy_imports import scan_for_injection

    injection_scan = scan_for_injection(content or "")
    is_suspicious = bool(injection_scan["is_suspicious"])
    risk_score = float(injection_scan["risk_score"])

    if risk_score >= 0.5:
        logger.warning(
            "import_shared_memory: REJECTED high-risk content from "
            "agent %s (shared_id=%s risk=%.2f matches=%s)",
            source_agent,
            shared_id,
            risk_score,
            injection_scan["matches"],
        )
        return {
            "_rejected": True,
            "enabled": True,
            "rejected": True,
            "reason": "high_risk_prompt_injection",
            "risk_score": risk_score,
            "matches": injection_scan["matches"],
        }
    return {
        "_rejected": False,
        "is_suspicious": is_suspicious,
        "risk_score": risk_score,
        "matches": injection_scan["matches"],
    }


def _build_untrusted_meta(meta_json, source_agent, shared_id, scan_result) -> dict:
    """Build the per-note metadata dict, parsing meta_json (if any)
    and stamping the untrusted_* fields the B7 quarantine contract
    requires callers to be able to inspect.

    Extracted 2026-06-22 from import_shared_memory().
    """
    meta: dict = {}
    try:
        if meta_json:
            meta = (
                json.loads(meta_json) if isinstance(meta_json, str) else dict(meta_json)
            )
    except Exception:
        logger.warning("Failed to parse metadata for shared memory %s", shared_id)
    meta["untrusted"] = scan_result["is_suspicious"]
    meta["untrusted_risk"] = scan_result["risk_score"]
    meta["untrusted_source_agent"] = source_agent
    meta["untrusted_shared_id"] = shared_id
    meta["untrusted_matches"] = scan_result["matches"][:5]
    return meta


def _add_provenance_tags(tags_json: str, is_suspicious: bool) -> list:
    """Parse the source tags_json and add "imported" + (when
    suspicious) "untrusted" provenance tags. Returns the new tags
    list. Extracted 2026-06-22.
    """
    tags_list = json.loads(tags_json) if tags_json else []
    if "imported" not in tags_list:
        tags_list.append("imported")
    if is_suspicious and "untrusted" not in tags_list:
        tags_list.append("untrusted")
    return tags_list


def _write_imported_note_crdt(
    conn: Any,
    new_id: str,
    content: str,
    source_agent: str,
    target_agent_id: str,
    source_file: str,
    category: str,
    tags_list: list,
) -> None:
    """v13 (2026-06-20): write field-level CRDT state for the imported
    note so concurrent edits from the source agent and the importer
    can both win on a per-field basis. Non-fatal: a pre-v13 DB without
    the field table falls through to the legacy note-level path.

    Extracted 2026-06-22.
    """
    from crdt.crdt_field import crdt_field_save, project_crdt_to_sql

    try:
        crdt_field_save(
            db_path=conn,
            note_id=new_id,
            content=content or "",
            remote_agent_id=source_agent or "shared",
            local_agent_id=target_agent_id,
            source_file=source_file,
            category=category or "imported",
            remote_vv_str=json.dumps({source_agent or "shared": 1}),
            remote_logical_clock=1,
            tags=json.dumps(tags_list),
        )
        project_crdt_to_sql(conn, new_id)
    except Exception as crdt_err:
        # Non-fatal: if the field table doesn't exist (pre-v13 DB)
        # the legacy note-level path still works.
        logger.debug(
            "import_shared_memory: field-level CRDT write skipped for %s: %s",
            new_id,
            crdt_err,
        )


def _run_import_indexers(
    conn,
    new_id: str,
    content: str,
    category: str,
    tags_list: list,
    source_file: str,
    is_suspicious: bool,
) -> None:
    """Run the save-pipeline indexers for the imported note.

    B7: skip the *graph-spreading* indexers (KG, facts, semantic
    backlinks) for untrusted content so the potentially-hostile
    content doesn't propagate across the graph. Chunks, embedding,
    and FTS-backlinks are kept so the note is findable but isolated.

    Extracted 2026-06-22.
    """
    from save_pipeline import (
        _index_chunks,
        _index_embedding,
        _index_kg,
        _index_facts,
        _auto_semantic_backlinks,
        _auto_fts_backlinks,
        _index_adaptive_retention,
    )

    _index_chunks(conn, new_id, content)
    _index_embedding(
        conn, new_id, content, category or "imported", tags_list, source_file
    )
    if not is_suspicious:
        # Graph-spreading indexers only for trusted imports.
        _index_kg(conn, new_id, content)
        _index_facts(conn, new_id, content)
        _auto_semantic_backlinks(conn, new_id, content)
    _auto_fts_backlinks(conn, new_id, content)
    _index_adaptive_retention(conn, new_id)


def import_shared_memory(
    shared_id: str, target_agent_id: str, db_path: str | None = None
) -> dict:
    """Import a shared memory into the target agent's workspace.

    Creates a new note in the agent's memories table from the shared pool.

    Args:
        shared_id: ID of the shared memory to import
        target_agent_id: identifier of the importing agent
        db_path: optional path to memory.db

    Returns:
        dict with status and new_note_id

    Decomposed 2026-06-22: 6 named helpers handle the path resolution,
    prompt-injection scan, metadata stamping, tag enrichment, CRDT
    field save, and indexer fan-out. The orchestrator below reads as
    a 7-step pipeline.
    """
    import sys

    if not sys.modules[__name__].MULTI_AGENT_ENABLED:
        return {"enabled": False}

    db_or_error = _resolve_import_db_path(db_path)
    if isinstance(db_or_error, dict):
        return db_or_error
    db = db_or_error

    try:
        conn = sqlite_write_queue.start_session(Path(db))
        try:
            _ensure_shared_table(conn)

            row = conn.execute(
                f"SELECT agent_id, content, category, tags, source_note_id, metadata "
                f"FROM {_SHARED_TABLE} WHERE id = ?",
                (shared_id,),
            ).fetchone()
            if not row:
                return {
                    "enabled": True,
                    "error": f"shared memory {shared_id} not found",
                }

            source_agent, content, category, tags_json, source_id, meta_json = row

            new_id = f"imported:{target_agent_id}:{source_id or shared_id}"
            from datetime import datetime, timezone

            datetime.now(timezone.utc).isoformat()
            source_file = f"imported/{new_id.replace(':', '_')}.md"

            scan_result = _scan_shared_content(content, source_agent, shared_id)
            if scan_result.get("_rejected"):
                # Strip the internal sentinel before returning.
                return {k: v for k, v in scan_result.items() if not k.startswith("_")}

            is_suspicious = scan_result["is_suspicious"]
            tier = "untrusted" if is_suspicious else "warm"
            meta = _build_untrusted_meta(
                meta_json, source_agent, shared_id, scan_result
            )
            tags_list = _add_provenance_tags(tags_json, is_suspicious)

            from save_pipeline import upsert_row

            upsert_row(
                conn,
                new_id,
                content,
                source_file=source_file,
                tags=tags_list,
                category=category or "imported",
                pinned=False,
                tier=tier,
                metadata=meta,
            )

            _write_imported_note_crdt(
                conn,
                new_id,
                content,
                source_agent,
                target_agent_id,
                source_file,
                category,
                tags_list,
            )
            _run_import_indexers(
                conn,
                new_id,
                content,
                category,
                tags_list,
                source_file,
                is_suspicious,
            )

            conn.commit()
            return {
                "enabled": True,
                "new_note_id": new_id,
                "source_agent": source_agent,
            }
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
    except Exception as e:
        return {"enabled": True, "error": str(e)}


def shared_pool_stats(db_path: str | None = None) -> dict:
    """Return shared pool statistics."""
    import sys

    if not sys.modules[__name__].MULTI_AGENT_ENABLED:
        return {"enabled": False}

    if db_path is not None:
        db = db_path
    else:
        try:
            from infra._lazy_imports import get_memory_paths

            _, local_mem, _ = get_memory_paths()
            db = str(local_mem / "memory.db")
        except ImportError:
            return {"enabled": True, "error": "memory_common not found"}

    try:
        conn = sqlite_write_queue.start_session(Path(db))
        try:
            _ensure_shared_table(conn)
            _purge_expired_shared(conn)
            conn.commit()

            total_row = conn.execute(f"SELECT COUNT(*) FROM {_SHARED_TABLE}").fetchone()
            total = int(total_row[0]) if total_row is not None else 0
            agents_row = conn.execute(
                f"SELECT COUNT(DISTINCT agent_id) FROM {_SHARED_TABLE}"
            ).fetchone()
            agents = int(agents_row[0]) if agents_row is not None else 0
            categories_row = conn.execute(
                f"SELECT COUNT(DISTINCT category) FROM {_SHARED_TABLE} WHERE category IS NOT NULL"
            ).fetchone()
            categories = int(categories_row[0]) if categories_row is not None else 0
        finally:
            conn.close()
        return {
            "enabled": True,
            "total_shared": total,
            "contributing_agents": agents,
            "categories": categories,
            "max_pool_size": sys.modules[__name__]._MAX_SHARED_POOL_SIZE,
        }
    except Exception:
        logger.warning("Failed to retrieve sharing stats")
        return {"enabled": True, "error": "stats unavailable"}


def list_share_candidates(
    min_importance: int = _AUTO_SHARE_MIN_IMPORTANCE,
    min_fitness: float = _AUTO_SHARE_MIN_FITNESS,
    limit: int = _AUTO_SHARE_MAX_PER_CYCLE,
    db_path: str | None = None,
) -> list[dict] | dict:
    """Return memory notes that look share-worthy and are not already in the pool.

    A note is a *candidate* when:

    * It is not soft-deleted (``deleted_at IS NULL``).
    * ``importance >= min_importance`` (default 4 — "high value").
    * ``fitness_score >= min_fitness`` (default 0.6).
    * It is not already in the shared pool for any agent.

    The list is ordered by ``(importance DESC, fitness_score DESC)`` so the
    auto-share cron picks the most valuable notes first within the
    per-cycle cap.

    Args:
        min_importance: minimum importance column value (1-5)
        min_fitness:   minimum fitness_score column value (0.0-1.0)
        limit:         max candidates to return
        db_path:       optional path to memory.db (defaults to active DB)

    Returns:
        list of candidate dicts with id/content/category/tags/importance/fitness.
        On error returns ``{"error": ...}``; on disabled returns ``[]``.
    """
    import sys

    if not sys.modules[__name__].MULTI_AGENT_ENABLED:
        return []

    if db_path is not None:
        db = db_path
    else:
        try:
            from infra._lazy_imports import get_memory_paths

            _, local_mem, _ = get_memory_paths()
            db = str(local_mem / "memory.db")
        except ImportError:
            return {"error": "memory_common not found"}

    try:
        from infra.db import open_db
        with open_db(Path(db), pooled=True, write=True) as conn:
            _ensure_shared_table(conn)
            rows = conn.execute(
                """
                SELECT m.id, m.content, m.category, m.tags,
                       COALESCE(m.importance, 3), COALESCE(m.fitness_score, 1.0)
                FROM memories m
                LEFT JOIN shared_memories s ON s.source_note_id = m.id
                WHERE m.deleted_at IS NULL
                  AND COALESCE(m.importance, 3) >= ?
                  AND COALESCE(m.fitness_score, 1.0) >= ?
                  AND s.id IS NULL
                ORDER BY COALESCE(m.importance, 3) DESC,
                         COALESCE(m.fitness_score, 1.0) DESC
                LIMIT ?
                """,
                (min_importance, min_fitness, limit),
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "content": (r[1] or "")[:200],
                    "category": r[2],
                    "tags": r[3],
                    "importance": r[4],
                    "fitness_score": float(r[5]),
                }
                for r in rows
            ]
    except Exception as e:
        logger.warning("list_share_candidates failed: %s", e)
        return {"error": str(e)}


def auto_share_high_value(
    agent_id: str | None = None,
    min_importance: int = _AUTO_SHARE_MIN_IMPORTANCE,
    min_fitness: float = _AUTO_SHARE_MIN_FITNESS,
    limit: int = _AUTO_SHARE_MAX_PER_CYCLE,
    db_path: str | None = None,
) -> dict:
    """Scan high-importance notes and share them into the shared pool.

    The "share-worthy" threshold is intentionally *strict* so the cron
    doesn't flood the pool with low-signal content. The defaults
    (importance>=4, fitness>=0.6) are the P2 #1 wiring choice —
    auto-share should be opt-in by virtue of being gated on
    multi_agent + importance/fitness thresholds, not on the entire
    corpus.

    Args:
        agent_id:        identifier of the agent performing the share;
                         defaults to the local CRDT agent id.
        min_importance:  minimum importance for share-worthiness
        min_fitness:     minimum fitness_score for share-worthiness
        limit:           hard cap on how many notes to share in one call
        db_path:         optional path to memory.db

    Returns:
        dict with:
          - ``enabled``     : True if multi_agent is on
          - ``scanned``     : number of candidates found
          - ``shared``      : number of notes successfully shared
          - ``skipped``     : number of candidates that were already in
                              the pool or that failed to share
          - ``candidates``  : the candidate list (truncated to limit)
          - ``shared_ids``  : ids of newly-shared notes
          - ``error``       : present iff multi_agent is off or fatal
    """
    import sys

    if not sys.modules[__name__].MULTI_AGENT_ENABLED:
        return {"enabled": False}

    if agent_id is None:
        try:
            from save.crdt_helpers import _crdt_agent_id

            agent_id = _crdt_agent_id()
        except Exception:
            agent_id = "auto-share"

    candidates = list_share_candidates(
        min_importance=min_importance,
        min_fitness=min_fitness,
        limit=limit,
        db_path=db_path,
    )
    if isinstance(candidates, dict) and "error" in candidates:
        return {
            "enabled": True,
            "scanned": 0,
            "shared": 0,
            "skipped": 0,
            "candidates": [],
            "shared_ids": [],
            "error": candidates["error"],
        }

    scanned = len(candidates)
    shared_ids: list[str] = []
    skipped = 0
    for cand in candidates:
        result = share_memory(cand["id"], agent_id, db_path=db_path)
        if isinstance(result, dict) and "shared_id" in result:
            shared_ids.append(result["shared_id"])
        else:
            skipped += 1

    return {
        "enabled": True,
        "scanned": scanned,
        "shared": len(shared_ids),
        "skipped": skipped,
        "candidates": candidates,
        "shared_ids": shared_ids,
        "agent_id": agent_id,
    }


from infra.memory_common import make_lazy_getattr

__getattr__ = make_lazy_getattr(
    {
        "MULTI_AGENT_ENABLED": "multi_agent",
        "_SHARED_POOL_TTL_DAYS": "shared_pool_ttl_days",
        "_MAX_SHARED_POOL_SIZE": "shared_pool_max_size",
    }
)
