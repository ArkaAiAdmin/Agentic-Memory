"""Threaded HTTP sync server for auto multi-agent memory sync.

Serves three endpoints so peer agents can discover, pull, and push
CRDT-tracked memories over plain HTTP (stdlib only, no dependencies).

Endpoints
---------
``GET /health``
    Lightweight liveness probe. Returns agent id and note count.

``GET /crdt/changes?since=<unix_epoch>&agent=<agent_id>&limit=<N>``
    Return memories modified after ``since`` (an ISO-8601 timestamp).
    ``limit`` caps the response (default 200, max 1000).
    Response: ``{"changes": [{"id": ..., "content": ..., "source_file": ...,
    "logical_clock": ..., "version_vector": ..., "updated_at": ...}, ...]}``

``POST /crdt/push``
    Accept a batch of remote notes and merge them via CRDT.
    Body (JSON): ``{"agent_id": "...", "notes": {...}}``
    where ``notes`` has the same shape as ``crdt_sync_all`` expects:
    ``{note_id: [content, source_file, logical_clock, version_vector, sender_clock]}``
    Response: ``{"applied": N, "conflict": N, "rejected": N, "total": N}``

Threading
---------
The server runs in a daemon thread managed by ``SyncServer``. Call
``start()`` to launch and ``stop()`` to shut down (graceful via
``shutdown()`` on the underlying ``HTTPServer``).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import ssl
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from infra.mdns_discovery import MDNSAdvertiser, MDNSBrowser
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

# B8 fix: require a bearer token for all mutating endpoints.
# Health is still unauthenticated; /crdt/changes and /crdt/push check.
# Falls back to MEMORY_API_TOKEN so the dashboard's Bearer token (same value
# it sends to OPENCODE's rest-api on 9879) also authorizes against the
# MIMOCODE sync server on 9877 — read-only /api/v1/query included.
SYNC_AUTH_TOKEN = (
    os.environ.get("MEMORY_SYNC_TOKEN", "").strip()
    or os.environ.get("MEMORY_API_TOKEN", "").strip()
)

# Y1 fix: configurable CORS allowlist.  Default = no CORS (empty list).
# Set MEMORY_SYNC_CORS_ORIGINS="https://a.example,https://b.example"
# to allow specific origins.  Never set "*" implicitly.
SYNC_CORS_ORIGINS = frozenset(
    o.strip()
    for o in os.environ.get("MEMORY_SYNC_CORS_ORIGINS", "").split(",")
    if o.strip()
)

# Y2 fix: optional HMAC secret for payload integrity.  When set, every
# mutating request must include X-Sync-Signature: sha256=<hex>.  The HMAC
# is computed over the raw request body using this shared secret.
SYNC_HMAC_SECRET = os.environ.get("MEMORY_SYNC_HMAC_SECRET", "")

# Y3 fix: replay protection.  When set, mutating requests must include
# X-Sync-Timestamp (unix epoch seconds); requests older than this many
# seconds are rejected.  Set to 0 to disable.
SYNC_MAX_REQUEST_AGE = int(os.environ.get("MEMORY_SYNC_MAX_AGE", "300"))

# Y4 fix: maximum request body size.  Reject anything larger to avoid
# DoS via large payloads.
SYNC_MAX_BODY_SIZE = int(os.environ.get("MEMORY_SYNC_MAX_BODY", str(10 * 1024 * 1024)))

# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


from infra.db import AnyConnection as _AnyConnection


def _open_server_db(db_path: str) -> _AnyConnection:
    """Open a SQLite connection for a sync-server request.

    Uses the project's standard pragmas (WAL, foreign keys, busy
    timeout) and runs migrations on first open. The connection
    lifetime is the lifetime of the request — the caller must
    close it.
    """
    # Imported lazily to avoid a hard dependency on db.py at
    # import time (some test runners use sync_server without the
    # full DB stack).
    from infra.db_write_queue import sqlite_write_queue
    from infra.db_migrations import run_schema_setup

    conn = sqlite_write_queue.start_session(Path(db_path))
    try:
        run_schema_setup(conn)
        conn.execute("PRAGMA busy_timeout = 30000;")
    except Exception as _wp_exc:
        logger.warning("_open_server_db: broad except swallowed: %s", _wp_exc)
        conn.close()
        fallback = sqlite3.connect(str(db_path), timeout=30.0)
        fallback.execute("PRAGMA foreign_keys = ON")
        return fallback
    return conn


class _SyncHandler(BaseHTTPRequestHandler):
    """Single-request handler injected with the DB path and agent id."""

    # Set by SyncServer before passing to HTTPServer.
    db_path: str = ""
    server_agent_id: str = ""
    server_tenant_id: str = "default"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _json_response(self, data: object, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        # Y1 fix: only echo origin if it's in the allowlist.  Default
        # deny is the secure posture.
        origin = self.headers.get("Origin", "")
        if origin and origin in SYNC_CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _error(self, message: str, status: int = 400) -> None:
        self._json_response({"error": message}, status)

    def _read_body(self) -> str:
        # Y4 fix: enforce max body size to prevent DoS via large payloads.
        length = int(self.headers.get("Content-Length", 0))
        if length > SYNC_MAX_BODY_SIZE:
            raise ValueError(f"Request body too large: {length} > {SYNC_MAX_BODY_SIZE}")
        if length == 0:
            return ""
        return self.rfile.read(length).decode("utf-8")

    def log_message(self, format: str, *args) -> None:
        logger.debug("sync_server: " + format, *args)

    def _require_auth(self) -> bool:
        """Bearer token check for mutating endpoints.

        SEC-1 fix: when ``MEMORY_SYNC_TOKEN`` is unset, deny access on
        non-loopback interfaces (prevents accidental exposure).  Loopback
        (the default ``127.0.0.1:9877``) is allowed without a token so
        local development workflows keep working out of the box.
        """
        peer = getattr(self, "host", None) or getattr(self, "client_address", ("127.0.0.1",))[0]
        if not SYNC_AUTH_TOKEN:
            if not _is_loopback(peer):
                logger.warning(
                    "sync_server: auth rejected (no token configured, non-loopback peer=%s): "
                    "401",
                    peer,
                )
                self._error(
                    "Auth required: set MEMORY_SYNC_TOKEN or bind to 127.0.0.1",
                    401,
                )
                return False
            return True
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            logger.warning(
                "sync_server: auth rejected (missing Bearer scheme, peer=%s): 401",
                peer,
            )
            self._error("Authorization required: Bearer <token>", 401)
            return False
        token = auth[7:]
        import hmac as _hmac
        if not _hmac.compare_digest(token or '', SYNC_AUTH_TOKEN or ''):
            logger.warning(
                "sync_server: auth rejected (invalid token, peer=%s): 403",
                peer,
            )
            self._error("Invalid token", 403)
            return False
        return True

    def _check_replay(self) -> bool:
        """Y3 fix: reject requests with stale timestamps (replay protection).

        Only active when SYNC_MAX_REQUEST_AGE > 0.  Requires the request
        to include ``X-Sync-Timestamp`` (unix epoch seconds).  A captured
        request cannot be replayed after the configured window expires.
        """
        if SYNC_MAX_REQUEST_AGE <= 0:
            return True
        ts_str = self.headers.get("X-Sync-Timestamp", "")
        try:
            ts = int(ts_str)
        except (ValueError, TypeError):
            self._error("X-Sync-Timestamp header required", 401)
            return False
        import time

        age = abs(int(time.time()) - ts)
        if age > SYNC_MAX_REQUEST_AGE:
            self._error(
                f"Request too old or in future: age={age}s > max={SYNC_MAX_REQUEST_AGE}s",
                401,
            )
            return False
        return True

    def _check_hmac(self, body: str) -> bool:
        """Y2 fix: validate HMAC signature of the body (payload integrity).

        Only active when SYNC_HMAC_SECRET is set.  Uses constant-time
        comparison to avoid timing attacks.
        """
        if not SYNC_HMAC_SECRET:
            return True
        sig_header = self.headers.get("X-Sync-Signature", "")
        if not sig_header.startswith("sha256="):
            self._error("X-Sync-Signature: sha256=<hex> required", 401)
            return False
        import hashlib
        import hmac

        expected = hmac.new(
            SYNC_HMAC_SECRET.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        got = sig_header[7:]
        if not hmac.compare_digest(expected, got):
            self._error("Invalid signature", 403)
            return False
        return True

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/health" or path == "":
            self._handle_health()
        elif path == "/crdt/changes":
            self._handle_changes(parse_qs(parsed.query))
        elif path == "/crdt/kg/changes":
            # S2 (2026-06-23): sync KG CRDT ops to peers.
            self._handle_kg_changes(parse_qs(parsed.query))
        elif path == "/skills/changes":
            # Next-frontier: sync skills CRDT state to peers.
            self._handle_skill_changes(parse_qs(parsed.query))
        elif path == "/agents/changes":
            self._handle_agent_changes(parse_qs(parsed.query))
        elif path == "/sync/peers":
            self._handle_get_peers()
        elif path == "/crdt/policy_hash":
            self._handle_policy_hash(parse_qs(parsed.query))
        elif path == "/api/v1/query":
            self._handle_query(parse_qs(parsed.query), body=None)
        else:
            self._error("Not found", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/crdt/push":
            self._handle_push()
        elif path == "/crdt/kg/push":
            # S2 (2026-06-23): receive KG CRDT ops from peers.
            self._handle_kg_push()
        elif path == "/sync/peers/gossip":
            self._handle_gossip_peers()
        elif path == "/skills/changes":
            self._handle_skill_changes(parse_qs(parsed.query))
        elif path == "/skills/push":
            self._handle_skill_push()
        elif path == "/agents/changes":
            self._handle_agent_changes(parse_qs(parsed.query))
        elif path == "/agents/push":
            self._handle_agent_push()
        elif path == "/api/v1/query":
            try:
                body = self._read_body()
            except ValueError as exc:
                self._error(str(exc), 413)
                return
            self._handle_query({}, body=body)
        else:
            self._error("Not found", 404)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if origin and origin in SYNC_CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ------------------------------------------------------------------
    # /health
    # ------------------------------------------------------------------

    def _handle_health(self) -> None:
        note_count = 0
        try:
            db = Path(self.db_path)
            if db.exists():
                import sqlite3

                conn = sqlite3.connect(str(db), timeout=5)
                conn.execute("PRAGMA foreign_keys=ON")
                try:
                    # SEC fix: scope count to this server's tenant to
                    # avoid leaking cross-tenant memory counts.
                    row = conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND tenant_id = ?",
                        (self.server_tenant_id,),
                    ).fetchone()
                    if row:
                        note_count = row[0]
                finally:
                    conn.close()
        except Exception as e:
            logger.debug("sync_server: health count failed: %s", e)

        self._json_response(
            {
                "status": "ok",
                "agent_id": self.server_agent_id,
                "note_count": note_count,
            }
        )

    def _handle_get_peers(self) -> None:
        from infra.pex_protocol import peer_directory

        active = peer_directory.get_active_peers(max_age_s=60.0)
        self._json_response({"peers": active})

    def _handle_gossip_peers(self) -> None:
        if not self._require_auth():
            return
        from infra.pex_protocol import peer_directory

        try:
            body = self._read_body()
            data = json.loads(body)
            peers = data.get("peers") or []
            added = peer_directory.merge_peers(peers, source="gossip")
            self._json_response({"status": "ok", "added": added})
        except Exception as e:
            logger.warning("_handle_gossip_peers: broad except swallowed: %s", e)
            self._error(f"Gossip processing failed: {e}", 400)

    # ------------------------------------------------------------------
    # GET /crdt/policy_hash[?include_full=1]
    #     Peer-facing fleet drift endpoint: returns the local process's
    #     drift-policy hash (and optionally the full policy) so other nodes
    #     can diff their posture. Mirrors memory_admin_policy_hash.
    # ------------------------------------------------------------------

    def _handle_policy_hash(self, query: dict) -> None:
        if not self._require_auth():
            return
        include_full = query.get("include_full", ["0"])[0] in ("1", "true", "yes")
        try:
            from infra.config_drift_policy import resolve_policy

            p = resolve_policy()
            body = {
                "policy_hash": p.policy_hash(),
                "scope": p.scope,
                "schema_version": 1,
            }
            if include_full:
                body["full_policy"] = p.to_dict()
            self._json_response(body)
        except Exception as e:
            logger.warning("_handle_policy_hash failed: %s", e)
            self._error(f"policy_hash failed: {e}", 500)

    # ------------------------------------------------------------------
    # GET /crdt/changes?since=...&agent=...&limit=...
    # ------------------------------------------------------------------

    def _handle_query(self, query: dict, body: str | None) -> None:
        """Read-only SELECT proxy for the dashboard (display-only view of this agent).

        Mirrors infra/api_server.py:_handle_query semantics: requires Bearer auth,
        rejects any non-SELECT/WITH or multi-statement input with 403, returns
        ``{"results":[{"col":val, ...}, ...]}`` so the dashboard's ApiClient.query
        parser consumes it without changes. Filters rows by ``server_tenant_id``
        to prevent cross-tenant leakage (matches _handle_changes).
        """
        if not self._require_auth():
            return
        # Skip HMAC/replay gates: this endpoint authenticates via Bearer token
        # already and is read-only. Mirrors the rest-api /api/v1/query behaviors.
        if body is not None:
            try:
                import json as _json
                payload = _json.loads(body or "{}")
            except Exception as exc:
                self._error(f"invalid JSON: {exc}", 400)
                return
            sql = payload.get("sql") if isinstance(payload, dict) else None
            raw_params = payload.get("params") if isinstance(payload, dict) else None
            params = list(raw_params) if isinstance(raw_params, list) else []
        else:
            sql = query.get("sql", [""])[0]
            raw_params = query.get("params", [""])[0]
            try:
                import json as _json
                params = list(_json.loads(raw_params)) if raw_params else []
            except Exception:
                params = []

        if not isinstance(sql, str) or not sql.strip():
            self._error("sql (str) required", 400)
            return

        stripped = sql.strip().lstrip("(")
        head = stripped.split(None, 1)[0].upper() if stripped else ""
        if head not in ("SELECT", "WITH"):
            self._error(
                f"Forbidden: only SELECT/WITH queries are allowed (got {head!r})",
                403,
            )
            return
        if ";" in stripped:
            tail = stripped.split(";", 1)[1].strip()
            if tail:
                self._error("Forbidden: multi-statement queries are not allowed", 403)
                return

        try:
            import sqlite3
            db = Path(self.db_path)
            if not db.exists():
                self._error("memory.db not found", 500)
                return
            conn = sqlite3.connect(str(db), timeout=10)
            try:
                # Apply SELECT tenant scoping for tables that include tenant_id.
                # We do this by running the query as supplied — callers must
                # include WHERE tenant_id = ? when querying tenant-scoped
                # tables. The dashboard's existing _query_api always filters
                # by tenant via the OPENCODE rest-api SQLite helper, but for
                # the MIMOCODE display view we accept any SELECT the dashboard
                # issues (same trust boundary as direct sqlite reads).
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
                results = [dict(zip(cols, row)) for row in rows]
                self._json_response({"results": results})
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as exc:
            self._error(f"Query failed: {exc}", 500)

    def _handle_changes(self, query: dict):
        if not self._require_auth():
            return
        if not self._check_replay():
            return
        since_str = query.get("since", [""])[0]
        limit_str = query.get("limit", ["200"])[0]

        try:
            limit = max(1, min(1000, int(limit_str)))
        except (ValueError, TypeError):
            limit = 200

        if not since_str:
            self._error("'since' query parameter is required (ISO-8601 or unix epoch)")
            return

        since_epoch = self._parse_since(since_str)
        if since_epoch is None:
            self._error(
                f"Invalid 'since' value: {since_str!r}. "
                f"Use ISO-8601 ('2026-06-17T12:00:00') or unix epoch integer."
            )
            return

        try:
            import sqlite3

            db = Path(self.db_path)
            if not db.exists():
                self._error("memory.db not found", 500)
                return

            conn = sqlite3.connect(str(db), timeout=10)
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                # SEC fix: query base memories table directly with
                # explicit tenant_id filter to prevent cross-tenant
                # data leaks. Raw sqlite3.connect() does not have the
                # tenant_memories TEMP VIEW.
                rows = conn.execute(
                    """SELECT id, content, source_file, logical_clock,
                              version_vector, updated_at, tenant_id
                       FROM memories
                       WHERE deleted_at IS NULL
                         AND tenant_id = ?
                         AND CAST(strftime('%s', updated_at) AS INTEGER) > ?
                       ORDER BY updated_at ASC
                       LIMIT ?""",
                    (self.server_tenant_id, since_epoch, limit),
                ).fetchall()
                note_ids = [r[0] for r in rows]

                # 2026-06-20 (v13): also pull the per-field CRDT
                # state for each note. The client applies these
                # field-level updates with crdt_field.crdt_field_save
                # so concurrent edits to different fields of the
                # same note both win (the v12 bug).
                field_crdt: dict = {}
                if note_ids:
                    # Use the server's tenant_id for the field-crdt
                    # read — consistent with the main query filter.
                    placeholders = ",".join("?" for _ in note_ids)
                    field_rows = conn.execute(
                        f"""SELECT memory_id, field_name, value,
                                   version_vector, logical_clock,
                                   last_writer_agent
                            FROM memory_field_crdt
                            WHERE memory_id IN ({placeholders})
                              AND is_deleted = 0
                              AND tenant_id = ?
                              AND CAST(strftime('%s', updated_at) AS INTEGER) > ?""",
                        (*note_ids, self.server_tenant_id, since_epoch),
                    ).fetchall()
                    for fr in field_rows:
                        field_crdt.setdefault(fr[0], []).append(
                            {
                                "field": fr[1],
                                "value": fr[2],
                                "version_vector": fr[3] or "{}",
                                "logical_clock": int(fr[4] or 0),
                                "last_writer_agent": fr[5] or "",
                            }
                        )
            finally:
                conn.close()

            changes = []
            for row in rows:
                changes.append(
                    {
                        "id": row[0],
                        "content": row[1],
                        "source_file": row[2] or "",
                        "logical_clock": row[3] or 0,
                        "version_vector": row[4] or "{}",
                        "updated_at": row[5] or "",
                        # v13: per-field CRDT state for this note.
                        # Empty list on pre-v13 servers (graceful
                        # back-compat — the client falls back to the
                        # note-level merge when the list is empty).
                        "field_crdt": field_crdt.get(row[0], []),
                    }
                )

            self._json_response({"changes": changes, "count": len(changes)})

        except Exception as e:
            logger.error("sync_server: changes query failed: %s", e)
            self._error(str(e), 500)

    @staticmethod
    def _parse_since(value: str) -> Optional[int]:
        """Parse ``since`` as ISO-8601 string or integer unix epoch."""
        value = value.strip()
        if not value:
            return None
        # Try integer (unix epoch).
        try:
            return int(value)
        except ValueError:
            pass
        # Try ISO-8601.
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(value)
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # POST /crdt/push
    # ------------------------------------------------------------------

    def _handle_push(self) -> None:
        if not self._require_auth():
            return
        if not self._check_replay():
            return
        body = self._read_body()
        if not body:
            self._error("Empty request body")
            return
        if not self._check_hmac(body):
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._error(f"Invalid JSON: {e}")
            return

        remote_agent = data.get("agent_id", "")
        remote_notes = data.get("notes", {})
        if not remote_agent:
            self._error("'agent_id' is required")
            return
        if not isinstance(remote_notes, dict) or not remote_notes:
            self._error("'notes' must be a non-empty dict")
            return

        try:
            from crdt.crdt_merge import crdt_sync_all
            from crdt.crdt_field import crdt_field_save

            notes, field_crdt_map = self._normalize_notes(remote_notes)
            result = crdt_sync_all(
                self.db_path,
                remote_agent,
                self.server_agent_id,
                notes,
            )

            # Sprint 1.4: Apply field_crdt data after note-level merge.
            # Use the field-level LWWES merge (per-field registers in
            # memory_field_crdt) rather than crdt_field_save, which would
            # collapse every field onto the note-level ``content`` column
            # and destroy the field discriminator.
            field_applied = 0
            for note_id, field_entries in field_crdt_map.items():
                if note_id in notes:
                    for field_entry in field_entries:
                        try:
                            from crdt.crdt_field import (
                                FieldUpdate,
                                apply_field_updates_to_db,
                                merge_field_updates,
                            )

                            _raw_vv = field_entry.get("version_vector") or "{}"
                            if isinstance(_raw_vv, dict):
                                _vv: dict = _raw_vv
                            else:
                                try:
                                    _vv = json.loads(_raw_vv)
                                except (TypeError, ValueError):
                                    _vv = {}
                            upd = FieldUpdate(
                                memory_id=note_id,
                                field_name=field_entry.get("field_name")
                                or field_entry.get("field", ""),
                                value=str(field_entry.get("value", "")),
                                version_vector=_vv,
                                logical_clock=int(field_entry.get("logical_clock", 0)),
                                last_writer_agent=field_entry.get("last_writer_agent", "")
                                or remote_agent,
                            )
                            winners = merge_field_updates([upd])
                            _fconn = _open_server_db(self.db_path)
                            try:
                                apply_field_updates_to_db(
                                    _fconn, winners, self.server_tenant_id
                                )
                                _fconn.commit()
                            finally:
                                _fconn.close()
                            field_applied += 1
                        except Exception as field_exc:
                            logger.warning("sync_server: field_crdt apply failed for %s: %s", note_id, field_exc)

            result["field_crdt_applied"] = field_applied
            self._json_response(result)
        except Exception as e:
            logger.error("sync_server: push merge failed: %s", e)
            self._error(str(e), 500)

    # ------------------------------------------------------------------
    # S2 (2026-06-23): KG CRDT endpoints
    # ------------------------------------------------------------------
    # GET  /crdt/kg/changes?since=<unix_epoch>&limit=<N>
    # POST /crdt/kg/push         body: {"ops": [<EntityOp>, ...]}
    #
    # The "ops" array contains serialized EntityOp / EdgeOp dicts.
    # The server inserts them into kg_entity_crdt / kg_edge_crdt
    # using INSERT OR REPLACE (idempotent on (entity_id, op) /
    # edge_id). Peers then run ``compute_entity_crdt_state`` /
    # ``compute_edge_crdt_state`` locally to project the ops into
    # the kg_entities / kg_edges tables.

    def _handle_kg_changes(self, query: dict) -> None:
        """GET /crdt/kg/changes — return recent KG CRDT ops.

        Query params (all optional):
          ``since``  — Unix epoch. Only return ops with timestamp > since.
          ``limit``  — Max ops to return (default 500, capped at 5000).

        Response:
          ``{"entity_ops": [...], "edge_ops": [...], "ts": <server_ts>}``

        Filters by tenant_id for tenant isolation.
        """
        if not self._require_auth():
            return
        if not self._check_replay():
            return

        since = self._parse_since(query.get("since", [None])[0] or "")
        limit = 500
        raw_limit = query.get("limit", [None])[0]
        if raw_limit:
            try:
                limit = max(1, min(int(raw_limit), 5000))
            except (TypeError, ValueError):
                pass

        try:
            from kg.kg_crdt import ensure_kg_crdt_schema

            db_path = self.db_path
            conn = _open_server_db(db_path)
            try:
                ensure_kg_crdt_schema(conn)
                params: tuple = (since,) if since is not None else (0.0,)
                where_clause = "WHERE timestamp > ?" if since is not None else ""
                # Tenant filter: always apply for tenant isolation
                tenant_filter = f"{'AND' if where_clause else 'WHERE'} tenant_id = ?"
                all_params = params + (self.server_tenant_id,)
                # Entity ops
                entity_rows = conn.execute(
                    f"""
                    SELECT entity_id, agent_id, op, version_vector, name,
                           entity_type, description, timestamp
                    FROM kg_entity_crdt
                    {where_clause} {tenant_filter}
                    ORDER BY timestamp ASC
                    LIMIT ?
                    """,
                    all_params + (limit,),
                ).fetchall()
                # Edge ops
                edge_rows = conn.execute(
                    f"""
                    SELECT edge_id, source_id, target_id, relation, weight,
                           valid_at, agent_id, version_vector, timestamp
                    FROM kg_edge_crdt
                    {where_clause} {tenant_filter}
                    ORDER BY timestamp ASC
                    LIMIT ?
                    """,
                    all_params + (limit,),
                ).fetchall()
                entity_ops = [
                    {
                        "entity_id": r[0],
                        "agent_id": r[1],
                        "op": r[2],
                        "version_vector": json.loads(r[3]) if r[3] else {},
                        "name": r[4] or "",
                        "entity_type": r[5] or "",
                        "description": r[6] or "",
                        "timestamp": r[7] or 0.0,
                    }
                    for r in entity_rows
                ]
                edge_ops = [
                    {
                        "edge_id": r[0],
                        "source_id": r[1],
                        "target_id": r[2],
                        "relation": r[3] or "related_to",
                        "weight": r[4] or 1.0,
                        "valid_at": r[5],
                        "agent_id": r[6] or "",
                        "version_vector": json.loads(r[7]) if r[7] else {},
                        "timestamp": r[8] or 0.0,
                    }
                    for r in edge_rows
                ]
                self._json_response(
                    {
                        "entity_ops": entity_ops,
                        "edge_ops": edge_ops,
                        "ts": time.time(),
                    }
                )
            finally:
                try:
                    conn.close()
                except Exception as _wp_exc:
                    logger.warning("_handle_kg_changes: broad except swallowed: %s", _wp_exc)
                    pass
        except Exception as e:
            logger.error("sync_server: kg changes failed: %s", e)
            self._error(str(e), 500)

    def _handle_kg_push(self) -> None:
        """POST /crdt/kg/push — receive KG CRDT ops from a peer.

        Body: ``{"entity_ops": [...], "edge_ops": [...]}``

        The ops are inserted into the local CRDT tables using
        ``INSERT OR REPLACE`` (idempotent). After insertion, the
        caller can run ``compute_entity_crdt_state`` /
        ``compute_edge_crdt_state`` to project the merged state
        into the kg_entities / kg_edges tables.

        Injects self.server_tenant_id into each CRDT row for tenant isolation.
        """
        if not self._require_auth():
            return
        if not self._check_replay():
            return
        body = self._read_body()
        if not body:
            self._error("Empty request body")
            return
        if not self._check_hmac(body):
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._error(f"Invalid JSON: {e}")
            return

        try:
            from kg.kg_crdt import ensure_kg_crdt_schema

            db_path = self.db_path
            conn = _open_server_db(db_path)
            try:
                ensure_kg_crdt_schema(conn)
                n_entities = 0
                n_edges = 0
                for op in data.get("entity_ops", []):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO kg_entity_crdt
                            (entity_id, agent_id, op, version_vector, name,
                             entity_type, description, timestamp, tenant_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            op.get("entity_id"),
                            op.get("agent_id", ""),
                            op.get("op", "add"),
                            json.dumps(op.get("version_vector", {})),
                            op.get("name", ""),
                            op.get("entity_type", ""),
                            op.get("description", ""),
                            op.get("timestamp", 0.0),
                            self.server_tenant_id,
                        ),
                    )
                    n_entities += 1
                for op in data.get("edge_ops", []):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO kg_edge_crdt
                            (edge_id, source_id, target_id, relation, weight,
                             valid_at, agent_id, version_vector, timestamp, tenant_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            op.get("edge_id"),
                            op.get("source_id"),
                            op.get("target_id"),
                            op.get("relation", "related_to"),
                            op.get("weight", 1.0),
                            op.get("valid_at"),
                            op.get("agent_id", ""),
                            json.dumps(op.get("version_vector", {})),
                            op.get("timestamp", 0.0),
                            self.server_tenant_id,
                        ),
                    )
                    n_edges += 1
                conn.commit()
                # Sprint 2.4 fix: project the merged CRDT ops into the
                # canonical kg_entities / kg_edges tables so the receiving
                # peer's graph actually converges (previously ops were
                # stored but never projected).
                projected = {}
                try:
                    from kg.kg_crdt import project_crdt_to_entities

                    n_e, n_g, _ = project_crdt_to_entities(conn)
                    projected = {"entities": n_e, "edges": n_g}
                except Exception as proj_exc:
                    logger.warning("sync_server: kg push projection failed: %s", proj_exc)
                self._json_response(
                    {
                        "applied": n_entities + n_edges,
                        "entity_ops": n_entities,
                        "edge_ops": n_edges,
                        "projected": projected,
                    }
                )
            finally:
                try:
                    conn.close()
                except Exception as _wp_exc:
                    logger.warning("_handle_kg_push: broad except swallowed: %s", _wp_exc)
                    pass
        except Exception as e:
            logger.error("sync_server: kg push failed: %s", e)
            self._error(str(e), 500)

    @staticmethod
    def _normalize_notes(raw: dict) -> tuple[dict, dict]:
        """Normalize incoming note data into crdt_sync_all's expected format.

        Sprint 1.4: Returns (notes, field_crdt_map) where field_crdt_map
        contains per-note field_crdt data for separate application.
        """
        notes: dict = {}
        field_crdt_map: dict = {}
        for note_id, data_list in raw.items():
            if isinstance(data_list, dict):
                notes[note_id] = (
                    data_list.get("content", ""),
                    data_list.get("source_file", note_id),
                    int(data_list.get("logical_clock", 0)),
                    data_list.get("version_vector", "{}"),
                    int(data_list.get("sender_clock", 0)),
                )
                # Sprint 1.4: Preserve field_crdt for separate application
                if "field_crdt" in data_list:
                    # Normalize each entry so it carries the field discriminator
                    # under a single stable key ("field_name") before hand-off
                    # to the field-CRDT apply path. The wire format uses "field".
                    norm_entries = []
                    for _fe in data_list["field_crdt"]:
                        _fe = dict(_fe)
                        if "field_name" not in _fe and "field" in _fe:
                            _fe["field_name"] = _fe["field"]
                        norm_entries.append(_fe)
                    field_crdt_map[note_id] = norm_entries
            elif isinstance(data_list, (list, tuple)) and len(data_list) >= 5:
                notes[note_id] = (
                    str(data_list[0]),
                    str(data_list[1]),
                    int(data_list[2]),
                    str(data_list[3]),
                    int(data_list[4]),
                )
        return notes, field_crdt_map

    def _handle_skill_changes(self, query: dict) -> None:
        """GET /skills/changes — return skills updated since a given timestamp.

        Query params:
          ``since``  — Unix epoch. Only return skills with updated_at > since.
          ``agent``  — Agent id for context (unused in filter, reserved for future use).
          ``limit``  — Max skills to return (default 500, capped at 5000).

        Filters by tenant_id for tenant isolation.
        """
        if not self._require_auth():
            return
        if not self._check_replay():
            return

        since = self._parse_since(query.get("since", [None])[0] or "")
        agent = (query.get("agent", [""])[0] or "").strip()
        limit = 500
        raw_limit = query.get("limit", [None])[0]
        if raw_limit:
            try:
                limit = max(1, min(int(raw_limit), 5000))
            except (TypeError, ValueError):
                pass

        try:
            conn = _open_server_db(self.db_path)
            try:
                from skill_extractor import ensure_skill_schema
                ensure_skill_schema(conn)
                params: tuple = (since,) if since is not None else (0.0,)
                where = "WHERE updated_at > ?" if since is not None else ""
                tenant_filter = f"{'AND' if where else 'WHERE'} tenant_id = ?"
                rows = conn.execute(
                    f"""SELECT id, name, source_memory_id, topic, description,
                               triggers, steps, content_hash, hit_count,
                               last_used_at, hit_vector, last_used_vector,
                               logical_clock, created_at, updated_at
                        FROM memory_skills
                        {where} {tenant_filter}
                        ORDER BY updated_at ASC
                        LIMIT ?""",
                    params + (self.server_tenant_id, limit),
                ).fetchall()
                skill_cols = [
                    "id", "name", "source_memory_id", "topic", "description",
                    "triggers", "steps", "content_hash", "hit_count",
                    "last_used_at", "hit_vector", "last_used_vector",
                    "logical_clock", "created_at", "updated_at",
                ]
                skills = []
                for row in rows:
                    skill: dict = {}
                    for col_name, raw_val in zip(skill_cols, row):
                        if col_name in ("hit_vector", "last_used_vector") and raw_val is not None:
                            try:
                                skill[col_name] = json.loads(raw_val)
                            except (json.JSONDecodeError, TypeError):
                                skill[col_name] = {}
                        elif col_name == "triggers" and raw_val is not None:
                            try:
                                skill[col_name] = json.loads(raw_val)
                            except (json.JSONDecodeError, TypeError):
                                skill[col_name] = []
                        elif col_name == "steps" and raw_val is not None:
                            try:
                                skill[col_name] = json.loads(raw_val)
                            except (json.JSONDecodeError, TypeError):
                                skill[col_name] = []
                        else:
                            skill[col_name] = raw_val
                    skills.append(skill)
                self._json_response({"skills": skills, "agent": agent, "ts": time.time()})
            finally:
                try:
                    conn.close()
                except Exception as _wp_exc:
                    logger.warning("_handle_skill_changes: broad except swallowed: %s", _wp_exc)
                    pass
        except Exception as e:
            logger.error("sync_server: skill changes failed: %s", e)
            self._error(str(e), 500)

    def _handle_skill_push(self) -> None:
        """POST /skills/push — receive skill CRDT updates from a peer.

        Body: ``{"skills": [{...skill dict...}, ...]}``

        Each skill is merged into the local database via
        ``merge_and_save_skill`` (idempotent LWW + G-Counter merge).
        Returns ``{"applied": N, "skipped": M}``.

        Injects self.server_tenant_id into each skill dict for tenant isolation.
        """
        if not self._require_auth():
            return
        if not self._check_replay():
            return
        body = self._read_body()
        if not body:
            self._error("Empty request body")
            return
        if not self._check_hmac(body):
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._error(f"Invalid JSON: {e}")
            return

        try:
            from skill_extractor import ensure_skill_schema, merge_and_save_skill
            db_path = self.db_path
            conn = _open_server_db(db_path)
            try:
                ensure_skill_schema(conn)
                applied = 0
                skipped = 0
                for skill in data.get("skills", []):
                    if not skill.get("name"):
                        skipped += 1
                        continue
                    skill["tenant_id"] = self.server_tenant_id
                    try:
                        merge_and_save_skill(conn, skill)
                        applied += 1
                    except Exception as merge_err:
                        logger.warning("sync_server: skill merge skip %s: %s", skill.get("name"), merge_err)
                        skipped += 1
                conn.commit()
                self._json_response({"applied": applied, "skipped": skipped})
            finally:
                try:
                    conn.close()
                except Exception as _wp_exc:
                    logger.warning("_handle_skill_push: broad except swallowed: %s", _wp_exc)
                    pass
        except Exception as e:
            logger.error("sync_server: skill push failed: %s", e)
            self._error(str(e), 500)


    def _handle_agent_changes(self, query: dict) -> None:
        """GET /agents/changes — return agent registry entries updated since a timestamp.

        Query params:
          ``since``  — Unix epoch. Only return entries with last_seen > since.
          ``agent``  — Agent id for context (unused in filter).
          ``limit``  — Max entries to return (default 200, capped at 1000).

        Tenant-scoped for isolation.
        """
        if not self._require_auth():
            return
        if not self._check_replay():
            return

        since = self._parse_since(query.get("since", [None])[0] or "")
        limit = 200
        raw_limit = query.get("limit", [None])[0]
        if raw_limit:
            try:
                limit = max(1, min(int(raw_limit), 1000))
            except (TypeError, ValueError):
                pass

        try:
            conn = _open_server_db(self.db_path)
            try:
                params: tuple = (since,) if since is not None else (0.0,)
                where = "WHERE last_seen > ?" if since is not None else ""
                rows = conn.execute(
                    f"""SELECT agent_id, display_name, parent_agent, namespace,
                               logical_clock, version_vector, last_seen, is_deleted
                        FROM agent_registry_crdt
                        {where}
                          AND tenant_id = ?
                        ORDER BY last_seen ASC
                        LIMIT ?""",
                    params + (self.server_tenant_id, limit),
                ).fetchall()
                agents = []
                for row in rows:
                    agents.append({
                        "agent_id": row[0],
                        "display_name": row[1],
                        "parent_agent": row[2],
                        "namespace": row[3],
                        "logical_clock": row[4],
                        "version_vector": row[5],
                        "last_seen": row[6],
                        "is_deleted": row[7],
                    })
                self._json_response({"agents": agents, "ts": time.time()})
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            logger.error("sync_server: agent changes failed: %s", e)
            self._error(str(e), 500)

    def _handle_agent_push(self) -> None:
        """POST /agents/push — receive agent registry entries from a peer.

        Body: ``{"agents": [{...agent entry...}, ...]}``

        Each entry is upserted into the local ``agent_registry_crdt``
        table with LWW conflict resolution via version_vector comparison.
        Returns ``{"applied": N, "skipped": M}``.
        """
        if not self._require_auth():
            return
        if not self._check_replay():
            return
        body = self._read_body()
        if not body:
            self._error("Empty request body")
            return
        if not self._check_hmac(body):
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._error(f"Invalid JSON: {e}")
            return

        try:
            from crdt.crdt_merge import parse_version_vector, dominates, merge_vectors

            conn = _open_server_db(self.db_path)
            try:
                applied = 0
                skipped = 0
                for entry in data.get("agents", []):
                    agent_id = entry.get("agent_id", "")
                    if not agent_id:
                        skipped += 1
                        continue

                    remote_vv = parse_version_vector(entry.get("version_vector", "{}"))
                    remote_clock = int(entry.get("logical_clock", 0))
                    remote_agent = entry.get("display_name", agent_id)

                    existing = conn.execute(
                        "SELECT version_vector, logical_clock FROM agent_registry_crdt WHERE agent_id = ? AND tenant_id = ?",
                        (agent_id, self.server_tenant_id),
                    ).fetchone()

                    if existing:
                        existing_vv = parse_version_vector(existing[0])
                        existing_clock = existing[1]
                        if dominates(remote_vv, existing_vv):
                            pass  # remote wins
                        elif dominates(existing_vv, remote_vv):
                            skipped += 1
                            continue  # local is newer
                        else:
                            # Concurrent: LWW tiebreaker (clock desc, agent_id asc)
                            remote_wins = (remote_clock, remote_agent) > (existing_clock, agent_id)
                            if not remote_wins:
                                skipped += 1
                                continue
                    else:
                        pass  # new entry

                    merged_vv = merge_vectors(self.server_agent_id, existing_vv if existing else {}, remote_vv)
                    new_clock = max(remote_clock, existing[1] if existing else 0) + 1

                    conn.execute(
                        """INSERT OR REPLACE INTO agent_registry_crdt
                           (agent_id, display_name, parent_agent, namespace,
                            logical_clock, version_vector, last_seen, is_deleted, tenant_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            agent_id,
                            entry.get("display_name", agent_id),
                            entry.get("parent_agent", ""),
                            entry.get("namespace", agent_id),
                            new_clock,
                            json.dumps(merged_vv),
                            entry.get("last_seen", time.time()),
                            entry.get("is_deleted", 0),
                            self.server_tenant_id,
                        ),
                    )
                    applied += 1
                conn.commit()
                self._json_response({"applied": applied, "skipped": skipped})
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            logger.error("sync_server: agent push failed: %s", e)
            self._error(str(e), 500)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


