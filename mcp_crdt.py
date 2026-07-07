from __future__ import annotations
"""
CRDT sync subsystem MCP tools — crdt_sync, crdt_status.

Extracted from mcp_maintenance.py to reduce module size.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401

import json
import os
import sqlite3

from mcp_common import (
    _resolve_memory_dir,
    _err,
    ErrorCode,
    logger,
    with_audit,
)
from mcp_instance import mcp


def _crdt_sync_authorized(agent_id: str, sync_token: str) -> bool:
    """Return True if a crdt_sync request is authorized.

    S3 — peer authentication before any remote JSON is merged. Authorized
    when EITHER:
      * ``sync_token`` equals ``MEMORY_SYNC_TOKEN`` (the same shared secret
        the sync server uses, see ``infra/sync_server.py``); OR
      * ``agent_id`` is listed in the ``MEMORY_CRDT_TRUSTED_PEERS`` allowlist
        (comma-separated agent ids / names).

    Requests with neither credential are rejected and the remote JSON is
    never parsed or merged.
    """
    expected = os.environ.get("MEMORY_SYNC_TOKEN", "")
    if expected and sync_token and sync_token == expected:
        return True
    trusted = os.environ.get("MEMORY_CRDT_TRUSTED_PEERS", "")
    if trusted:
        allowed = {p.strip() for p in trusted.split(",") if p.strip()}
        if agent_id and agent_id in allowed:
            return True
    return False


@mcp.tool()
@with_audit("memory_crdt_sync")
def memory_crdt_sync(
    agent_id: str,
    remote_notes_json: str,
    sync_token: str = "",
) -> str:
    """Bulk-sync notes from a remote agent using CRDT conflict resolution.

    Args:
        agent_id: Identifier for the sending agent.
        remote_notes_json: JSON dict mapping note_id to a 5-element list:
            [content, source_file, logical_clock, version_vector_str, sender_clock].
        sync_token: Shared secret matching ``MEMORY_SYNC_TOKEN`` (the same
            token the sync server authenticates with). Required unless
            ``agent_id`` is in the ``MEMORY_CRDT_TRUSTED_PEERS`` allowlist.
            Requests without a valid credential are rejected and the remote
            JSON is never merged.

    Returns JSON with applied/conflicted/rejected/total counts.
    """
    from crdt.crdt_merge import crdt_sync_all
    from save.crdt_helpers import _crdt_agent_id

    # S3: authenticate the peer BEFORE touching the remote payload.
    if not _crdt_sync_authorized(agent_id, sync_token):
        return _err(
            ErrorCode.INVALID_PARAMS,
            "crdt_sync rejected: missing or invalid sync_token (MEMORY_SYNC_TOKEN) "
            "and agent_id not in MEMORY_CRDT_TRUSTED_PEERS allowlist.",
        )

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

    status_list = []
    for p in peers:
        entry = {
            "name": p.get("name", p.get("agent_id", "?")),
            "url": p.get("url", ""),
            "agent_id": p.get("agent_id", ""),
        }
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.execute("PRAGMA foreign_keys=ON")
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
                else:
                    entry["last_sync_at"] = None
                    entry["total_cycles"] = 0

                last_sync_val = entry.get("last_sync_at")
                if last_sync_val is not None:
                    pending = conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND strftime('%s', updated_at) > ?",
                        (int(last_sync_val),),
                    ).fetchone()
                    entry["pending_changes"] = pending[0] if pending else 0
            finally:
                conn.close()
        except Exception as e:
            entry["error"] = str(e)[:200]

        status_list.append(entry)

    return json.dumps(
        {"peers": status_list, "sync_enabled": cfg.sync_enable_server}, indent=2
    )
