"""SyncManager — CRDT sync and cross-agent memory sharing."""

from __future__ import annotations

import logging

import json
from pathlib import Path
from typing import Any

from agentic_memory.exceptions import SyncError
from agentic_memory.utils import resolve_db_path

logger = logging.getLogger(__name__)


class SyncManager:
    """Multi-agent sync and sharing manager.

    Wraps the CRDT sync subsystem and the cross-agent memory sharing
    pool into a single typed Python API.

    Examples::

        sm = SyncManager()
        result = sm.sync("agent-b", remote_notes)
        status = sm.status()
        sm.share("note-123", "agent-b")
    """

    def __init__(self, db_path: str | Path | None = None):
        self._db_path = resolve_db_path(db_path)

    # ── CRDT sync ──────────────────────────────────────────────────────

    def sync(
        self,
        agent_id: str,
        remote_notes: dict[str, list],
    ) -> dict[str, Any]:
        """Bulk-sync notes from a remote agent using CRDT resolution.

        Args:
            agent_id: Identifier for the sending agent.
            remote_notes: Dict mapping ``note_id`` to a 5-element list:
                ``[content, source_file, logical_clock, version_vector_str, sender_clock]``.

        Returns:
            Dict with keys: ``applied``, ``conflicted``, ``rejected``,
            ``total``.
        """
        from crdt.crdt_merge import crdt_sync_all
        from save.crdt_helpers import _crdt_agent_id

        notes: dict[str, tuple[str, str, int, str, int]] = {}
        for note_id, data in remote_notes.items():
            if isinstance(data, list) and len(data) >= 5:
                notes[note_id] = (
                    str(data[0]),
                    str(data[1]),
                    int(data[2]),
                    str(data[3]),
                    int(data[4]),
                )
            else:
                raise SyncError(
                    f"Invalid data for {note_id}: "
                    f"expected 5-element list, got {type(data).__name__}"
                )

        try:
            result = crdt_sync_all(
                str(self._db_path), agent_id, _crdt_agent_id(), notes
            )
            if isinstance(result, dict):
                return result
            if isinstance(result, str):
                return json.loads(result)
            return {"raw": str(result)}
        except Exception as exc:
            logger.exception("CRDT sync failed")
            raise SyncError(f"CRDT sync failed: {exc}") from exc

    def status(self) -> dict[str, Any]:
        """Return peer sync status.

        Queries the config for sync peers and reads the ``sync_log``
        table to build per-peer status with last sync time, success
        counts, error counts, and pending changes.

        Returns:
            Dict with ``peers`` (list of per-peer status dicts) and
            ``sync_enabled`` flag.
        """
        from infra._lazy_imports import get_config
        import sqlite3

        cfg = get_config()
        peers = cfg.sync_peers
        if not peers:
            return {"peers": [], "sync_enabled": cfg.sync_enable_server}

        from mcp_common import _resolve_memory_dir

        target_base = _resolve_memory_dir()
        db_path = target_base / "memory.db"
        if not db_path.exists():
            return {
                "peers": [],
                "sync_enabled": cfg.sync_enable_server,
                "error": "db not found",
            }

        status_list: list[dict[str, Any]] = []
        for p in peers:
            entry: dict[str, Any] = {
                "name": p.get("name", p.get("agent_id", "?")),
                "url": p.get("url", ""),
                "agent_id": p.get("agent_id", ""),
            }
            try:
                conn = sqlite3.connect(str(db_path), timeout=5)
                conn.execute("PRAGMA foreign_keys=ON")
                try:
                    row = conn.execute(
                        """SELECT MAX(completed_at),
                                  SUM(CASE WHEN success=1 THEN 1 ELSE 0 END),
                                  SUM(error_count),
                                  COUNT(*)
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

                    last_sync = entry.get("last_sync_at")
                    if last_sync is not None:
                        last_sync_ts = last_sync if isinstance(last_sync, (int, float)) else int(last_sync)
                        pending = conn.execute(
                            "SELECT COUNT(*) FROM memories "
                            "WHERE deleted_at IS NULL "
                            "AND strftime('%s', updated_at) > ?",
                            (last_sync_ts,),
                        ).fetchone()
                        entry["pending_changes"] = pending[0] if pending else 0
                finally:
                    conn.close()
            except Exception as exc:
                logger.warning("status failed: %s", exc)
                entry["error"] = str(exc)[:200]

            status_list.append(entry)

        return {"peers": status_list, "sync_enabled": cfg.sync_enable_server}

    # ── Sharing ────────────────────────────────────────────────────────

    def share(self, note_id: str, agent_id: str) -> bool:
        """Share a memory to the cross-agent shared pool.

        Args:
            note_id: ID of the note to share.
            agent_id: Target agent identifier.

        Returns:
            True if the share succeeded.
        """
        import memory_sharing as ma

        if not ma.MULTI_AGENT_ENABLED:
            logger.warning("Multi-agent sharing disabled; set MEMORY_MULTI_AGENT=1")
            return False

        try:
            result = ma.share_memory(note_id, agent_id)
            if isinstance(result, dict):
                return bool(result.get("ok", result.get("success", False)))
            if isinstance(result, str):
                parsed = json.loads(result)
                return bool(parsed.get("ok", parsed.get("success", False)))
            return bool(result)
        except Exception as exc:
            logger.exception("Share failed")
            raise SyncError(f"Share failed: {exc}") from exc

    def list_shared(
        self,
        agent_id: str = "",
        category: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List memories in the shared pool.

        Args:
            agent_id: Optional filter by agent.
            category: Optional filter by category.
            limit: Max results to return.

        Returns:
            List of shared memory dicts.
        """
        import memory_sharing as ma

        if not ma.MULTI_AGENT_ENABLED:
            return []

        try:
            results = ma.list_shared_memories(
                agent_id=agent_id or None,
                category=category or None,
                limit=limit,
            )
            if isinstance(results, list):
                return results
            if isinstance(results, str):
                return json.loads(results)
            return list(results) if results else []
        except Exception as exc:
            logger.exception("List shared failed")
            raise SyncError(f"List shared failed: {exc}") from exc

    def import_shared(self, shared_id: str, target_agent_id: str) -> bool:
        """Import a shared memory into the target agent's workspace.

        Args:
            shared_id: ID of the shared memory entry.
            target_agent_id: Agent to import into.

        Returns:
            True if the import succeeded.
        """
        import memory_sharing as ma

        if not ma.MULTI_AGENT_ENABLED:
            return False

        try:
            result = ma.import_shared_memory(shared_id, target_agent_id)
            if isinstance(result, dict):
                return bool(result.get("ok", result.get("success", False)))
            if isinstance(result, str):
                parsed = json.loads(result)
                return bool(parsed.get("ok", parsed.get("success", False)))
            return bool(result)
        except Exception as exc:
            logger.exception("Import shared failed")
            raise SyncError(f"Import shared failed: {exc}") from exc

    def auto_share(
        self,
        agent_id: str = "",
        min_importance: int = 0,
        min_fitness: float = 0.0,
        limit: int = 0,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Scan high-importance memories and share them with peers.

        Args:
            agent_id: Sharing agent (defaults to local CRDT agent id).
            min_importance: Override importance threshold (1-5).
            min_fitness: Override fitness threshold (0.0-1.0).
            limit: Max notes to share in this call.
            dry_run: If True, list candidates without sharing.

        Returns:
            Dict with ``scanned``, ``shared``, ``candidates``, etc.
        """
        import memory_sharing as ma

        if not ma.MULTI_AGENT_ENABLED:
            return {
                "enabled": False,
                "message": "Set MEMORY_MULTI_AGENT=1 to enable.",
            }

        try:
            kwargs: dict[str, Any] = {}
            if min_importance:
                kwargs["min_importance"] = min_importance
            if min_fitness:
                kwargs["min_fitness"] = min_fitness
            if limit:
                kwargs["limit"] = limit
            if agent_id:
                kwargs["agent_id"] = agent_id

            if dry_run:
                candidates = ma.list_share_candidates(**kwargs)
                return {
                    "enabled": True,
                    "dry_run": True,
                    "candidates": candidates,
                    "count": len(candidates) if isinstance(candidates, list) else 0,
                }

            result = ma.auto_share_high_value(**kwargs)
            if isinstance(result, str):
                return json.loads(result)
            return dict(result)
        except Exception as exc:
            logger.exception("Auto-share failed")
            raise SyncError(f"Auto-share failed: {exc}") from exc