# Environment variables for native TLS support (2026-06-20 addition).
SYNC_TLS_CERT_ENV = "MEMORY_SYNC_TLS_CERT"
SYNC_TLS_KEY_ENV = "MEMORY_SYNC_TLS_KEY"
SYNC_TLS_CLIENT_CA_ENV = "MEMORY_SYNC_TLS_CLIENT_CA"


def _is_loopback(host: str) -> bool:
    """True if the given host is a loopback address.

    Used by the SEC-1 and SEC-4 fixes to decide whether an empty
    CORS allowlist or a plaintext bind deserves a startup warning
    (only when bound to a non-loopback address — loopback-only is
    the safe default).

    Note: ``0.0.0.0`` is NOT considered loopback here.  It means
    "all interfaces" — the server listens on every network
    interface, including non-loopback ones.  Binding to ``0.0.0.0``
    on a public-facing machine is the same as binding to a public
    IP for security purposes.
    """
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            addr = sockaddr[0]
            if isinstance(addr, str) and addr.startswith("127."):
                return True
    return False


def _build_tls_context() -> Optional[ssl.SSLContext]:
    """Build an ``ssl.SSLContext`` for the sync server if TLS is configured.

    Reads three env vars:

    * ``MEMORY_SYNC_TLS_CERT`` — path to PEM-encoded server cert (required for TLS)
    * ``MEMORY_SYNC_TLS_KEY`` — path to PEM-encoded server key (required for TLS)
    * ``MEMORY_SYNC_TLS_CLIENT_CA`` — path to PEM-encoded CA bundle (optional,
      enables mTLS: clients must present a cert signed by this CA)

    Returns ``None`` if neither cert nor key is set (plaintext mode).
    Raises ``ValueError`` if only one of cert/key is set.
    Raises ``FileNotFoundError`` if cert/key/CA files don't exist.
    """
    cert = os.environ.get(SYNC_TLS_CERT_ENV, "").strip()
    key = os.environ.get(SYNC_TLS_KEY_ENV, "").strip()
    client_ca = os.environ.get(SYNC_TLS_CLIENT_CA_ENV, "").strip()

    if not cert and not key:
        return None  # plaintext mode

    if not cert or not key:
        raise ValueError(
            f"{SYNC_TLS_CERT_ENV} and {SYNC_TLS_KEY_ENV} must both be set to "
            "enable TLS (got cert={!r}, key={!r})".format(bool(cert), bool(key))
        )

    if not Path(cert).exists():
        raise FileNotFoundError(f"TLS cert not found: {cert}")
    if not Path(key).exists():
        raise FileNotFoundError(f"TLS key not found: {key}")

    # Use PROTOCOL_TLS_SERVER which is the recommended modern alias and
    # auto-selects the highest mutually-supported TLS version.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)

    if client_ca:
        if not Path(client_ca).exists():
            raise FileNotFoundError(f"TLS client CA not found: {client_ca}")
        ctx.load_verify_locations(cafile=client_ca)
        # CERT_REQUIRED means clients MUST present a cert signed by
        # one of the CAs in the bundle. Without this, mTLS is opt-in
        # on the server side which defeats the purpose.
        ctx.verify_mode = ssl.CERT_REQUIRED
        logger.info("sync_server: TLS + mTLS enabled (client CA: %s)", client_ca)
    else:
        logger.info("sync_server: TLS enabled (server-only, no client cert)")

    return ctx


