from __future__ import annotations
"""
CRDT sync subsystem MCP tools — crdt_sync, crdt_status.

Extracted from mcp_maintenance.py to reduce module size.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401

import json
import sqlite3

from mcp_common import (
    _resolve_memory_dir,
    _err,
    ErrorCode,
    logger,
    with_audit,
)
from mcp_instance import mcp


@mcp.tool()
@with_audit("memory_crdt_sync")
def memory_crdt_sync(agent_id: str, remote_notes_json: str) -> str:
    """Bulk-sync notes from a remote agent using CRDT conflict resolution.

    Args:
        agent_id: Identifier for the sending agent.
        remote_notes_json: JSON dict mapping note_id to a 5-element list:
            [content, source_file, logical_clock, version_vector_str, sender_clock].

    Returns JSON with applied/conflicted/rejected/total counts.
    """
    from crdt.crdt_merge import crdt_sync_all
    from save.crdt_helpers import _crdt_agent_id

    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, "memory.db not found")

    try:
        remote_notes_raw = json.loads(remote_notes_json)
    except json.JSONDecodeError:
        logger.exception("invalid JSON")
        return _err(ErrorCode.INVALID_PARAMS, "invalid JSON")

    notes: dict[str, tuple[str, str, int, str, int]] = {}
    for note_id, data in remote_notes_raw.items():
        if isinstance(data, list) and len(data) >= 5:
            notes[note_id] = (
                data[0],
                data[1],
                int(data[2]),
                str(data[3]),
                int(data[4]),
            )
        else:
            return _err(
                ErrorCode.INVALID_PARAMS,
                f"invalid data for {note_id}: expected 5-element list, got {type(data).__name__}",
            )

    try:
        result = crdt_sync_all(str(db_path), agent_id, _crdt_agent_id(), notes)
        return json.dumps(result)
    except Exception:
        logger.exception("crdt_sync failed")
        return _err(ErrorCode.DB_ERROR, "crdt_sync failed")


@mcp.tool()
@with_audit("memory_crdt_status")
def memory_crdt_status() -> str:
    """Return peer sync status: last sync time, error count, and pending
    changes for each configured peer.

    Reads peer config and sync_log from the active DB.
    """
    from infra._lazy_imports import get_config

    cfg = get_config()
    peers = cfg.sync_peers
    if not peers:
        return json.dumps({"peers": [], "sync_enabled": cfg.sync_enable_server})

    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, "memory.db not found")

    from infra.db import open_db
    status_list = []
    with open_db(db_path, timeout=5.0, pooled=True, write=False) as conn:
        for p in peers:
            entry = {
                "name": p.get("name", p.get("agent_id", "?")),
                "url": p.get("url", ""),
                "agent_id": p.get("agent_id", ""),
            }
            try:
                row = conn.execute(
                    """SELECT MAX(completed_at), SUM(CASE WHEN success=1 THEN 1 ELSE 0 END),
                              SUM(error_count), COUNT(*)
                       FROM sync_log WHERE peer_name=?""",
                    (entry["name"],),
                ).fetchone()
                if row and row[0]:
                    entry["last_sync_at"] = row[0]
                    entry["success_count"] = row[1] or 0
                    entry["total_errors"] = row[2] or 0
                    entry["total_cycles"] = row[3] or 0

                    last_sync_val = entry.get("last_sync_at")
                    if last_sync_val is not None:
                        pending = conn.execute(
                            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND strftime('%s', updated_at) > ?",
                            (int(last_sync_val),),
                        ).fetchone()
                        entry["pending_changes"] = pending[0] if pending else 0
                else:
                    entry["last_sync_at"] = None
                    entry["total_cycles"] = 0
            except sqlite3.OperationalError:
                entry["status"] = "table_missing"
            except Exception as e:
                entry["error"] = str(e)[:200]

            status_list.append(entry)

    return json.dumps(
        {"peers": status_list, "sync_enabled": cfg.sync_enable_server}, indent=2
    )
