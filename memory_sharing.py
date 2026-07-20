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

import logging

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    MULTI_AGENT_ENABLED: bool

if TYPE_CHECKING:
    from infra.db import AnyConnection
import time
from pathlib import Path

logger = logging.getLogger(__name__)
from typing import Any

from config import resolve_db_path

__all__ = [  # pyright: ignore[reportUnsupportedDunderAll]
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


def _purge_expired_shared(conn: AnyConnection, tenant_id: str = "default") -> None:
    """Delete TTL-expired entries from the shared pool, scoped to a tenant."""
    import sys

    this_mod = sys.modules[__name__]
    if this_mod._SHARED_POOL_TTL_DAYS > 0:
        cutoff = time.time() - (this_mod._SHARED_POOL_TTL_DAYS * 86400)
        conn.execute(
            f"DELETE FROM {_SHARED_TABLE} WHERE shared_at < ? AND tenant_id = ?",
            (cutoff, tenant_id),
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
            metadata TEXT,
            target_agent_id TEXT DEFAULT NULL,
            shared_with TEXT DEFAULT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default'
        )
    """)
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_SHARED_TABLE})").fetchall()}
        if "tenant_id" not in cols:
            conn.execute(f"ALTER TABLE {_SHARED_TABLE} ADD COLUMN tenant_id TEXT DEFAULT 'default'")
            conn.execute(f"UPDATE {_SHARED_TABLE} SET tenant_id = 'default' WHERE tenant_id IS NULL")
        if "target_agent_id" not in cols:
            conn.execute(f"ALTER TABLE {_SHARED_TABLE} ADD COLUMN target_agent_id TEXT DEFAULT NULL")
        if "shared_with" not in cols:
            conn.execute(f"ALTER TABLE {_SHARED_TABLE} ADD COLUMN shared_with TEXT DEFAULT NULL")
    except Exception as e:
        logger.warning("_ensure_shared_table failed: %s", e)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_shared_agent ON {_SHARED_TABLE}(agent_id)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_shared_category ON {_SHARED_TABLE}(category)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_shared_target_agent ON {_SHARED_TABLE}(target_agent_id)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_shared_shared_with ON {_SHARED_TABLE}(shared_with)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_shared_tenant_id ON {_SHARED_TABLE}(tenant_id)"
    )


def _sidecar_path(local_mem: Path, note_id: str) -> Path:
    """Return the sidecar file path for a note's shared-metadata.

    The sidecar lives in the same category directory as the source
    markdown note so it is picked up by normal backup / sync tooling.
    Falls back to `memory/shared/` if the category directory doesn't
    exist (e.g., for synthetic notes with no backing file).
    """
    category = note_id.split("/")[0] if "/" in note_id else ""
    if category:
        slug = note_id.split("/", 1)[1]
        candidate = local_mem / category / f"{slug}.shared.json"
    else:
        candidate = local_mem / f"{note_id}.shared.json"
    if candidate.parent.exists() or category:
        return candidate
    return local_mem / "shared" / f"{note_id}.shared.json"


def _write_shared_sidecar(local_mem: Path, note_id: str, entry: dict) -> None:
    """Append a single share entry to the note's sidecar file.

    The sidecar is a JSON array of share records.  It is written
    atomically to avoid partial writes on crash.
    """
    path = _sidecar_path(local_mem, note_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict] = []
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = [existing]
            except (json.JSONDecodeError, OSError):
                existing = []
        existing.append(entry)
        tmp = path.with_suffix(".shared.json.tmp")
        tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.debug("_write_shared_sidecar failed for %s: %s", note_id, e)


def _load_shared_sidecars(local_mem: Path) -> list[dict]:
    """Load all shared-memory sidecar entries from the memory directory.

    Scans recursively for `*.shared.json` files and flattens them
    into a single list of share records suitable for INSERT into
    ``shared_memories``.
    """
    entries: list[dict] = []
    if not local_mem.exists():
        return entries
    for path in local_mem.rglob("*.shared.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                entries.extend(data)
            elif isinstance(data, dict):
                entries.append(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("_load_shared_sidecars: skipping %s: %s", path, e)
    return entries


def share_memory(
    note_id: str,
    agent_id: str,
    target_agent_id: str | None = None,
    shared_with: str | None = None,
    db_path: str | None = None,
    tenant_id: str = "default",
) -> dict:
    """Share a memory from one agent's workspace to the shared pool.

    Args:
        note_id: the note to share
        agent_id: identifier of the agent sharing this memory (sharer)
        target_agent_id: if set, store the target agent id (directed share).
        shared_with: human-readable target label; mirrors target_agent_id.
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
        from infra.db_path_flock import db_path_flock
        import sqlite3

        # Direct per-task connection under the per-DB-path cross-process
        # flock.  Avoids the singleton sqlite_write_queue writer thread,
        # which can block/hang a share behind an in-flight save session.
        with db_path_flock(Path(db)):
            conn = sqlite3.connect(str(db), timeout=30.0)
            try:
                _ensure_shared_table(conn)

                # B12 fix: BEGIN IMMEDIATE before the SELECT so the existence
                # check and the subsequent write are in the same write
                # transaction.  Otherwise a concurrent unshare could remove
                # the row between our SELECT and INSERT.
                conn.execute("BEGIN IMMEDIATE")
                # Read the source note (include tenant_id for pool scoping).
                row = conn.execute(
                    "SELECT content, category, tags, metadata, tenant_id FROM memories "
                    "WHERE id = ? AND deleted_at IS NULL",
                    (note_id,),
                ).fetchone()
                if not row:
                    conn.rollback()
                    return {"enabled": True, "error": f"note {note_id} not found"}

                content, category, tags_json, meta_json, note_tenant_id = row

                _tid = note_tenant_id or "default"
                _purge_expired_shared(conn, tenant_id=_tid)
                try:
                    count_row = conn.execute(
                        f"SELECT COUNT(*) FROM {_SHARED_TABLE} WHERE tenant_id = ?",
                        (_tid,),
                    ).fetchone()
                    count = int(count_row[0]) if count_row is not None else 0
                    this_mod = sys.modules[__name__]
                    if count >= this_mod._MAX_SHARED_POOL_SIZE:
                        to_evict = count - this_mod._MAX_SHARED_POOL_SIZE + 1
                        conn.execute(
                            f"DELETE FROM {_SHARED_TABLE} WHERE id IN "
                            f"(SELECT s.id FROM {_SHARED_TABLE} s "
                            f"LEFT JOIN memories m ON s.source_note_id = m.id "
                            f"WHERE s.tenant_id = ? "
                            f"ORDER BY COALESCE(m.importance, 0) ASC, s.shared_at ASC "
                            f"LIMIT ?)",
                            (_tid, to_evict,)
                        )

                    shared_id = f"shared:{agent_id}:{note_id}"
                    _tid = note_tenant_id or "default"
                    conn.execute(
                        f"INSERT INTO {_SHARED_TABLE} "
                        f"(id, agent_id, content, category, tags, shared_at, source_note_id, metadata, "
                        f"target_agent_id, shared_with, tenant_id) "
                        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        f"ON CONFLICT(id) DO UPDATE SET "
                        f"content=excluded.content, category=excluded.category, "
                        f"tags=excluded.tags, shared_at=excluded.shared_at, "
                        f"source_note_id=excluded.source_note_id, metadata=excluded.metadata, "
                        f"target_agent_id=excluded.target_agent_id, shared_with=excluded.shared_with, "
                        f"tenant_id=excluded.tenant_id",
                        (
                            shared_id,
                            agent_id,
                            content,
                            category,
                            tags_json,
                            time.time(),
                            note_id,
                            meta_json,
                            target_agent_id,
                            shared_with or target_agent_id,
                            _tid,
                        ),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning("share_memory failed: %s", e)
                    conn.rollback()
                    raise
                sidecar_entry = {
                    "shared_id": shared_id,
                    "agent_id": agent_id,
                    "content": content,
                    "category": category,
                    "tags": tags_json,
                    "shared_at": time.time(),
                    "source_note_id": note_id,
                    "metadata": meta_json,
                    "target_agent_id": target_agent_id,
                    "shared_with": shared_with or target_agent_id,
                    "tenant_id": _tid,
                }
                try:
                    _write_shared_sidecar(local_mem, note_id, sidecar_entry)
                except Exception as sidecar_exc:
                    logger.debug("shared sidecar write failed for %s: %s", note_id, sidecar_exc)
                return {"enabled": True, "shared_id": shared_id, "agent_id": agent_id}
            finally:
                conn.close()
    except Exception as e:
        logger.warning("share_memory failed: %s", e)
        return {"enabled": True, "error": str(e)}


def list_shared_memories(
    agent_id: str | None = None,
    category: str | None = None,
    limit: int = 50,
    shared_with_me: bool = False,
    db_path: str | None = None,
    tenant_id: str = "default",
) -> list[dict] | dict:
    """List memories in the shared pool, scoped to a tenant.

    Args:
        agent_id: filter by sharing agent
        category: filter by category
        limit: max results
        shared_with_me: if True, restrict to rows where target_agent_id or
            shared_with matches the current agent (directed shares only).
        db_path: optional path to memory.db
        tenant_id: tenant scope (default "default")

    Returns:
        list of shared memory dicts
    """
    import sys

    if not sys.modules[__name__].MULTI_AGENT_ENABLED:
        return []

    current_agent: str | None = None
    if shared_with_me:
        try:
            from agent_context import get_agent as _gwa
            current_agent = _gwa().agent_id
        except (ImportError, Exception) as e:
            logger.warning("list_shared_memories failed: %s", e)

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
        # Read path (write=False) avoids the singleton sqlite_write_queue
        # writer thread, which can stall behind an in-flight save session.
        with open_db(Path(db), pooled=True, write=False) as conn:
            _ensure_shared_table(conn)

            query = (
                f"SELECT id, agent_id, content, category, tags, shared_at, "
                f"source_note_id, target_agent_id, shared_with "
                f"FROM {_SHARED_TABLE}"
            )
            params: list = []
            conditions = ["tenant_id = ?"]
            params.append(tenant_id)
            if agent_id:
                conditions.append("agent_id = ?")
                params.append(agent_id)
            if category:
                conditions.append("category = ?")
                params.append(category)
            if shared_with_me and current_agent:
                conditions.append(
                    "(target_agent_id = ? OR shared_with = ?)"
                )
                params.extend([current_agent, current_agent])
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
                    "target_agent_id": r[7],
                    "shared_with": r[8],
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
    shared_id: str, target_agent_id: str, db_path: str | None = None, tenant_id: str = "default"
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
        from infra.db_path_flock import db_path_flock
        import sqlite3

        # Direct connection under per-DB-path flock (avoids the singleton
        # write-queue writer thread, which can stall behind a save session).
        with db_path_flock(Path(db)):
            conn = sqlite3.connect(str(db), timeout=30.0)
            try:
                _ensure_shared_table(conn)

                row = conn.execute(
                    f"SELECT agent_id, content, category, tags, source_note_id, metadata, tenant_id "
                    f"FROM {_SHARED_TABLE} WHERE id = ? AND tenant_id = ?",
                    (shared_id, tenant_id),
                ).fetchone()
                if not row:
                    return {
                        "enabled": True,
                        "error": f"shared memory {shared_id} not found",
                    }

                source_agent, content, category, tags_json, source_id, meta_json, _sm_tenant_id = row

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

                # Run indexers before the CRDT write. crdt_field_save commits
                # the session connection internally (project_crdt_to_sql),
                # so writing CRDT first would prematurely commit the uncommitted
                # memories row and break rollback on indexer failure.
                _run_import_indexers(
                    conn,
                    new_id,
                    content,
                    category,
                    tags_list,
                    source_file,
                    is_suspicious,
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

                conn.commit()
                return {
                    "enabled": True,
                    "new_note_id": new_id,
                    "source_agent": source_agent,
                }
            except Exception as e:
                logger.warning("import_shared_memory failed: %s", e)
                try:
                    conn.rollback()
                except Exception as e:
                    logger.warning("import_shared_memory failed: %s", e)
                raise
            finally:
                conn.close()
    except Exception as e:
        logger.warning("import_shared_memory failed: %s", e)
        return {"enabled": True, "error": str(e)}


def shared_pool_stats(db_path: str | None = None, tenant_id: str = "default") -> dict:
    """Return shared pool statistics, scoped to a tenant."""
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
        from infra.db_path_flock import db_path_flock
        import sqlite3

        # Direct connection under per-DB-path flock (avoids the singleton
        # write-queue writer thread, which can stall behind a save session).
        with db_path_flock(Path(db)):
            conn = sqlite3.connect(str(db), timeout=30.0)
            try:
                _ensure_shared_table(conn)
                _purge_expired_shared(conn)
                conn.commit()

                total_row = conn.execute(
                    f"SELECT COUNT(*) FROM {_SHARED_TABLE} WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()
                total = int(total_row[0]) if total_row is not None else 0
                agents_row = conn.execute(
                    f"SELECT COUNT(DISTINCT agent_id) FROM {_SHARED_TABLE} WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()
                agents = int(agents_row[0]) if agents_row is not None else 0
                categories_row = conn.execute(
                    f"SELECT COUNT(DISTINCT category) FROM {_SHARED_TABLE} "
                    f"WHERE category IS NOT NULL AND tenant_id = ?",
                    (tenant_id,),
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
    tenant_id: str = "default",
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
        # Read path (write=False) avoids the singleton sqlite_write_queue
        # writer thread, which can stall behind an in-flight save session.
        with open_db(Path(db), pooled=True, write=False) as conn:
            _ensure_shared_table(conn)
            _purge_expired_shared(conn)
            rows = conn.execute(
                """
                SELECT m.id, m.content, m.category, m.tags,
                       COALESCE(m.importance, 3), COALESCE(m.fitness_score, 1.0)
                FROM memories m
                LEFT JOIN shared_memories s ON s.source_note_id = m.id
                    AND s.tenant_id = ?
                WHERE m.deleted_at IS NULL
                  AND m.tenant_id = ?
                  AND (m.category IS NULL OR m.category NOT IN ('sessions', 'tests'))
                  AND COALESCE(m.importance, 3) >= ?
                  AND COALESCE(m.fitness_score, 1.0) >= ?
                  AND s.id IS NULL
                ORDER BY COALESCE(m.importance, 3) DESC,
                         COALESCE(m.fitness_score, 1.0) DESC
                LIMIT ?
                """,
                (tenant_id, tenant_id, min_importance, min_fitness, limit),
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
    tenant_id: str = "default",
) -> dict:
    """Scan high-importance notes and share them into the shared pool.

    The "share-worthy" threshold is intentionally *strict* so the cron
    doesn't flood the pool with low-signal content. Sessions and tests
    are excluded entirely. Defaults (importance>=4, fitness>=0.6) gate
    auto-share to high-signal content only.

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
        except Exception as e:
            logger.warning("auto_share_high_value failed: %s", e)
            agent_id = "auto-share"

    candidates = list_share_candidates(
        min_importance=min_importance,
        min_fitness=min_fitness,
        limit=limit,
        db_path=db_path,
        tenant_id=tenant_id,
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
        result = share_memory(cand["id"], agent_id, db_path=db_path, tenant_id=tenant_id)
        if isinstance(result, dict) and "shared_id" in result:
            shared_ids.append(result["shared_id"])
        else:
            skipped += 1

    if shared_ids:
        try:
            _create_notification_tasks_for_peers(
                shared_ids, agent_id or "auto-share", db_path=db_path
            )
        except Exception:
            logger.debug("Failed to create peer notification tasks", exc_info=True)

    return {
        "enabled": True,
        "scanned": scanned,
        "shared": len(shared_ids),
        "skipped": skipped,
        "candidates": candidates,
        "shared_ids": shared_ids,
        "agent_id": agent_id,
    }


def _create_notification_tasks_for_peers(
    shared_ids: list[str],
    source_agent_id: str,
    db_path: str | None = None,
) -> int:
    """After auto-share, write coordination tasks into peer agents' DBs.

    Each peer's ``shared_tasks`` table gets one pending task per shared
    memory so that ``claim_pending_tasks`` on their next session start
    picks it up and tells them to import it.

    All DBs are local files; peer DB paths are derived from agent IDs
    (``memory.db`` vs ``memory-{agent_id.lower()}.db``). Silently skips
    peers whose DB doesn't exist or can't be opened.

    Returns the number of tasks created.
    """
    import sqlite3

    from agent_context import list_agents as _list_agents
    from infra.infrastructure import resolve_active_memory_dir

    agents = _list_agents()
    if len(agents) <= 1:
        return 0

    mem_dir = Path(db_path).parent if db_path else resolve_active_memory_dir()
    local_key = source_agent_id.upper()
    count = 0
    now = time.time()

    for peer_id in agents:
        if peer_id.upper() == local_key:
            continue

        peer_db = _resolve_peer_db_path(mem_dir, peer_id)
        if not peer_db or not peer_db.exists():
            continue

        try:
            pconn = sqlite3.connect(str(peer_db), timeout=5)
            try:
                pconn.execute("PRAGMA journal_mode=WAL")
                pconn.execute("""
                    CREATE TABLE IF NOT EXISTS shared_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id TEXT NOT NULL,
                        task_type TEXT NOT NULL,
                        description TEXT,
                        assigned_to TEXT,
                        status TEXT DEFAULT 'pending',
                        created_by TEXT NOT NULL,
                        created_at REAL,
                        updated_at REAL,
                        depends_on INTEGER REFERENCES shared_tasks(id)
                    )
                """)
                for sid in shared_ids:
                    desc = (
                        f"New shared memory: {sid} — "
                        f"review and import via memory_share(action='import', share_with='{source_agent_id}')"
                    )
                    pconn.execute(
                        "INSERT INTO shared_tasks "
                        "(project_id, task_type, description, assigned_to, status, created_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "shared",
                            "review_shared_memory",
                            desc,
                            peer_id,
                            "pending",
                            source_agent_id,
                            now,
                            now,
                        ),
                    )
                pconn.commit()
                count += len(shared_ids)
            finally:
                pconn.close()
        except Exception:
            logger.debug("Could not create notification tasks for peer %s at %s", peer_id, peer_db)
            continue

    if count:
        logger.info("Created %d coordination tasks across %d peers for %d shared memories", count, len(agents) - 1, len(shared_ids))
    return count


def _resolve_peer_db_path(mem_dir: Path, agent_id: str) -> Path | None:
    """Resolve a peer agent's DB file path from its agent ID."""
    aid = agent_id.upper().strip()
    if aid == "OPENCODE" or aid == "DEFAULT":
        return mem_dir / "memory.db"
    if aid == "MIMOCODE":
        return mem_dir / "memory-mimocode.db"
    candidate = mem_dir / f"memory-{agent_id.lower().replace(' ', '-')}.db"
    return candidate


from infra.memory_common import make_lazy_getattr

__getattr__ = make_lazy_getattr(
    {
        "MULTI_AGENT_ENABLED": "multi_agent",
        "_SHARED_POOL_TTL_DAYS": "shared_pool_ttl_days",
        "_MAX_SHARED_POOL_SIZE": "shared_pool_max_size",
    }
)