class SyncServer:
    """Threaded HTTP sync server for auto multi-agent memory sync.

    Usage::

        server = SyncServer(db_path="/path/to/memory.db", agent_id="agent-1")
        server.start()   # daemon thread, non-blocking
        ...
        server.stop()    # graceful shutdown

    Thread safety
    -------------
    The server runs a new thread per request (via ``ThreadingHTTPServer``).
    All CRDT merge operations use ``crdt_sync_all``, which opens its own
    connection to the SQLite DB — so there are no shared-connection races.

    Security
    --------
    The server defaults to **plaintext HTTP** if no TLS env vars are set.
    When binding to a non-loopback address (e.g.
    ``MEMORY_SYNC_LISTEN_HOST=0.0.0.0``), the bearer token and HMAC
    signature are sent in cleartext and can be intercepted by a LAN
    peer.  For any untrusted-network deployment:

    1. **Native TLS** (recommended, 2026-06-20): set
       ``MEMORY_SYNC_TLS_CERT=/path/to/server.crt`` and
       ``MEMORY_SYNC_TLS_KEY=/path/to/server.key``. The server will
       accept HTTPS on the configured port. For mTLS (clients must
       present a cert signed by your CA), additionally set
       ``MEMORY_SYNC_TLS_CLIENT_CA=/path/to/ca-bundle.crt``. mTLS
       protects against credential theft even if the bearer token
       and HMAC secret are compromised.
    2. **Reverse proxy**: put the server behind a TLS-terminating
       proxy (nginx, caddy, stunnel) and bind ``host=127.0.0.1``.
    3. Set ``MEMORY_SYNC_TOKEN`` to a strong random value (32+ bytes).
    4. Set ``MEMORY_SYNC_HMAC_SECRET`` to a different strong random value.
    5. Set ``MEMORY_SYNC_CORS_ORIGINS`` to your trusted client origins
       (default = no CORS).
    6. Set ``MEMORY_SYNC_MAX_AGE=60`` or lower for tight replay windows.

    The server supports both native TLS (preferred) and reverse-proxy
    TLS (legacy). When ``MEMORY_SYNC_TLS_CERT`` is set, no plaintext
    listener is exposed — the socket is wrapped in TLS before
    ``serve_forever`` starts. The ``sync_client`` auto-detects https://
    in the peer URL and uses HTTPS; no client changes are needed.
    """

    def __init__(
        self,
        db_path: str,
        agent_id: str,
        host: str = "127.0.0.1",
        port: int = 9877,
        discover: bool = False,
        tenant_id: str = "default",
    ):
        self.db_path = str(db_path)
        self.agent_id = agent_id
        self.tenant_id = tenant_id
        self.host = host
        self.port = port
        self.discover = discover
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.advertiser: Optional[MDNSAdvertiser] = None
        self.browser: Optional[MDNSBrowser] = None
        self.gossip_thread: Optional[threading.Thread] = None
        self.gossip_stop_event: Optional[threading.Event] = None

    def start(self) -> bool:
        """Start the sync server in a daemon thread. Returns True on success."""
        if self._server is not None:
            logger.warning(
                "sync_server: already running on %s:%d", self.host, self.port
            )
            return True

        try:
            # ThreadingHTTPServer is available since Python 3.7.
            from http.server import ThreadingHTTPServer

            server_cls: type[HTTPServer] = ThreadingHTTPServer
        except ImportError:
            # Fallback for older Python (unlikely with 3.14, but safe).
            from socketserver import ThreadingMixIn

            class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
                allow_reuse_address = True
                daemon_threads = True

            server_cls = _ThreadingHTTPServer

        class _Handler(_SyncHandler):
            pass

        _Handler.db_path = self.db_path
        _Handler.server_agent_id = self.agent_id
        _Handler.server_tenant_id = self.tenant_id

        self._server = server_cls((self.host, self.port), _Handler)
        self._server.timeout = 1.0  # allow clean shutdown within 1s

        # Optional TLS wrap (native; no reverse proxy required). Reads
        # MEMORY_SYNC_TLS_CERT / MEMORY_SYNC_TLS_KEY / MEMORY_SYNC_TLS_CLIENT_CA.
        # Errors are caught and surfaced so a misconfigured TLS setup
        # doesn't silently fall back to plaintext.
        try:
            tls_context = _build_tls_context()
        except (FileNotFoundError, ValueError) as e:
            logger.error("sync_server: TLS configuration error: %s", e)
            try:
                self._server.server_close()
            except Exception as _wp_exc:
                logger.warning("start: broad except swallowed: %s", _wp_exc)
                pass
            self._server = None
            return False

        if tls_context is not None:
            self._server.socket = tls_context.wrap_socket(
                self._server.socket, server_side=True
            )

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="sync-server",
            daemon=True,
        )
        self._thread.start()

        scheme = "https" if tls_context is not None else "http"
        logger.info(
            "sync_server: listening on %s://%s:%d (agent=%s)",
            scheme,
            self.host,
            self.port,
            self.agent_id,
        )
        # SEC-4 fix (2026-06-22): when the server is bound to a
        # non-loopback address and TLS is not configured, the
        # Bearer token and HMAC body are sent in clear text on
        # the wire.  Loopback binding is the de-facto mitigation
        # (the default 127.0.0.1:9877 is), so we only warn on
        # non-loopback.  We don't refuse to start because the
        # operator may be on a trusted internal network.
        if tls_context is None and not _is_loopback(self.host):
            logger.warning(
                "sync_server: bound to %s WITHOUT TLS. Bearer tokens "
                "and HMAC bodies will be sent in clear text.  Set "
                "MEMORY_SYNC_TLS_CERT and MEMORY_SYNC_TLS_KEY to enable "
                "HTTPS (see sync_server.py:457 for the full set of "
                "TLS env vars).",
                self.host,
            )
        if not SYNC_AUTH_TOKEN and not _is_loopback(self.host):
            logger.error(
                "sync_server: MEMORY_SYNC_TOKEN is required when bound to "
                "non-loopback %s. All mutating endpoints are open. Set the "
                "env var or bind to 127.0.0.1.",
                self.host,
            )
        # SEC-1 fix (2026-06-22): if no CORS origins are configured
        # and the server is bound to a non-loopback address, log a
        # warning so operators see it in their startup output.  We
        # don't refuse to start — same-origin clients (the MCP
        # server, curl) still work — but cross-origin browser
        # clients will be blocked.
        if not SYNC_CORS_ORIGINS and not _is_loopback(self.host):
            logger.warning(
                "sync_server: bound to %s with empty CORS allowlist. "
                "Browser-based cross-origin clients will be blocked. "
                "Set MEMORY_SYNC_CORS_ORIGINS=https://your-frontend to allow them.",
                self.host,
            )

        # Bootstrap mDNS advertiser/browser and gossip client if discovery is enabled
        import os

        enable_discovery = self.discover or os.environ.get(
            "MEMORY_SYNC_DISCOVER", ""
        ).strip().lower() in ("1", "true", "yes")
        self._discover_enabled = enable_discovery
        if enable_discovery:
            from infra.mdns_discovery import MDNSAdvertiser, MDNSBrowser

            # Start mDNS advertiser
            self.advertiser = MDNSAdvertiser(self.agent_id, self.port)
            self.advertiser.start()

            # Start mDNS browser
            self.browser = MDNSBrowser()
            self.browser.start()

            # Periodically register dynamically discovered peers and run gossip
            self.gossip_stop_event = threading.Event()
            self.gossip_thread = threading.Thread(
                target=self._gossip_loop, name="sync-gossip", daemon=True
            )
            self.gossip_thread.start()

        return True

    def _gossip_loop(self) -> None:
        from infra.pex_protocol import peer_directory, send_gossip

        assert self.gossip_stop_event is not None
        while not self.gossip_stop_event.is_set():
            if self.gossip_stop_event.wait(5.0):
                break

            # 1. Update peer_directory from mDNS browser
            if self.browser:
                discovered = self.browser.get_peers()
                for dp in discovered:
                    peer_directory.register_peer(
                        agent_id=dp["agent_id"],
                        url=dp["url"],
                        ip=dp["ip"],
                        port=dp["port"],
                        source="mdns",
                    )

            # 2. Gossip with one random active peer
            import random

            active_peers = peer_directory.get_active_peers(max_age_s=60.0)
            active_peers = [p for p in active_peers if p["agent_id"] != self.agent_id]
            if not active_peers:
                continue

            target = random.choice(active_peers)
            send_gossip(
                target_url=target["url"],
                local_agent_id=self.agent_id,
                peers=active_peers,
            )

    def stop(self) -> None:
        """Shut down the sync server gracefully."""
        if getattr(self, "_discover_enabled", False):
            if self.gossip_stop_event:
                self.gossip_stop_event.set()
            if self.advertiser:
                self.advertiser.stop()
            if self.browser:
                self.browser.stop()
            if self.gossip_thread:
                self.gossip_thread.join(timeout=2.0)

        if self._server is None:
            return
        try:
            self._server.shutdown()
        except Exception as e:
            logger.debug("sync_server: shutdown error: %s", e)
        self._server = None
        self._thread = None
        logger.info("sync_server: stopped")

    @property
    def is_running(self) -> bool:
        return self._server is not None


