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

import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, cast

logger = logging.getLogger(__name__)

# Default timeout for HTTP requests (seconds).
_HTTP_TIMEOUT = 30


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
    from infra.db import open_db

    db_path = Path(db_path)
    if not db_path.exists():
        logger.warning("sync_log: DB not found at %s", db_path)
        return
    try:
        with open_db(db_path, timeout=10.0) as conn:
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
    except Exception as e:
        logger.warning("sync_log: write failed: %s", e)


def _get_last_push_timestamp(db_path: str | Path, peer_name: str) -> float:
    """Return the unix epoch of the last successful push to ``peer_name``.

    Returns 0 if no successful push has been recorded.
    """
    from infra.db import open_db

    db_path = Path(db_path)
    if not db_path.exists():
        return 0.0
    try:
        with open_db(db_path, timeout=5.0, pooled=True, write=False) as conn:
            row = conn.execute(
                """SELECT MAX(completed_at) FROM sync_log
                   WHERE peer_name=? AND direction='push' AND success=1""",
                (peer_name,),
            ).fetchone()
            return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0


def _get_last_pull_timestamp(db_path: str | Path, peer_name: str) -> float:
    """Return the unix epoch of the last successful pull from ``peer_name``.

    Returns 0 if no successful pull has been recorded.
    """
    from infra.db import open_db

    db_path = Path(db_path)
    if not db_path.exists():
        return 0.0
    try:
        with open_db(db_path, timeout=5.0, pooled=True, write=False) as conn:
            row = conn.execute(
                """SELECT MAX(completed_at) FROM sync_log
                   WHERE peer_name=? AND direction='pull' AND success=1""",
                (peer_name,),
            ).fetchone()
            return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _json_get(url: str, timeout: int = _HTTP_TIMEOUT) -> Optional[dict]:
    """HTTP GET, return parsed JSON dict or None on failure."""
    try:
        req = urllib.request.Request(url, method="GET")
        # sync_server._check_replay requires X-Sync-Timestamp when
        # SYNC_MAX_REQUEST_AGE > 0. Send it on every request.
        req.add_header("X-Sync-Timestamp", str(int(time.time())))
        # Add Authorization header if SYNC_AUTH_TOKEN is set
        from infra.sync_server import SYNC_AUTH_TOKEN
        if SYNC_AUTH_TOKEN:
            req.add_header("Authorization", f"Bearer {SYNC_AUTH_TOKEN}")
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
        # Add Authorization header if SYNC_AUTH_TOKEN is set
        from infra.sync_server import SYNC_AUTH_TOKEN
        if SYNC_AUTH_TOKEN:
            req.add_header("Authorization", f"Bearer {SYNC_AUTH_TOKEN}")
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

    Returns:
        Dict with peer's response (applied/conflict/rejected/total) or error.
    """
    start = time.time()

    try:
        from infra.db import open_db

        db = Path(db_path)
        if not db.exists():
            return {"error": "local memory.db not found"}

        with open_db(db, timeout=10.0, pooled=True, write=False) as conn:
            rows = conn.execute(
                """SELECT id, content, source_file, logical_clock,
                          version_vector
                   FROM memories
                   WHERE deleted_at IS NULL
                     AND CAST(strftime('%s', updated_at) AS INTEGER) > ?
                   ORDER BY updated_at ASC
                   LIMIT ?""",
                (int(since), limit),
            ).fetchall()
    except Exception as e:
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
    for row in rows:
        notes[row[0]] = {
            "content": row[1] or "",
            "source_file": row[2] or row[0],
            "logical_clock": row[3] or 0,
            "version_vector": row[4] or "{}",
        }

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
# Full sync: push then pull
# ---------------------------------------------------------------------------


def sync_with_peer(
    db_path: str | Path,
    peer_url: str,
    peer_name: str,
    peer_agent_id: str,
    local_agent_id: str,
    limit: int = 200,
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
        "duration_ms": duration,
        "success": success,
    }


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
            except Exception:
                db_path = "memory/memory.db"
    db_path = str(db_path)

    # Resolve peer_name and peer_agent_id.
    if not peer_name:
        # Best-effort: hostname from URL.
        from urllib.parse import urlparse

        try:
            u = urlparse(peer_url)
            peer_name = (u.hostname or "peer") + (f":{u.port}" if u.port else "")
        except Exception:
            peer_name = "peer"
    if not peer_agent_id:
        peer_agent_id = (
            os.environ.get("MEMORY_SYNC_PEER_AGENT_ID", "").strip() or peer_name
        )
    if not local_agent_id:
        try:
            from save.crdt_helpers import _crdt_agent_id

            local_agent_id = _crdt_agent_id()
        except Exception:
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
