"""HTTP client for auto multi-agent memory sync.

Pushes and pulls CRDT-tracked memories to/from peer sync servers
using the same protocol that ``sync_server.py`` serves.

Pulling
-------
1. Fetch the peer's last-sync timestamp from the local ``sync_log`` table.
2. ``GET /crdt/changes?since=<timestamp>&agent=<agent_id>``
3. For each change received, call ``crdt_save()`` locally.

Pushing
-------
1. Query local notes modified since the last successful push to this peer.
2. ``POST /crdt/push`` with the local changes (in ``crdt_sync_all`` format).
3. Record the result in ``sync_log``.

Thread safety
-------------
All client functions are stateless — they open their own HTTP connections
and DB connections. Safe to call from any thread.
"""

from __future__ import annotations

import logging

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Optional, cast

from infra.sync_server import SYNC_AUTH_TOKEN as _SERVER_SYNC_TOKEN

logger = logging.getLogger(__name__)

# Default timeout for HTTP requests (seconds).
_HTTP_TIMEOUT = 30


def _get_sync_token() -> str:
    """Resolve the sync auth token at runtime.

    Resolution order:
      1. ``MEMORY_SYNC_TOKEN`` env var (explicit sync token).
      2. ``MEMORY_API_TOKEN`` env var (legacy, also used by sync server).
      3. ``.api_token`` file in the memory directory (persisted at server start).
      4. Empty string (no token — loopback-only mode).
    """
    import os
    token = os.environ.get("MEMORY_SYNC_TOKEN", "").strip()
    if token:
        return token
    token = os.environ.get("MEMORY_API_TOKEN", "").strip()
    if token:
        return token
    # Fallback: read from .api_token file (written by api_server at startup)
    try:
        from infra.infrastructure import resolve_active_memory_dir
        token_file = resolve_active_memory_dir() / ".api_token"
        if token_file.exists():
            return token_file.read_text().strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _log_sync_result(
    db_path: str | Path,
    peer_name: str,
    peer_url: str,
    peer_agent_id: str,
    direction: str,
    success: bool,
    changes_pushed: int = 0,
    changes_pulled: int = 0,
    error_message: str = "",
    error_count: int = 0,
    duration_ms: int = 0,
) -> None:
    """Insert a row into ``sync_log`` after a sync cycle."""
    import sqlite3

    db_path = Path(db_path)
    if not db_path.exists():
        logger.warning("sync_log: DB not found at %s", db_path)
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute(
                """INSERT INTO sync_log
                   (peer_name, peer_url, peer_agent_id, direction,
                    started_at, completed_at, success,
                    changes_pushed, changes_pulled,
                    error_message, error_count, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    peer_name,
                    peer_url,
                    peer_agent_id,
                    direction,
                    time.time() - duration_ms / 1000,
                    time.time(),
                    1 if success else 0,
                    changes_pushed,
                    changes_pulled,
                    error_message[:500] if error_message else "",
                    error_count,
                    duration_ms,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("sync_log: write failed: %s", e)


def _get_last_push_timestamp(db_path: str | Path, peer_name: str) -> float:
    """Return the unix epoch of the last successful push to ``peer_name``.

    Returns 0 if no successful push has been recorded.
    """
    import sqlite3

    db_path = Path(db_path)
    if not db_path.exists():
        return 0.0
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            row = conn.execute(
                """SELECT MAX(completed_at) FROM sync_log
                   WHERE peer_name=? AND direction='push' AND success=1""",
                (peer_name,),
            ).fetchone()
            return float(row[0]) if row and row[0] else 0.0
        finally:
            conn.close()
    except Exception as e:
        logger.warning("_get_last_push_timestamp failed: %s", e)
        return 0.0


def _get_last_pull_timestamp(db_path: str | Path, peer_name: str) -> float:
    """Return the unix epoch of the last successful pull from ``peer_name``.

    Returns 0 if no successful pull has been recorded.
    """
    import sqlite3

    db_path = Path(db_path)
    if not db_path.exists():
        return 0.0
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            row = conn.execute(
                """SELECT MAX(completed_at) FROM sync_log
                   WHERE peer_name=? AND direction='pull' AND success=1""",
                (peer_name,),
            ).fetchone()
            return float(row[0]) if row and row[0] else 0.0
        finally:
            conn.close()
    except Exception as e:
        logger.warning("_get_last_pull_timestamp failed: %s", e)
        return 0.0


def _open_conn(db_path: str | Path) -> Any:
    import sqlite3

    return sqlite3.connect(str(db_path), timeout=10)


def _query_skills_since(db_path: str | Path, since: float, limit: int) -> list:
    """Return skill rows modified after `since` for skill sync."""
    import sqlite3

    db = Path(db_path)
    if not db.exists() or since <= 0:
        return []
    conn = sqlite3.connect(str(db), timeout=10)
    try:
        try:
            from skill_extractor import ensure_skill_schema
            ensure_skill_schema(conn)
        except ImportError:
            pass
        rows = conn.execute(
            """SELECT id, name, source_memory_id, topic, description,
                      triggers, steps, content_hash, hit_count,
                      last_used_at, hit_vector, last_used_vector,
                      logical_clock, created_at, updated_at
               FROM memory_skills
               WHERE updated_at > ?
               ORDER BY updated_at ASC
               LIMIT ?""",
            (since, limit),
        ).fetchall()
        return list(rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _json_get(url: str, timeout: int = _HTTP_TIMEOUT) -> Optional[dict]:
    """HTTP GET, return parsed JSON dict or None on failure."""
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-Sync-Timestamp", str(int(time.time())))
        _token = _get_sync_token()
        if _token:
            req.add_header("Authorization", f"Bearer {_token}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return cast(Optional[dict], json.loads(body))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        json.JSONDecodeError,
        TimeoutError,
    ) as e:
        logger.debug("sync_client: GET %s failed: %s", url, e)
        return None


def _json_post(url: str, data: dict, timeout: int = _HTTP_TIMEOUT) -> Optional[dict]:
    """HTTP POST with JSON body, return parsed JSON dict or None."""
    try:
        body_bytes = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body_bytes,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Sync-Timestamp": str(int(time.time())),
            },
            )
        _token = _get_sync_token()
        if _token:
            req.add_header("Authorization", f"Bearer {_token}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8")
            return cast(Optional[dict], json.loads(resp_body))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        json.JSONDecodeError,
        TimeoutError,
    ) as e:
        logger.debug("sync_client: POST %s failed: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Pull: fetch remote changes and merge locally
# ---------------------------------------------------------------------------


def pull_from_peer(
    db_path: str | Path,
    peer_url: str,
    peer_name: str,
    peer_agent_id: str,
    local_agent_id: str,
    since: float = 0.0,
    limit: int = 200,
) -> dict:
    """Pull changes from a peer server and merge them into local storage.

    Args:
        db_path: Path to local memory.db.
        peer_url: Base URL of the peer sync server (e.g. ``http://host:9877``).
        peer_name: Human-readable peer name for logging.
        peer_agent_id: The agent id the peer identifies as.
        local_agent_id: This agent's id (used for CRDT saves).
        since: Unix epoch timestamp. Only fetch notes changed after this.
        limit: Maximum number of changes per request.

    Returns:
        Dict with ``applied``, ``conflict``, ``rejected``, ``total``, ``error``.
    """
    start = time.time()

    changes_url = (
        f"{peer_url.rstrip('/')}/crdt/changes"
        f"?since={int(since)}&agent={local_agent_id}&limit={limit}"
    )
    resp = _json_get(changes_url)
    if resp is None:
        return {"error": f"Failed to fetch changes from {peer_url}"}

    changes = resp.get("changes", [])
    if not changes:
        duration = int((time.time() - start) * 1000)
        _log_sync_result(
            db_path,
            peer_name,
            peer_url,
            peer_agent_id,
            "pull",
            True,
            changes_pulled=0,
            duration_ms=duration,
        )
        return {"applied": 0, "conflict": 0, "rejected": 0, "total": 0}

    from crdt.crdt_merge import crdt_save
    from crdt.crdt_field import crdt_field_save

    applied = conflict = rejected = 0
    for note in changes:
        note_id = note.get("id", "")
        if not note_id:
            continue
        # 2026-06-20 (v13): prefer the per-field LWWES path when
        # the server includes field_crdt in the response. The
        # note-level fallback is still there for pre-v13 peers
        # (where the field_crdt list is absent or empty).
        field_crdt = note.get("field_crdt") or []
        if field_crdt:
            r = crdt_field_save(
                db_path,
                note_id,
                note.get("content", ""),
                peer_agent_id,
                local_agent_id,
                source_file=note.get("source_file", ""),
                remote_vv_str=note.get("version_vector", "{}"),
                remote_logical_clock=int(note.get("logical_clock", 0)),
                tags=note.get("tags", "[]"),
            )
        else:
            r = crdt_save(
                db_path,
                note_id,
                note.get("content", ""),
                peer_agent_id,
                local_agent_id,
                source_file=note.get("source_file", ""),
                remote_vv_str=note.get("version_vector", "{}"),
                remote_logical_clock=int(note.get("logical_clock", 0)),
            )
        if r.get("applied"):
            applied += 1
        if r.get("conflict"):
            conflict += 1
        if r.get("rejected"):
            rejected += 1

    duration = int((time.time() - start) * 1000)
    _log_sync_result(
        db_path,
        peer_name,
        peer_url,
        peer_agent_id,
        "pull",
        True,
        changes_pulled=len(changes),
        duration_ms=duration,
    )
    return {
        "applied": applied,
        "conflict": conflict,
        "rejected": rejected,
        "total": len(changes),
    }


# ---------------------------------------------------------------------------
# Push: send local changes to a peer
# ---------------------------------------------------------------------------


def push_to_peer(
    db_path: str | Path,
    peer_url: str,
    peer_name: str,
    peer_agent_id: str,
    local_agent_id: str,
    since: float = 0.0,
    limit: int = 200,
    tenant_id: str = "default",
) -> dict:
    """Push local changes since ``since`` to a peer server.

    Args:
        db_path: Path to local memory.db.
        peer_url: Base URL of the peer sync server.
        peer_name: Human-readable peer name.
        peer_agent_id: The agent id the peer identifies as.
        local_agent_id: This agent's id (sent in the push payload).
        since: Unix epoch timestamp. Only push notes modified after this.
        limit: Maximum number of notes per push.
        tenant_id: Tenant scope for the push (default "default").

    Returns:
        Dict with peer's response (applied/conflict/rejected/total) or error.
    """
    start = time.time()

    # Query local changes since last sync.
    try:
        import sqlite3

        db = Path(db_path)
        if not db.exists():
            return {"error": "local memory.db not found"}

        conn = sqlite3.connect(str(db), timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            # SEC fix: filter by tenant_id to prevent pushing
            # cross-tenant data to peers.
            rows = conn.execute(
                """SELECT id, content, source_file, logical_clock,
                          version_vector
                   FROM memories
                   WHERE deleted_at IS NULL
                     AND tenant_id = ?
                     AND CAST(strftime('%s', updated_at) AS INTEGER) > ?
                   ORDER BY updated_at ASC
                   LIMIT ?""",
                (tenant_id, int(since), limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("push_to_peer failed: %s", e)
        return {"error": f"Local query failed: {e}"}

    if not rows:
        duration = int((time.time() - start) * 1000)
        _log_sync_result(
            db_path,
            peer_name,
            peer_url,
            peer_agent_id,
            "push",
            True,
            changes_pushed=0,
            duration_ms=duration,
        )
        return {"applied": 0, "conflict": 0, "rejected": 0, "total": 0}

    notes = {}
    note_ids = [row[0] for row in rows]
    for row in rows:
        notes[row[0]] = {
            "content": row[1] or "",
            "source_file": row[2] or row[0],
            "logical_clock": row[3] or 0,
            "version_vector": row[4] or "{}",
        }

    # C2 fix: include field_crdt data so the peer can apply
    # per-field LWWES merges instead of note-level fallback.
    if note_ids:
        conn2 = sqlite3.connect(str(db), timeout=10)
        conn2.execute("PRAGMA foreign_keys=ON")
        try:
            placeholders = ",".join("?" for _ in note_ids)
            field_rows = conn2.execute(
                f"""SELECT memory_id, field_name, value,
                           version_vector, logical_clock,
                           last_writer_agent
                    FROM memory_field_crdt
                    WHERE memory_id IN ({placeholders})
                      AND is_deleted = 0""",
                note_ids,
            ).fetchall()
            for fr in field_rows:
                notes.setdefault(fr[0], {}).setdefault("field_crdt", []).append(
                    {
                        "field": fr[1],
                        "value": fr[2],
                        "version_vector": fr[3] or "{}",
                        "logical_clock": int(fr[4] or 0),
                        "last_writer_agent": fr[5] or "",
                    }
                )
        finally:
            conn2.close()

    push_url = f"{peer_url.rstrip('/')}/crdt/push"
    resp = _json_post(
        push_url,
        {"agent_id": local_agent_id, "notes": notes},
    )
    if resp is None:
        return {"error": f"Failed to push to {push_url}"}

    duration = int((time.time() - start) * 1000)
    _log_sync_result(
        db_path,
        peer_name,
        peer_url,
        peer_agent_id,
        "push",
        True,
        changes_pushed=len(notes),
        duration_ms=duration,
    )
    return {
        "applied": resp.get("applied", 0),
        "conflict": resp.get("conflict", 0),
        "rejected": resp.get("rejected", 0),
        "total": resp.get("total", len(notes)),
        "response": resp,
    }


# ---------------------------------------------------------------------------
# Sprint 2.4: KG CRDT sync
# ---------------------------------------------------------------------------


def pull_kg_changes(
    peer_url: str,
    since_ts: float,
    limit: int = 500,
) -> list[dict]:
    """Pull KG CRDT changes from a peer.

    Sprint 2.4: Calls /crdt/kg/changes endpoint.

    The server returns ``{"entity_ops": [...], "edge_ops": [...]}``. We
    normalise both into a single list tagged with a ``type`` field
    ("entity" / "edge") so the caller's dispatch loop (which keys on
    ``op["type"]``) works. A bug fix: the original code read
    ``resp.get("changes", [])`` which never matched the server's keys,
    so pulls silently returned nothing.
    """
    url = f"{peer_url.rstrip('/')}/crdt/kg/changes?since={int(since_ts)}&limit={limit}"
    resp = _json_get(url)
    if resp is None:
        return []
    merged: list[dict] = []
    for op in resp.get("entity_ops", []) or []:
        op = dict(op)
        op["type"] = "entity"
        merged.append(op)
    for op in resp.get("edge_ops", []) or []:
        op = dict(op)
        op["type"] = "edge"
        merged.append(op)
    return merged


def push_kg_changes(
    peer_url: str,
    local_agent_id: str,
    changes: list[dict],
) -> dict:
    """Push KG CRDT changes to a peer.

    Sprint 2.4: Calls /crdt/kg/push endpoint.

    The server expects ``{"entity_ops": [...], "edge_ops": [...]}``. We
    split the mixed ``changes`` list (each tagged with a ``type`` field)
    into those two lists. A bug fix: the original code sent
    ``{"ops": changes}`` which the server ignored (it reads
    ``entity_ops`` / ``edge_ops``), so pushes silently applied nothing.
    """
    entity_ops = [op for op in changes if op.get("type") == "entity"]
    edge_ops = [op for op in changes if op.get("type") == "edge"]
    url = f"{peer_url.rstrip('/')}/crdt/kg/push"
    resp = _json_post(url, {
        "agent_id": local_agent_id,
        "entity_ops": entity_ops,
        "edge_ops": edge_ops,
    })
    if resp is None:
        return {"error": f"Failed to push to {url}"}
    return resp


def sync_kg_with_peer(
    db_path: str | Path,
    peer_url: str,
    peer_name: str,
    local_agent_id: str,
    since_ts: float = 0,
) -> dict:
    """Full KG sync cycle with a peer.

    Sprint 2.4: Pull remote KG changes, then push local changes. After
    both directions, project the merged CRDT ops into the canonical
    ``kg_entities`` / ``kg_edges`` tables so the graph actually converges
    (previously the ops were stored but never projected, leaving the
    canonical tables stale).
    """
    from kg.kg_crdt import (
        ensure_kg_crdt_schema,
        record_entity_add,
        record_edge_add,
        compute_entity_crdt_state,
        compute_edge_crdt_state,
        project_crdt_to_entities,
    )
    from infra.db import open_db

    results = {"pulled": 0, "pushed": 0, "projected": None, "errors": []}

    # Pull remote changes
    try:
        remote_changes = pull_kg_changes(peer_url, since_ts)
        if remote_changes:
            with open_db(Path(db_path), timeout=10.0) as conn:
                ensure_kg_crdt_schema(conn)
                for op in remote_changes:
                    try:
                        if op.get("type") == "entity":
                            record_entity_add(
                                conn,
                                op["entity_id"],
                                op.get("agent_id", peer_name),
                                op.get("version_vector", {}),
                                op.get("name", ""),
                                op.get("entity_type", ""),
                                op.get("description", ""),
                                op.get("fingerprint"),
                            )
                            results["pulled"] += 1
                        elif op.get("type") == "edge":
                            record_edge_add(
                                conn,
                                op["source_id"],
                                op["target_id"],
                                op.get("relation", ""),
                                op.get("weight", 1.0),
                                op.get("agent_id", peer_name),
                                op.get("version_vector", {}),
                                op.get("valid_at"),
                            )
                            results["pulled"] += 1
                    except Exception as op_exc:
                        results["errors"].append(str(op_exc))
                conn.commit()
                # Project pulled ops into canonical tables.
                try:
                    n_e, n_g, _ = project_crdt_to_entities(conn)
                    results["projected"] = {"entities": n_e, "edges": n_g}
                except Exception as proj_exc:
                    results["errors"].append(f"project(pull) failed: {proj_exc}")
    except Exception as exc:
        results["errors"].append(f"pull failed: {exc}")

    # Push local changes
    try:
        with open_db(Path(db_path), timeout=10.0) as conn:
            ensure_kg_crdt_schema(conn)
            entity_state = compute_entity_crdt_state(conn)
            edge_state = compute_edge_crdt_state(conn)

            local_ops = []
            for entity_id, info in entity_state.items():
                local_ops.append({
                    "type": "entity",
                    "entity_id": entity_id,
                    "agent_id": local_agent_id,
                    "name": info.get("name", ""),
                    "entity_type": info.get("entity_type", ""),
                    "description": info.get("description", ""),
                    "fingerprint": info.get("fingerprint"),
                    "version_vector": info.get("version_vector", {}),
                    "timestamp": info.get("timestamp", 0),
                })
            for edge_id, info in edge_state.items():
                local_ops.append({
                    "type": "edge",
                    "edge_id": edge_id,
                    "source_id": info.get("source_id", 0),
                    "target_id": info.get("target_id", 0),
                    "relation": info.get("relation", ""),
                    "weight": info.get("weight", 1.0),
                    "valid_at": info.get("valid_at"),
                    "agent_id": local_agent_id,
                    "version_vector": info.get("version_vector", {}),
                    "timestamp": info.get("timestamp", 0),
                })

            if local_ops:
                push_resp = push_kg_changes(peer_url, local_agent_id, local_ops)
                results["pushed"] = push_resp.get("applied", 0)
                # Re-project after recording any locally-new ops so the
                # canonical tables reflect the full merged state.
                try:
                    n_e, n_g, _ = project_crdt_to_entities(conn)
                    results["projected"] = {"entities": n_e, "edges": n_g}
                except Exception as proj_exc:
                    results["errors"].append(f"project(push) failed: {proj_exc}")
    except Exception as exc:
        results["errors"].append(f"push failed: {exc}")

    return results


# ---------------------------------------------------------------------------
# Full sync: push then pull
# ---------------------------------------------------------------------------


def sync_with_peer(
    db_path: str | Path,
    peer_url: str,
    peer_name: str,
    peer_agent_id: str,
    local_agent_id: str,
    limit: int = 200,
    tenant_id: str = "default",
) -> dict:
    """Run a full two-way sync with a peer (push local, then pull remote).

    Returns aggregated results for both directions.
    """
    start = time.time()
    last_push_ts = _get_last_push_timestamp(db_path, peer_name)
    last_pull_ts = _get_last_pull_timestamp(db_path, peer_name)

    push_result = push_to_peer(
        db_path,
        peer_url,
        peer_name,
        peer_agent_id,
        local_agent_id,
        since=last_push_ts,
        limit=limit,
        tenant_id=tenant_id,
    )
    pull_result = pull_from_peer(
        db_path,
        peer_url,
        peer_name,
        peer_agent_id,
        local_agent_id,
        since=last_pull_ts,
        limit=limit,
    )

    try:
        skill_sync_result = sync_skills_with_peer(
            db_path,
            peer_url,
            peer_name,
            peer_agent_id,
            local_agent_id,
            limit=max(limit, 500),
        )
    except Exception as e:
        logger.warning("sync_with_peer: skills sync failed: %s", e)
        skill_sync_result = {"error": str(e), "success": False}

    try:
        agent_sync_result = sync_agents_with_peer(
            db_path,
            peer_url,
            peer_name,
            local_agent_id,
            limit=limit,
        )
    except Exception as e:
        logger.warning("sync_with_peer: agent sync failed: %s", e)
        agent_sync_result = {"error": str(e), "pulled": 0, "pushed": 0}

    duration = int((time.time() - start) * 1000)
    success = ("error" not in push_result or not push_result.get("error")) and (
        "error" not in pull_result or not pull_result.get("error")
    )

    _log_sync_result(
        db_path,
        peer_name,
        peer_url,
        peer_agent_id,
        "sync",
        success,
        changes_pushed=push_result.get("total", 0),
        changes_pulled=pull_result.get("total", 0),
        error_message=push_result.get("error", "") or pull_result.get("error", ""),
        error_count=(1 if push_result.get("error") else 0)
        + (1 if pull_result.get("error") else 0),
        duration_ms=duration,
    )

    return {
        "push": push_result,
        "pull": pull_result,
        "skills": skill_sync_result,
        "agents": agent_sync_result,
        "duration_ms": duration,
        "success": success,
    }


def sync_skills_with_peer(
    db_path: str | Path,
    peer_url: str,
    peer_name: str,
    peer_agent_id: str,
    local_agent_id: str,
    limit: int = 500,
) -> dict:
    """Two-way skills sync using CRDT merge (pull remote changes, push local changes).

    Returns applied/skipped counts for both directions.
    """
    try:
        from skill_extractor import ensure_skill_schema, merge_and_save_skill
    except ImportError as e:
        return {"error": f"skill_extractor not available: {e}"}

    start = time.time()
    ensure_skill_schema(_open_conn(db_path))

    since_pull = _get_last_pull_timestamp(db_path, peer_name)
    since_push = _get_last_push_timestamp(db_path, peer_name)

    pull_url = (
        f"{peer_url.rstrip('/')}/skills/changes"
        f"?since={int(since_pull)}&agent={local_agent_id}&limit={limit}"
    )
    resp = _json_get(pull_url)
    if resp is None:
        return {"error": f"Failed to fetch skill changes from {peer_url}"}

    remote_skills = resp.get("skills", [])
    applied = skipped = 0
    for skill_dict in remote_skills:
        skill_conn = _open_conn(db_path)
        try:
            merge_and_save_skill(skill_conn, skill_dict)
            applied += 1
        except Exception as e:
            logger.warning("sync_skills_with_peer failed: %s", e)
            skipped += 1
        finally:
            skill_conn.close()

    local_rows = _query_skills_since(db_path, since_push, limit)
    local_skills = []
    cols = ["id", "name", "source_memory_id", "topic", "description",
            "triggers", "steps", "content_hash", "hit_count",
            "last_used_at", "hit_vector", "last_used_vector",
            "logical_clock", "created_at", "updated_at"]
    for row in local_rows:
        skill: dict = {}
        for col, val in zip(cols, row):
            if col in ("hit_vector", "last_used_vector") and val is not None:
                try:
                    skill[col] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    skill[col] = {}
            elif col in ("triggers", "steps") and val is not None:
                try:
                    skill[col] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    skill[col] = [] if col == "triggers" else []
            else:
                skill[col] = val
        local_skills.append(skill)

    push_url = f"{peer_url.rstrip('/')}/skills/push"
    push_resp = _json_post(push_url, {"skills": local_skills})
    push_applied = push_resp.get("applied", 0) if push_resp else 0
    push_skipped = push_resp.get("skipped", 0) if push_resp else 0

    duration = int((time.time() - start) * 1000)
    _log_sync_result(
        db_path,
        peer_name,
        peer_url,
        peer_agent_id,
        "sync",
        True,
        changes_pulled=applied,
        changes_pushed=push_applied,
        duration_ms=duration,
    )
    return {
        "pull_applied": applied,
        "pull_skipped": skipped,
        "push_applied": push_applied,
        "push_skipped": push_skipped,
        "duration_ms": duration,
        "success": True,
    }


# ---------------------------------------------------------------------------
# Agent registry sync (sync-based agent discovery)
# ---------------------------------------------------------------------------


def pull_agent_changes(
    peer_url: str,
    since_ts: float,
    limit: int = 200,
) -> list[dict]:
    """Pull agent registry entries from a peer.

    Calls ``GET /agents/changes``. Returns list of agent entry dicts.
    """
    url = f"{peer_url.rstrip('/')}/agents/changes?since={int(since_ts)}&limit={limit}"
    resp = _json_get(url)
    if resp is None:
        return []
    return resp.get("agents", []) or []


def push_agent_changes(
    peer_url: str,
    local_agent_id: str,
    entries: list[dict],
) -> dict:
    """Push agent registry entries to a peer.

    Calls ``POST /agents/push``. Expects a list of agent entry dicts.
    """
    url = f"{peer_url.rstrip('/')}/agents/push"
    resp = _json_post(url, {
        "agent_id": local_agent_id,
        "agents": entries,
    })
    if resp is None:
        return {"error": f"Failed to push to {url}"}
    return resp


def _get_last_agent_pull_timestamp(db_path: str | Path, peer_name: str) -> float:
    """Return the unix epoch of the last successful agent registry pull.

    Uses direction='agent_pull' in sync_log to avoid collision with
    note sync timestamps.
    """
    import sqlite3
    db_path = Path(db_path)
    if not db_path.exists():
        return 0.0
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            row = conn.execute(
                """SELECT MAX(completed_at) FROM sync_log
                   WHERE peer_name=? AND direction='agent_pull' AND success=1""",
                (peer_name,),
            ).fetchone()
            return float(row[0]) if row and row[0] else 0.0
        finally:
            conn.close()
    except Exception:
        return 0.0


def _get_last_agent_push_timestamp(db_path: str | Path, peer_name: str) -> float:
    """Return the unix epoch of the last successful agent registry push.

    Uses direction='agent_push' in sync_log to avoid collision with
    note sync timestamps.
    """
    import sqlite3
    db_path = Path(db_path)
    if not db_path.exists():
        return 0.0
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            row = conn.execute(
                """SELECT MAX(completed_at) FROM sync_log
                   WHERE peer_name=? AND direction='agent_push' AND success=1""",
                (peer_name,),
            ).fetchone()
            return float(row[0]) if row and row[0] else 0.0
        finally:
            conn.close()
    except Exception:
        return 0.0


def _log_agent_sync_result(
    db_path: str | Path,
    peer_name: str,
    peer_url: str,
    agent_id: str,
    direction: str,
    success: bool,
    changes_pulled: int = 0,
    changes_pushed: int = 0,
    error_message: str = "",
    error_count: int = 0,
    duration_ms: int = 0,
) -> None:
    """Log an agent registry sync result to sync_log."""
    _log_sync_result(
        db_path, peer_name, peer_url, agent_id,
        direction, success,
        changes_pushed=changes_pushed, changes_pulled=changes_pulled,
        error_message=error_message, error_count=error_count,
        duration_ms=duration_ms,
    )


def sync_agents_with_peer(
    db_path: str | Path,
    peer_url: str,
    peer_name: str,
    local_agent_id: str,
    limit: int = 200,
) -> dict:
    """Two-way agent registry sync with a peer.

    Pulls remote agent entries, then pushes local entries.
    Uses dedicated timestamp tracking (agent_pull/agent_push)
    so it doesn't collide with note sync timestamps.
    """
    start = time.time()
    results = {"pulled": 0, "pushed": 0, "errors": []}

    since_pull = _get_last_agent_pull_timestamp(db_path, peer_name)
    since_push = _get_last_agent_push_timestamp(db_path, peer_name)

    # Pull remote agent entries
    try:
        remote_entries = pull_agent_changes(peer_url, since_pull, limit)
        if remote_entries:
            conn = _open_conn(db_path)
            try:
                from crdt.crdt_merge import parse_version_vector, dominates, merge_vectors

                for entry in remote_entries:
                    agent_id = entry.get("agent_id", "")
                    if not agent_id:
                        continue
                    remote_vv = parse_version_vector(entry.get("version_vector", "{}"))
                    remote_clock = int(entry.get("logical_clock", 0))
                    remote_agent = entry.get("display_name", agent_id)

                    existing = conn.execute(
                        "SELECT version_vector, logical_clock FROM agent_registry_crdt WHERE agent_id = ? AND tenant_id = 'default'",
                        (agent_id,),
                    ).fetchone()

                    if existing:
                        existing_vv = parse_version_vector(existing[0])
                        existing_clock = existing[1]
                        if dominates(remote_vv, existing_vv):
                            pass  # remote wins
                        elif dominates(existing_vv, remote_vv):
                            continue  # local is newer
                        else:
                            remote_wins = (remote_clock, remote_agent) > (existing_clock, agent_id)
                            if not remote_wins:
                                continue
                    else:
                        pass  # new entry

                    merged_vv = merge_vectors(local_agent_id, existing_vv if existing else {}, remote_vv)
                    new_clock = max(remote_clock, existing[1] if existing else 0) + 1

                    conn.execute(
                        """INSERT OR REPLACE INTO agent_registry_crdt
                           (agent_id, display_name, parent_agent, namespace,
                            logical_clock, version_vector, last_seen, is_deleted, tenant_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'default')""",
                        (
                            agent_id,
                            entry.get("display_name", agent_id),
                            entry.get("parent_agent", ""),
                            entry.get("namespace", agent_id),
                            new_clock,
                            json.dumps(merged_vv),
                            entry.get("last_seen", time.time()),
                            entry.get("is_deleted", 0),
                        ),
                    )
                    results["pulled"] += 1
                conn.commit()
            except Exception as e:
                results["errors"].append(f"pull merge failed: {e}")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        _log_agent_sync_result(
            db_path, peer_name, peer_url, local_agent_id,
            "agent_pull", True,
            changes_pulled=results["pulled"],
            duration_ms=int((time.time() - start) * 1000),
        )
    except Exception as exc:
        results["errors"].append(f"pull failed: {exc}")
        _log_agent_sync_result(
            db_path, peer_name, peer_url, local_agent_id,
            "agent_pull", False,
            error_message=str(exc),
        )

    # Push local agent entries
    try:
        conn = _open_conn(db_path)
        try:
            rows = conn.execute(
                """SELECT agent_id, display_name, parent_agent, namespace,
                          logical_clock, version_vector, last_seen, is_deleted
                   FROM agent_registry_crdt
                   WHERE tenant_id = 'default'
                   ORDER BY last_seen ASC""",
            ).fetchall()
            local_entries = []
            for row in rows:
                local_entries.append({
                    "agent_id": row[0],
                    "display_name": row[1],
                    "parent_agent": row[2],
                    "namespace": row[3],
                    "logical_clock": row[4],
                    "version_vector": row[5],
                    "last_seen": row[6],
                    "is_deleted": row[7],
                })
            if local_entries:
                push_resp = push_agent_changes(peer_url, local_agent_id, local_entries)
                results["pushed"] = push_resp.get("applied", 0)
                if push_resp.get("error"):
                    results["errors"].append(push_resp["error"])
        except Exception as e:
            results["errors"].append(f"local query failed: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
        _log_agent_sync_result(
            db_path, peer_name, peer_url, local_agent_id,
            "agent_push", True,
            changes_pushed=results["pushed"],
            duration_ms=int((time.time() - start) * 1000),
        )
    except Exception as exc:
        results["errors"].append(f"push failed: {exc}")
        _log_agent_sync_result(
            db_path, peer_name, peer_url, local_agent_id,
            "agent_push", False,
            error_message=str(exc),
        )

    duration = int((time.time() - start) * 1000)
    _log_agent_sync_result(
        db_path, peer_name, peer_url, local_agent_id,
        "agent_sync", not results["errors"],
        changes_pushed=results["pushed"],
        changes_pulled=results["pulled"],
        duration_ms=duration,
    )
    return results


# ---------------------------------------------------------------------------
# One-shot sync (P2 #2 wire-up)
# ---------------------------------------------------------------------------


def sync_once(
    peer_url: str | None = None,
    db_path: str | Path | None = None,
    peer_name: str | None = None,
    peer_agent_id: str | None = None,
    local_agent_id: str | None = None,
    limit: int = 200,
    tenant_id: str = "default",
) -> dict:
    """Run a one-shot two-way sync to a single peer.

    This is the P2 #2 wire-up: the function that ``agentic-memory-sync
    --peer <url>`` and the ``cron/cron_sync.py`` cron invoke. It
    resolves sensible defaults from env / config and writes a row to
    ``sync_log`` (via ``sync_with_peer``) so the sync_log table stops
    being empty.

    Args:
        peer_url:        URL of the peer sync server. Falls back to
                         ``MEMORY_SYNC_PEER`` env var.
        db_path:         local memory.db path. Falls back to
                         ``MEMORY_DB_PATH`` env var, then
                         ``config.get().db_path``.
        peer_name:       human-readable peer name. Defaults to the host
                         portion of ``peer_url`` or ``"peer"``.
        peer_agent_id:   agent id the peer identifies as. Falls back
                         to ``MEMORY_SYNC_PEER_AGENT_ID`` env var.
        local_agent_id:  this agent's id. Falls back to the
                         ``_crdt_agent_id()`` helper (env >
                         config.agent_id > hostname > "local").
        limit:           max notes per push/pull.

    Returns:
        dict from ``sync_with_peer`` (push/pull/duration_ms/success),
        or ``{"error": "..."}`` when the call cannot run (no peer,
        no db, etc.).
    """
    # Resolve peer_url.
    if not peer_url:
        peer_url = os.environ.get("MEMORY_SYNC_PEER", "").strip()
    if not peer_url:
        from infra.pex_protocol import peer_directory
        active_peers = peer_directory.get_active_peers(max_age_s=60.0)
        if active_peers:
            # Sync with all active discovered peers
            results = []
            for p in active_peers:
                r = sync_with_peer(
                    db_path=db_path or "memory/memory.db",
                    peer_url=p["url"],
                    peer_name=p["agent_id"],
                    peer_agent_id=p["agent_id"],
                    local_agent_id=local_agent_id or "local",
                    limit=limit,
                    tenant_id=tenant_id,
                )
                results.append(r)
            success = all(r.get("success", False) for r in results)
            return {"success": success, "results": results, "total_peers": len(results)}
        return {
            "error": (
                "peer_url is required (positional arg or MEMORY_SYNC_PEER env var), and no discovered peers found."
            )
        }

    # Resolve db_path.
    if db_path is None:
        env_db = os.environ.get("MEMORY_DB_PATH", "").strip()
        if env_db:
            db_path = env_db
        else:
            try:
                from infra._lazy_imports import get_config

                cfg = get_config()
                db_path = cfg.db_path
            except Exception as e:
                logger.warning("sync_once failed: %s", e)
                db_path = "memory/memory.db"
    db_path = str(db_path)

    # Resolve peer_name and peer_agent_id.
    if not peer_name:
        # Best-effort: hostname from URL.
        from urllib.parse import urlparse

        try:
            u = urlparse(peer_url)
            peer_name = (u.hostname or "peer") + (f":{u.port}" if u.port else "")
        except Exception as e:
            logger.warning("sync_once failed: %s", e)
            peer_name = "peer"
    if not peer_agent_id:
        peer_agent_id = (
            os.environ.get("MEMORY_SYNC_PEER_AGENT_ID", "").strip() or peer_name
        )
    if not local_agent_id:
        try:
            from save.crdt_helpers import _crdt_agent_id

            local_agent_id = _crdt_agent_id()
        except Exception as e:
            logger.warning("sync_once failed: %s", e)
            local_agent_id = "local"

    if not Path(db_path).exists():
        return {"error": f"local memory.db not found: {db_path}"}

    return sync_with_peer(
        db_path=db_path,
        peer_url=peer_url,
        peer_name=peer_name,
        peer_agent_id=peer_agent_id,
        local_agent_id=local_agent_id,
        limit=limit,
        tenant_id=tenant_id,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list) -> int:
    """Standalone CLI for one-shot peer sync.

    Usage::

        python sync_client.py --peer <url> [--db <path>] [--name <n>]
                              [--peer-agent <id>] [--limit N]
    """
    import argparse

    parser = argparse.ArgumentParser(description="One-shot peer sync (P2 #2)")
    parser.add_argument("--peer", help="Peer sync server URL (or MEMORY_SYNC_PEER)")
    parser.add_argument("--db", help="Local memory.db path (or MEMORY_DB_PATH)")
    parser.add_argument("--name", help="Peer name (for sync_log)")
    parser.add_argument(
        "--peer-agent", help="Peer agent id (or MEMORY_SYNC_PEER_AGENT_ID)"
    )
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv[1:])

    result = sync_once(
        peer_url=args.peer,
        db_path=args.db,
        peer_name=args.name,
        peer_agent_id=args.peer_agent,
        limit=args.limit,
    )
    if "error" in result and not result.get("push"):
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv))