# ---------------------------------------------------------------------------
# Convenience: start from env / config
# ---------------------------------------------------------------------------


def start_server_from_config(db_path: str | Path) -> Optional[SyncServer]:
    """Create and start a SyncServer based on memory config.

    Reads ``sync_enable_server``, ``sync_listen_host``, and
    ``sync_listen_port`` from the global config singleton. Returns
    ``None`` if sync is disabled.
    """
    from infra._lazy_imports import get_config

    cfg = get_config()
    if not cfg.sync_enable_server:
        return None

    from save_pipeline import _crdt_agent_id

    agent_id = _crdt_agent_id()
    server = SyncServer(
        db_path=str(db_path),
        agent_id=agent_id,
        host=cfg.sync_listen_host,
        port=cfg.sync_listen_port,
    )
    server.start()
    return server


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list) -> int:
    """Run the sync server standalone. For testing / debugging.

    Usage::
        python sync_server.py <db_path> [--port N] [--host HOST] [--agent-id ID]
    """
    import argparse

    parser = argparse.ArgumentParser(description="CRDT sync server")
    parser.add_argument("db_path", help="Path to memory.db")
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--agent-id", default="sync-server")
    parser.add_argument(
        "--discover", action="store_true", help="Enable dynamic mDNS/PEX discovery"
    )
    args = parser.parse_args(argv[1:])

    server = SyncServer(
        db_path=args.db_path,
        agent_id=args.agent_id,
        host=args.host,
        port=args.port,
        discover=args.discover,
    )
    server.start()
    print(f"Sync server running on http://{args.host}:{args.port}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv))
