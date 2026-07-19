"""Threaded REST and WebSocket server for Agentic Memory System.

Provides zero-dependency REST endpoints for MemoryClient operations and a
real-time WebSocket event streaming interface using the database Outbox pattern.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs

from agentic_memory.client import MemoryClient

logger = logging.getLogger(__name__)

# Config variables (from env with fallback to empty/defaults)
API_AUTH_TOKEN = os.environ.get("MEMORY_API_TOKEN", "")
API_CORS_ORIGINS = frozenset(
    o.strip()
    for o in os.environ.get("MEMORY_API_CORS_ORIGINS", "").split(",")
    if o.strip()
)

# ── Known MCP memory fields (used for update validation) ─────────────────
_MEMORY_UPDATE_FIELDS = frozenset({
    "content", "category", "tags", "pinned", "is_global",
    "importance", "title_slug", "tier",
})

def _is_loopback(host: str) -> bool:
    """Check if host resolves to loopback."""
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            sockaddr = info[4]
            if sockaddr:
                addr = sockaddr[0]
                if isinstance(addr, str) and (addr.startswith("127.") or addr == "::1"):
                    return True
    except socket.gaierror:
        pass
    return False


class APIRequestHandler(BaseHTTPRequestHandler):
    """Handles REST HTTP routes and upgrades to WebSockets."""

    server: APIServer

    def log_message(self, format: str, *args) -> None:
        logger.debug("api_server: " + format, *args)

    def _error(self, message: str, status_code: int) -> None:
        self._write_json({"error": message}, status_code)

    def _write_json(self, data: dict | list, status_code: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Flush any queued auth cookies (set after send_response so the status
        # line is emitted first and the response stays well-formed).
        for _ck in getattr(self, "_pending_cookies", []) or []:
            self.send_header("Set-Cookie", _ck)
        self._pending_cookies = []
        # CORS
        origin = self.headers.get("Origin", "")
        if origin and (not API_CORS_ORIGINS or origin in API_CORS_ORIGINS):
            self.send_header("Access-Control-Allow-Origin", origin)
        elif not API_CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    # ── Auth cookie + rate-limit helpers (Phase 2) ──────────────────────────

    _AUTH_COOKIE = "am_token"

    def _set_auth_cookie(self, token: str) -> None:
        """Queue the HttpOnly JWT session cookie used by the dashboard.

        Stored on the handler and emitted by ``_write_json`` (which calls
        ``send_response`` first). Calling ``send_header`` directly here would
        prepend the cookie before the status line and corrupt the response.
        """
        self._pending_cookies = [
            f"{self._AUTH_COOKIE}={token}; HttpOnly; SameSite=Lax; "
            f"Path=/; Max-Age=3600",
        ]

    def _clear_auth_cookie(self) -> None:
        self._pending_cookies = [
            f"{self._AUTH_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0",
        ]

    def _cookie_token(self) -> str:
        """Return the JWT from the session cookie, if present."""
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(f"{self._AUTH_COOKIE}="):
                return part[len(self._AUTH_COOKIE) + 1 :]
        return ""

    def _rate_limited(self, key: str | None = None) -> bool:
        """Sliding-window per-key rate limit. Returns True if caller is limited.

        ``key`` defaults to the client IP when ``None``.  After authentication
        the caller should pass the principal ID so limits are per-user, not
        per-IP.  Limits are best-effort and never raise.  Disabled when the
        limit is <= 0.
        """
        limit = getattr(self.server, "rate_limit", 0)
        if limit <= 0:
            return False
        if key is None:
            key = self.client_address[0]
        window = getattr(self.server, "rate_window", 60)
        now = time.time()
        store = self.server._rate_buckets  # type: ignore[attr-defined]
        lock = self.server._rate_lock  # type: ignore[attr-defined]
        with lock:
            times = store.get(key, [])
            times = [t for t in times if now - t < window]
            if len(times) >= limit:
                store[key] = times
                return True
            times.append(now)
            store[key] = times
        return False

    def _require_auth(self) -> bool:
        """Enforce authentication.

        Phase 2 (SSO/OIDC/SAML): first tries JWT validation via Authlib
        (SSO-issued tokens signed by ``idem_token_key``). On success the
        principal is resolved from the JWT claims and stored on ``self``
        for downstream handlers.

        Fallback: static bearer token comparison (``MEMORY_API_TOKEN`` or
        ``server.token``), which also attempts principal resolution via
        ``infra.authorizer.resolve_principal`` for backward compatibility.

        The loopback auth bypass is gated behind ``insecure_loopback``.
        """
        self._principal = None
        self._principal_id = None
        peer = self.client_address[0]
        if getattr(self.server, "insecure_loopback", False) and _is_loopback(peer):
            return True
        auth = self.headers.get("Authorization", "")
        bearer = ""
        if auth.startswith("Bearer "):
            bearer = auth[7:]
        else:
            # Phase 2: accept the dashboard session cookie as an alternative
            # to the Authorization header. Both carry a JWT; the cookie path
            # is what the Streamlit login flow uses.
            cookie_tok = self._cookie_token()
            if cookie_tok:
                bearer = cookie_tok
        if not bearer:
            self._error("Authorization required: Bearer <token> or session cookie", 401)
            return False

        # Phase 2: try JWT validation first (SSO-issued tokens via Authlib).
        try:
            import sqlite3 as _sqlite3
            from infra.authlib_sso import (
                resolve_principal_by_external_sub,
                verify_token,
            )

            _conn = _sqlite3.connect(str(self.server.db_path))
            try:
                claims = verify_token(_conn, bearer)
                sub = claims.get("sub", "")
                provider = claims.get("provider", "")
                if sub and provider:
                    pid = resolve_principal_by_external_sub(
                        _conn, provider, sub,
                    )
                    if pid:
                        self._principal_id = pid
                        self._principal = type("_Principal", (), {"id": pid})()
                return True
            except Exception:
                pass
            finally:
                try:
                    _conn.close()
                except Exception:
                    pass

        except Exception:
            pass

        # Fallback: static bearer token comparison.
        token = getattr(self.server, "token", "") or os.environ.get("MEMORY_API_TOKEN", "")
        if not token:
            self._error("Auth required: set MEMORY_API_TOKEN or request locally", 401)
            return False
        if bearer != token:
            self._error("Invalid token", 403)
            return False
        # Resolve principal for downstream handlers (config-first, DB fallback)
        try:
            from infra.authorizer import resolve_principal
            principal = resolve_principal(
                db_path=str(self.server.db_path), token=bearer,
            )
            if principal:
                self._principal_id = principal.id
                self._principal = principal
        except Exception:
            pass
        return True

    def _require_auth_ws(self) -> bool:
        """Auth check for WebSocket upgrades.

        Phase 2: tries JWT validation first (SSO-issued tokens), then
        falls back to static bearer token comparison.

        Empty-token access is NOT allowed by default. The token may be
        unset only in a deliberate dev opt-in: when the server was started
        with ``insecure_loopback``.
        """
        self._principal = None
        self._principal_id = None

        # Phase 2: try JWT first.
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            bearer = auth[7:]
            try:
                import sqlite3 as _sqlite3
                from infra.authlib_sso import (
                    resolve_principal_by_external_sub,
                    verify_token,
                )

                _conn = _sqlite3.connect(str(self.server.db_path))
                try:
                    claims = verify_token(_conn, bearer)
                    sub = claims.get("sub", "")
                    provider = claims.get("provider", "")
                    if sub and provider:
                        pid = resolve_principal_by_external_sub(
                            _conn, provider, sub,
                        )
                        if pid:
                            self._principal_id = pid
                            self._principal = type("_Principal", (), {"id": pid})()
                    return True
                except Exception:
                    pass
                finally:
                    try:
                        _conn.close()
                    except Exception:
                        pass
            except Exception:
                pass

        # Fallback: static token comparison.
        token = getattr(self.server, "token", "") or os.environ.get("MEMORY_API_TOKEN", "")
        if not token:
            if getattr(self.server, "insecure_loopback", False):
                return True
            self._error(
                "Auth required: set MEMORY_API_TOKEN or start the server with "
                "insecure_loopback=True for local dev only",
                401,
            )
            return False

        if auth.startswith("Bearer ") and auth[7:] == token:
            self._resolve_ws_principal(auth[7:])
            return True
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        ws_token = qs.get("token", [""])[0]
        if ws_token and ws_token == token:
            self._resolve_ws_principal(ws_token)
            return True
        self._error("Unauthorized: provide token in Authorization header or ?token= query", 401)
        return False

    def _resolve_ws_principal(self, raw_token: str) -> None:
        """Resolve principal from a WS bearer token and store on self."""
        try:
            from infra.authorizer import resolve_principal
            principal = resolve_principal(
                db_path=str(self.server.db_path), token=raw_token,
            )
            if principal:
                self._principal_id = principal.id
                self._principal = principal
        except Exception:
            pass

    def do_OPTIONS(self) -> None:
        """CORS preflight handling."""
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if origin and (not API_CORS_ORIGINS or origin in API_CORS_ORIGINS):
            self.send_header("Access-Control-Allow-Origin", origin)
        elif not API_CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # WebSocket Upgrade Check
        is_ws = (self.headers.get("Upgrade", "").lower() == "websocket" and 
                 "upgrade" in self.headers.get("Connection", "").lower())
        
        if is_ws and (path == "/ws" or path == "/api/v1/streaming"):
            if self._rate_limited():
                self._error("Rate limit exceeded", 429)
                return
            self._handle_ws_handshake()
            return

        # Regular REST API Routes
        if path == "/health" or path == "":
            self._handle_health()
        elif path.startswith("/gateway/"):
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_gateway_proxy(path[len("/gateway/"):], "GET", parsed.query)
        elif path == "/api/v1/memories":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_list_memories(parse_qs(parsed.query))
        elif path == "/api/v1/memories/stats":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_stats()
        elif path == "/api/v1/memories/search":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_search_memories(parse_qs(parsed.query))
        elif path.startswith("/api/v1/memories/"):
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            note_id = path[len("/api/v1/memories/"):]
            self._handle_get_memory(note_id)
        elif path == "/api/v1/memories/categories":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_categories()
        elif path == "/api/v1/kg/nodes":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_kg_nodes(parse_qs(parsed.query))
        elif path == "/api/v1/kg/edges":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_kg_edges(parse_qs(parsed.query))
        elif path == "/api/v1/cloud/deployments":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_cloud_list_deployments(parse_qs(parsed.query))
        elif path == "/api/v1/cloud/usage":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_cloud_get_usage(parse_qs(parsed.query))
        elif path == "/api/v1/audit/logs":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_audit_logs(parse_qs(parsed.query))
        else:
            self._error("Not found", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Auth endpoints are public — rate limit by IP before auth.
        if path == "/api/v1/auth/login":
            if self._rate_limited():
                self._error("Rate limit exceeded", 429)
                return
            self._handle_login()
            return
        if path == "/api/v1/auth/logout":
            if self._rate_limited():
                self._error("Rate limit exceeded", 429)
                return
            self._handle_logout()
            return
        if path.startswith("/gateway/"):
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._handle_gateway_proxy(path[len("/gateway/"):], "POST", parsed.query, body)
            return
        if path == "/api/v1/cloud/webhooks/stripe":
            self._handle_cloud_stripe_webhook()
            return

        if not self._require_auth():
            return

        # Authenticated routes — rate limit by principal ID (per-user).
        if self._rate_limited(key=getattr(self, "_principal_id", None)):
            self._error("Rate limit exceeded", 429)
            return

        if path == "/api/v1/memories":
            self._handle_add_memory()
        elif path == "/api/v1/memories/search":
            self._handle_search_memories_post()
        elif path == "/api/v1/memories/clear":
            self._handle_clear_memories()
        elif path == "/api/v1/query":
            self._handle_query()
        elif path == "/api/v1/maintenance/rebuild":
            self._handle_rebuild()
        elif path == "/api/v1/maintenance/compact":
            self._handle_compact()
        elif path == "/api/v1/maintenance/integrity":
            self._handle_integrity()
        elif path == "/api/v1/compliance/gdpr/erase":
            self._handle_gdpr_erase()
        elif path == "/api/v1/rbac/init":
            self._handle_rbac_init()
        elif path == "/api/v1/rbac/principals":
            self._handle_rbac_create_principal()
        elif path == "/api/v1/rbac/roles":
            self._handle_rbac_create_role()
        elif path == "/api/v1/rbac/bindings":
            self._handle_rbac_grant()
        elif path == "/api/v1/acl/rules":
            self._handle_acl_add_rule()
        elif path == "/api/v1/kg/dedup":
            self._handle_kg_dedup()
        elif path == "/api/v1/kg/edges":
            self._handle_kg_create_edge()
        elif path == "/api/v1/kg/prune":
            self._handle_kg_prune()
        elif path == "/api/v1/kg/merge":
            self._handle_kg_merge()
        elif path == "/api/v1/memories/archive-stale":
            self._handle_archive_stale()
        elif path == "/api/v1/coordination/tasks":
            self._handle_create_task()
        elif path == "/api/v1/coordination/locks":
            self._handle_acquire_lock()
        elif path == "/api/v1/coordination/messages":
            self._handle_send_message()
        elif path == "/api/v1/coordination/state":
            self._handle_update_project_state()
        elif path == "/api/v1/cloud/checkout":
            self._handle_cloud_checkout()
        elif path == "/api/v1/cloud/signup":
            self._handle_cloud_signup()
        else:
            self._error("Not found", 404)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if not self._require_auth():
            return

        if self._rate_limited(key=getattr(self, "_principal_id", None)):
            self._error("Rate limit exceeded", 429)
            return

        if path.startswith("/gateway/"):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._handle_gateway_proxy(path[len("/gateway/"):], "PUT", parsed.query, body)
            return

        if path.startswith("/api/v1/memories/"):
            note_id = path[len("/api/v1/memories/"):]
            self._handle_update_memory(note_id)
        elif path.startswith("/api/v1/kg/entities/"):
            entity_id = path[len("/api/v1/kg/entities/"):]
            self._handle_update_kg_entity(entity_id)
        elif path.startswith("/api/v1/coordination/tasks/"):
            task_id = int(path[len("/api/v1/coordination/tasks/"):])
            self._handle_update_task(task_id)
        else:
            self._error("Not found", 404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if not self._require_auth():
            return

        if self._rate_limited(key=getattr(self, "_principal_id", None)):
            self._error("Rate limit exceeded", 429)
            return

        if path.startswith("/gateway/"):
            self._handle_gateway_proxy(path[len("/gateway/"):], "DELETE", parsed.query)
            return

        if path.startswith("/api/v1/memories/"):
            note_id = path[len("/api/v1/memories/"):]
            self._handle_delete_memory(note_id)
        elif path.startswith("/api/v1/kg/entities/"):
            entity_id = path[len("/api/v1/kg/entities/"):]
            self._handle_delete_kg_entity(entity_id)
        elif path.startswith("/api/v1/kg/edges/"):
            edge_id = path[len("/api/v1/kg/edges/"):]
            self._handle_delete_kg_edge(edge_id)
        elif path == "/api/v1/rbac/bindings":
            self._handle_rbac_revoke()
        elif path == "/api/v1/acl/rules":
            self._handle_acl_delete_rule()
        elif path.startswith("/api/v1/coordination/locks"):
            self._handle_release_lock(parse_qs(parsed.query))
        else:
            self._error("Not found", 404)

    # Handlers
    def _handle_health(self) -> None:
        note_count = 0
        try:
            client = MemoryClient(db_path=self.server.db_path)
            note_count = client.stats().memories
        except Exception as _wp_exc:
            logger.warning("_handle_health: broad except swallowed: %s", _wp_exc)
            pass
        self._write_json({
            "status": "healthy",
            "agent_id": self.server.agent_id,
            "note_count": note_count,
            "version": "1.1.0"
        })

    def _handle_list_memories(self, query_params: dict) -> None:
        try:
            limit = int(query_params.get("limit", ["50"])[0])
            offset = int(query_params.get("offset", ["0"])[0])
            client = MemoryClient(db_path=self.server.db_path)
            memories = client.list(limit=limit, offset=offset)
            # Serialize MemoryResult dataclasses
            memories_list = [
                {
                    "id": m.id,
                    "content": m.content,
                    "tags": m.tags,
                    "category": m.category,
                    "created_at": m.created_at,
                    "pinned": m.pinned,
                    "importance": m.importance
                }
                for m in memories
            ]
            self._write_json({"memories": memories_list})
        except Exception as e:
            logger.warning("_handle_list_memories: broad except swallowed: %s", e)
            self._error(f"Failed to list memories: {e}", 500)

    def _handle_search_memories(self, query_params: dict) -> None:
        try:
            query = query_params.get("query", [""])[0]
            if not query:
                self._error("Missing required query parameter", 400)
                return
            limit = int(query_params.get("limit", ["10"])[0])
            rerank = query_params.get("rerank", ["true"])[0].lower() == "true"
            tags_str = query_params.get("tags", [None])[0]
            tags = tags_str.split(",") if tags_str else None
            client = MemoryClient(db_path=self.server.db_path)
            results = client.search(query, limit=limit, rerank=rerank, tags=tags)
            
            # Serialize SearchResults object
            results_list = [
                {
                    "id": r.id,
                    "content": r.content,
                    "score": r.score,
                    "tags": r.tags,
                    "category": r.category,
                    "created_at": r.created_at
                }
                for r in results.results
            ]
            self._write_json({"results": results_list})
        except Exception as e:
            logger.warning("_handle_search_memories: broad except swallowed: %s", e)
            self._error(f"Search failed: {e}", 500)

    def _handle_search_memories_post(self) -> None:
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            query = req.get("query", "")
            if not query:
                self._error("Missing query field in request body", 400)
                return
            limit = req.get("limit", 10)
            rerank = req.get("rerank", True)
            tags = req.get("tags", None)
            client = MemoryClient(db_path=self.server.db_path)
            results = client.search(query, limit=limit, rerank=rerank, tags=tags)
            
            # Serialize SearchResults object
            results_list = [
                {
                    "id": r.id,
                    "content": r.content,
                    "score": r.score,
                    "tags": r.tags,
                    "category": r.category,
                    "created_at": r.created_at
                }
                for r in results.results
            ]
            self._write_json({"results": results_list})
        except Exception as e:
            logger.warning("_handle_search_memories_post: broad except swallowed: %s", e)
            self._error(f"Search failed: {e}", 500)

    def _handle_stats(self) -> None:
        try:
            client = MemoryClient(db_path=self.server.db_path)
            stats = client.stats()
            self._write_json({
                "memories": stats.memories,
                "vector_keys": stats.vector_keys,
                "chunks": stats.chunks,
                "facts": stats.facts,
                "entities": stats.entities,
                "relations": stats.relations,
            })
        except Exception as e:
            logger.warning("_handle_stats: broad except swallowed: %s", e)
            self._error(f"Failed to retrieve stats: {e}", 500)

    def _handle_get_memory(self, note_id: str) -> None:
        try:
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT id, content, tags, category, created_at, updated_at, deleted_at "
                    "FROM tenant_memories WHERE id = ?",
                    (note_id,),
                ).fetchone()
                if not row:
                    self._error(f"Memory not found: {note_id}", 404)
                    return
                # Check soft-deleted
                if row[6] is not None:
                    self._error(f"Memory soft-deleted: {note_id}", 410)
                    return
                
                tags_val = row[2]
                tags = json.loads(tags_val) if isinstance(tags_val, str) else list(tags_val or [])
                self._write_json({
                    "id": row[0],
                    "content": row[1],
                    "tags": tags,
                    "category": row[3],
                    "created_at": row[4],
                    "updated_at": row[5],
                    "deleted_at": row[6],
                })
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_get_memory: broad except swallowed: %s", e)
            self._error(f"Failed to retrieve memory: {e}", 500)

    def _read_json_body(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise ValueError("empty request body")
        body = self.rfile.read(length)
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc

    # ── Phase 2: auth endpoints ─────────────────────────────────────────────

    def _handle_login(self) -> None:
        """Exchange a valid API token for a JWT session cookie.

        Accepts either the static ``MEMORY_API_TOKEN``/``server.token`` or a
        ``[api.principals]``-mapped token. On success issues a short-lived JWT
        (signed by ``idem_token_key``) as an HttpOnly cookie. The dashboard's
        identity layer: it reuses the existing token trust anchor and the JWT
        signing/verification primitives — no new credential store.
        """
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        token = (req.get("token") or "").strip()
        if not token:
            self._error("Missing token field", 400)
            return

        # Validate the presented token against the same anchors _require_auth
        # trusts. Resolves a principal so the JWT can carry the identity.
        principal_id = None
        legacy = getattr(self.server, "token", "") or os.environ.get("MEMORY_API_TOKEN", "")
        if legacy and token == legacy:
            # Legacy token grants full access; mint a cookie without a bound
            # principal (downstream RBAC is not enforced for it, matching the
            # pre-RBAC behaviour).
            principal_id = None
        else:
            from infra.authorizer import resolve_principal

            principal = resolve_principal(
                db_path=str(self.server.db_path), token=token,
            )
            if principal is not None:
                principal_id = principal.id
            else:
                # Unknown token: fall back to static [api.principals] mapping so
                # a configured token still logs in even if no DB principal row
                # exists yet.
                from infra.authorizer import _load_principal_config

                entry = _load_principal_config().get(token)
                if entry:
                    principal_id = entry.partition(":")[-1]

        if principal_id is None and not (legacy and token == legacy):
            self._error("Invalid token", 403)
            return

        # Mint the JWT cookie.
        try:
            import sqlite3 as _sqlite3
            from infra.authlib_sso import sign_token

            _conn = _sqlite3.connect(str(self.server.db_path))
            try:
                jwt_token, _kid = sign_token(
                    _conn,
                    {"sub": principal_id or "legacy", "provider": "dashboard"},
                    expires_in=3600,
                )
            finally:
                try:
                    _conn.close()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("_handle_login: token issuance failed: %s", exc)
            self._error("Login unavailable: token signing not configured", 503)
            return

        self._set_auth_cookie(jwt_token)
        self._write_json({"status": "ok", "principal_id": principal_id})

    def _handle_logout(self) -> None:
        """Clear the session cookie."""
        self._clear_auth_cookie()
        self._write_json({"status": "ok"})

    def _handle_add_memory(self) -> None:
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            content = req.get("content", "")
            if not content:
                self._error("Missing content field", 400)
                return
            tags = req.get("tags", [])
            category = req.get("category", "sdk")
            is_global = req.get("is_global", False)
            pinned = req.get("pinned", False)
            importance = req.get("importance", 3)
            title_slug = req.get("title_slug", "")

            client = MemoryClient(db_path=self.server.db_path)
            note_id = client.save(
                content=content,
                tags=tags,
                category=category,
                is_global=is_global,
                pinned=pinned,
                importance=importance,
                title_slug=title_slug,
            )
            # Audit: tag as dashboard REST call
            try:
                from infra.audit import enqueue_audit
                enqueue_audit(
                    db_path=str(self.server.db_path),
                    tool="dashboard_save",
                    args={"note_id": note_id, "category": category, "tags": tags},
                    results_count=1,
                    principal_id=getattr(self, "_principal_id", None),
                )
            except Exception:
                pass
            self._write_json({"id": note_id, "status": "success"}, 201)
        except Exception as e:
            logger.warning("_handle_add_memory: broad except swallowed: %s", e)
            self._error(f"Failed to add memory: {e}", 500)

    def _handle_update_memory(self, note_id: str) -> None:
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            # `tier` is a simple metadata column — update via the saga-backed
            # ``update_tier`` (same write path, no embeddings/KG re-extraction).
            tier = req.get("tier")
            if tier is not None:
                if tier not in ("hot", "warm", "cold", "untrusted", "archive"):
                    self._error("invalid tier value", 400)
                    return
                from save.pipeline import update_tier
                found = update_tier(
                    note_id=note_id, tier=tier,
                    db_path=str(self.server.db_path),
                )
                if not found:
                    self._error(f"Memory not found: {note_id}", 404)
                    return
            other = {k: v for k, v in req.items()
                     if k in _MEMORY_UPDATE_FIELDS and k != "tier"}
            if other:
                from save.pipeline import save_memory
                other["note_id"] = note_id
                if "tags" not in other:
                    other["tags"] = []
                save_memory(db_path=str(self.server.db_path), **other)
            # Audit: tag as dashboard REST call
            try:
                from infra.audit import enqueue_audit
                enqueue_audit(
                    db_path=str(self.server.db_path),
                    tool="dashboard_update",
                    args={"note_id": note_id, "fields": list(req.keys())},
                    results_count=1,
                    principal_id=getattr(self, "_principal_id", None),
                )
            except Exception:
                pass
            self._write_json({"id": note_id, "status": "updated"})
        except Exception as e:
            logger.warning("_handle_update_memory: %s", e)
            self._error(f"Failed to update memory: {e}", 500)

    def _handle_delete_memory(self, note_id: str) -> None:
        try:
            client = MemoryClient(db_path=self.server.db_path)
            success = client.delete(note_id)
            if success:
                # Audit: tag as dashboard REST call
                try:
                    from infra.audit import enqueue_audit
                    enqueue_audit(
                        db_path=str(self.server.db_path),
                        tool="dashboard_delete",
                        args={"note_id": note_id},
                        results_count=1,
                        principal_id=getattr(self, "_principal_id", None),
                    )
                except Exception:
                    pass
                self._write_json({"success": True})
            else:
                self._error(f"Memory not found or delete failed: {note_id}", 404)
        except Exception as e:
            logger.warning("_handle_delete_memory: broad except swallowed: %s", e)
            self._error(f"Failed to delete memory: {e}", 500)

    def _handle_clear_memories(self) -> None:
        try:
            client = MemoryClient(db_path=self.server.db_path)
            cleared_count = client.clear()
            self._write_json({"cleared": cleared_count})
        except Exception as e:
            logger.warning("_handle_clear_memories: broad except swallowed: %s", e)
            self._error(f"Failed to clear memories: {e}", 500)

    def _handle_rebuild(self) -> None:
        try:
            client = MemoryClient(db_path=self.server.db_path)
            client.rebuild(scope="active")
            self._write_json({"success": True})
        except Exception as e:
            logger.warning("_handle_rebuild: broad except swallowed: %s", e)
            self._error(f"Rebuild index failed: {e}", 500)

    def _handle_compact(self) -> None:
        try:
            from agentic_memory.maintenance import Maintenance
            maint = Maintenance(db_path=self.server.db_path)
            result = maint.compact()
            self._write_json({"success": True, "result": str(result)})
        except Exception as e:
            logger.warning("_handle_compact: broad except swallowed: %s", e)
            self._error(f"Compaction failed: {e}", 500)

    def _handle_integrity(self) -> None:
        try:
            client = MemoryClient(db_path=self.server.db_path)
            report = client.check_integrity(deep=False)
            self._write_json({
                "success": not report.errors,
                "errors": report.errors,
            })
        except Exception as e:
            logger.warning("_handle_integrity: broad except swallowed: %s", e)
            self._error(f"Integrity check failed: {e}", 500)

    def _handle_gdpr_erase(self) -> None:
        """POST /api/v1/compliance/gdpr/erase — GDPR Right-to-Be-Forgotten.

        The target tenant is resolved from the authenticated principal, never
        from the request body (GAP 1 / GAP 5). A caller cannot erase a tenant
        other than their own unless they hold a cross-tenant admin role.
        """
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        data_subject_sub = req.get("data_subject_sub", "")
        if not data_subject_sub:
            self._error("data_subject_sub is required", 400)
            return
        try:
            from infra.gdpr import gdpr_erase
            from infra.authorizer import mcp_authorize
            from infra.db import open_db
            from pathlib import Path

            principal = getattr(self, "_principal", None)
            # Resolve tenant from the authenticated principal, NOT the body.
            tenant_id = principal.tenant_id if principal else "default"

            # RBAC gate: compliance:gdpr-erase (tenant-scoped).
            allowed = mcp_authorize(
                principal_id=getattr(self, "_principal_id", None),
                action="compliance",
                resource="gdpr-erase",
                db_path=str(self.server.db_path) if hasattr(self.server, "db_path") else None,
                tenant_id=tenant_id,
            )
            if not allowed:
                self._error(
                    "Forbidden: requires compliance:gdpr-erase role for this tenant",
                    403,
                )
                return

            with open_db(Path(str(self.server.db_path))) as conn:
                result = gdpr_erase(
                    conn=conn,  # type: ignore[arg-type]
                    principal_id=getattr(self, "_principal_id", "api"),
                    data_subject_sub=data_subject_sub,
                    tenant_id=tenant_id,
                )
            self._write_json(result)
        except Exception as e:
            logger.warning("_handle_gdpr_erase: %s", e)
            self._error(f"GDPR erase failed: {e}", 500)

    # ── Generic read-only query ────────────────────────────────────────────

    def _handle_query(self) -> None:
        """POST /api/v1/query — run a read-only SQL query.

        Security: only SELECT statements are allowed.
        """
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            sql = req.get("sql", "").strip()
            if not sql:
                self._error("Missing sql field", 400)
                return
            sql_upper = sql.upper().strip()
            if not sql_upper.startswith("SELECT") or "INTO" in sql_upper:
                self._error("Only SELECT queries allowed", 403)
                return
            params = req.get("params", [])
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                cursor = conn.execute(sql, params)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                results = [dict(zip(columns, row)) for row in rows]
                self._write_json({"results": results, "count": len(results)})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_query: %s", e)
            self._error(f"Query failed: {e}", 500)

    # ── Categories ──────────────────────────────────────────────────────────

    def _handle_categories(self) -> None:
        """GET /api/v1/memories/categories — list distinct memory categories."""
        try:
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                rows = conn.execute(
                    "SELECT DISTINCT category FROM memories WHERE category IS NOT NULL ORDER BY category"
                ).fetchall()
                cats = [r[0] for r in rows if r[0]]
                self._write_json({"categories": cats})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_categories: %s", e)
            self._error(f"Failed: {e}", 500)

    # ── RBAC ────────────────────────────────────────────────────────────────

    def _handle_rbac_init(self) -> None:
        """POST /api/v1/rbac/init — seed default RBAC roles."""
        try:
            from infra._lazy_imports import connection_pool, safe_close_db
            from infra.rbac import seed_default_roles
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                n = seed_default_roles(conn)
                conn.commit()
                self._write_json({"created": n})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_rbac_init: %s", e)
            self._error(f"RBAC init failed: {e}", 500)

    def _handle_rbac_create_principal(self) -> None:
        """POST /api/v1/rbac/principals — create a principal."""
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            pid = req.get("id", "")
            if not pid:
                self._error("Missing id field", 400)
                return
            kind = req.get("kind", "agent")
            display_name = req.get("display_name", pid)
            tenant_id = req.get("tenant_id", "default")
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO principals (id, kind, display_name, tenant_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
                    (pid, kind, display_name, tenant_id),
                )
                conn.commit()
                self._write_json({"id": pid, "status": "created"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_rbac_create_principal: %s", e)
            self._error(f"Failed: {e}", 500)

    def _handle_rbac_create_role(self) -> None:
        """POST /api/v1/rbac/roles — create a role."""
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            rid = req.get("id", "")
            if not rid:
                self._error("Missing id field", 400)
                return
            description = req.get("description", "")
            tenant_id = req.get("tenant_id", "default")
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO roles (id, description, tenant_id) VALUES (?, ?, ?)",
                    (rid, description or None, tenant_id),
                )
                conn.commit()
                self._write_json({"id": rid, "status": "created"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_rbac_create_role: %s", e)
            self._error(f"Failed: {e}", 500)

    def _handle_rbac_grant(self) -> None:
        """POST /api/v1/rbac/bindings — grant a role to a principal."""
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            principal_id = req.get("principal_id", "")
            role_id = req.get("role_id", "")
            if not principal_id or not role_id:
                self._error("principal_id and role_id required", 400)
                return
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO role_bindings (principal_id, role_id, granted_at, granted_by) "
                    "VALUES (?, ?, datetime('now'), 'api')",
                    (principal_id, role_id),
                )
                conn.commit()
                self._write_json({"status": "granted"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_rbac_grant: %s", e)
            self._error(f"Failed: {e}", 500)

    def _handle_rbac_revoke(self) -> None:
        """DELETE /api/v1/rbac/bindings — revoke a role from a principal."""
        try:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            principal_id = params.get("principal_id", [None])[0]
            role_id = params.get("role_id", [None])[0]
            if not principal_id or not role_id:
                self._error("Query params principal_id and role_id required", 400)
                return
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                conn.execute(
                    "DELETE FROM role_bindings WHERE principal_id=? AND role_id=?",
                    (principal_id, role_id),
                )
                conn.commit()
                self._write_json({"status": "revoked"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_rbac_revoke: %s", e)
            self._error(f"Failed: {e}", 500)

    # ── ACL ─────────────────────────────────────────────────────────────────

    def _handle_acl_add_rule(self) -> None:
        """POST /api/v1/acl/rules — add an ACL override rule."""
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            principal_id = req.get("principal_id", "")
            resource_id = req.get("resource_id", "")
            action = req.get("action", "")
            effect = req.get("effect", "allow")
            if not principal_id or not resource_id or not action:
                self._error("principal_id, resource_id, action required", 400)
                return
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO acl_overrides "
                    "(principal_id, resource_id, action, effect, granted_at, granted_by) "
                    "VALUES (?, ?, ?, ?, datetime('now'), 'api')",
                    (principal_id, resource_id, action, effect),
                )
                conn.commit()
                self._write_json({"status": "added"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_acl_add_rule: %s", e)
            self._error(f"Failed: {e}", 500)

    def _handle_acl_delete_rule(self) -> None:
        """DELETE /api/v1/acl/rules — delete an ACL override rule."""
        try:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            principal_id = params.get("principal_id", [None])[0]
            resource_id = params.get("resource_id", [None])[0]
            action = params.get("action", [None])[0]
            if not principal_id or not resource_id or not action:
                self._error("Query params principal_id, resource_id, action required", 400)
                return
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                conn.execute(
                    "DELETE FROM acl_overrides WHERE principal_id=? AND resource_id=? AND action=?",
                    (principal_id, resource_id, action),
                )
                conn.commit()
                self._write_json({"status": "deleted"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_acl_delete_rule: %s", e)
            self._error(f"Failed: {e}", 500)

    def _handle_kg_nodes(self, query_params: dict) -> None:
        try:
            limit = int(query_params.get("limit", ["100"])[0])
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                rows = conn.execute(
                    "SELECT id, name, type, properties FROM kg_entities LIMIT ?",
                    (limit,),
                ).fetchall()
                nodes = [
                    {
                        "id": r[0],
                        "name": r[1],
                        "type": r[2],
                        "properties": json.loads(r[3]) if r[3] else {}
                    }
                    for r in rows
                ]
                self._write_json({"nodes": nodes})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_kg_nodes: broad except swallowed: %s", e)
            self._error(f"Failed to list KG nodes: {e}", 500)

    def _handle_kg_edges(self, query_params: dict) -> None:
        try:
            limit = int(query_params.get("limit", ["100"])[0])
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                rows = conn.execute(
                    "SELECT source_id, target_id, relation, weight FROM kg_edges LIMIT ?",
                    (limit,),
                ).fetchall()
                edges = [
                    {
                        "source": r[0],
                        "target": r[1],
                        "relation": r[2],
                        "weight": r[3],
                    }
                    for r in rows
                ]
                self._write_json({"edges": edges})
            finally:
                safe_close_db(conn)
        except Exception as e:
                logger.warning("_handle_kg_edges: broad except swallowed: %s", e)
                self._error(f"Failed to list KG edges: {e}", 500)

    def _handle_kg_create_edge(self) -> None:
        """Create a KG edge via the authenticated API (dashboard gap-detector)."""
        try:
            body = self._read_json_body() or {}
            source_id = body.get("source_id")
            target_id = body.get("target_id")
            relation = body.get("relation") or "related"
            weight = float(body.get("weight", 1.0))
            properties = body.get("properties") or {}
            if not isinstance(source_id, int) or not isinstance(target_id, int):
                self._error("source_id and target_id (int) required", 400)
                return
            from infra.db import open_db
            with open_db(Path(str(self.server.db_path)), write=True) as conn:
                cur = conn.execute(
                    "INSERT INTO kg_edges (source_id, target_id, relation, weight) "
                    "VALUES (?, ?, ?, ?)",
                    (source_id, target_id, relation, weight),
                )
                conn.commit()
                edge_id = cur.lastrowid
                self._write_json({"id": edge_id, "status": "ok"})
        except Exception as e:
            logger.warning("_handle_kg_create_edge: %s", e)
            self._error(f"Failed to create KG edge: {e}", 500)

    def _handle_update_kg_entity(self, entity_id: str) -> None:
        """Update a KG entity's entity_type via the authenticated API."""
        try:
            eid = int(entity_id)
            body = self._read_json_body() or {}
            entity_type = body.get("entity_type")
            if entity_type is None:
                self._error("entity_type required", 400)
                return
            from infra.db import open_db
            with open_db(Path(str(self.server.db_path)), write=True) as conn:
                existing = conn.execute(
                    "SELECT id FROM kg_entities WHERE id=?", (eid,)
                ).fetchone()
                if not existing:
                    self._error(f"KG entity not found: {eid}", 404)
                    return
                conn.execute(
                    "UPDATE kg_entities SET entity_type=? WHERE id=?",
                    (entity_type, eid),
                )
                conn.commit()
                self._write_json({"id": eid, "status": "ok"})
        except ValueError:
            self._error("entity_id must be an int", 400)
        except Exception as e:
            logger.warning("_handle_update_kg_entity: %s", e)
            self._error(f"Failed to update KG entity: {e}", 500)

    def _handle_delete_kg_entity(self, entity_id: str) -> None:
        """Delete a KG entity by id. Orphaned edges are cleaned up via cascade.

        KG mutations are coordinated writes: the connection is acquired from the
        per-DB pool (file lock first, then conn — Hard Rule 9) and committed
        through the same path the saga uses, so the single-writer invariant holds.
        """
        if not entity_id:
            self._error("entity id required", 400)
            return
        try:
            from infra.db import open_db
            with open_db(Path(str(self.server.db_path)), write=True) as conn:
                conn.execute("DELETE FROM kg_edges WHERE source_id=? OR target_id=?", (entity_id, entity_id))
                cur = conn.execute("DELETE FROM kg_entities WHERE id=?", (entity_id,))
                conn.commit()
                if cur.rowcount == 0:
                    self._error(f"KG entity not found: {entity_id}", 404)
                    return
                self._write_json({"status": "deleted", "id": entity_id})
        except Exception as e:
            logger.warning("_handle_delete_kg_entity: %s", e)
            self._error(f"Failed to delete KG entity: {e}", 500)

    def _handle_delete_kg_edge(self, edge_id: str) -> None:
        if not edge_id:
            self._error("edge id required", 400)
            return
        try:
            from infra.db import open_db
            with open_db(Path(str(self.server.db_path)), write=True) as conn:
                cur = conn.execute("DELETE FROM kg_edges WHERE id=?", (edge_id,))
                conn.commit()
                if cur.rowcount == 0:
                    self._error(f"KG edge not found: {edge_id}", 404)
                    return
                self._write_json({"status": "deleted", "id": edge_id})
        except Exception as e:
            logger.warning("_handle_delete_kg_edge: %s", e)
            self._error(f"Failed to delete KG edge: {e}", 500)

    def _handle_kg_dedup(self) -> None:
        try:
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                merged = 0
                dupes = conn.execute(
                    "SELECT name, COUNT(*) cnt, GROUP_CONCAT(id) ids "
                    "FROM kg_entities GROUP BY LOWER(name) HAVING cnt > 1"
                ).fetchall()
                for name, cnt, ids_str in dupes:
                    ids = [int(x) for x in ids_str.split(",")]
                    keep = ids[0]
                    for remove_id in ids[1:]:
                        conn.execute("UPDATE kg_edges SET source_id=? WHERE source_id=?", (keep, remove_id))
                        conn.execute("UPDATE kg_edges SET target_id=? WHERE target_id=?", (keep, remove_id))
                        conn.execute("DELETE FROM kg_entities WHERE id=?", (remove_id,))
                        merged += 1
                conn.execute(
                    "DELETE FROM kg_edges WHERE id NOT IN ("
                    "SELECT MAX(id) FROM kg_edges GROUP BY source_id, target_id, relation)"
                )
                conn.commit()
                self._write_json({"merged": merged, "status": "ok"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_kg_dedup: broad except swallowed: %s", e)
            self._error(f"Failed to dedup KG: {e}", 500)

    def _handle_kg_prune(self) -> None:
        """Prune (delete) a set of KG entities and their incident edges in one txn."""
        try:
            body = self._read_json_body() or {}
            entity_ids = body.get("entity_ids")
            if not isinstance(entity_ids, list):
                self._error("entity_ids (list[int]) required", 400)
                return
            entity_ids = [int(x) for x in entity_ids]
            if not entity_ids:
                self._write_json({"pruned": 0})
                return
            from infra.db import open_db
            with open_db(Path(str(self.server.db_path)), write=True) as conn:
                ph = ",".join("?" for _ in entity_ids)
                conn.execute(f"DELETE FROM kg_entities WHERE id IN ({ph})", entity_ids)
                conn.execute(
                    f"DELETE FROM kg_edges WHERE source_id IN ({ph}) OR target_id IN ({ph})",
                    entity_ids + entity_ids,
                )
                conn.commit()
                self._write_json({"pruned": len(entity_ids)})
        except Exception as e:
            logger.warning("_handle_kg_prune: %s", e)
            self._error(f"Failed to prune KG entities: {e}", 500)

    def _handle_kg_merge(self) -> None:
        """Merge remove_id into keep_id: reassign edges, sum mentions, delete remove_id.

        Ports the exact SQL semantics from dashboard/tab_knowledge.py _merge_entities.
        KG mutations use the per-DB pool connection (file lock first, then conn —
        Hard Rule 9) committed through the saga path so the single-writer invariant holds.
        """
        try:
            body = self._read_json_body() or {}
            keep_id = body.get("keep_id")
            remove_id = body.get("remove_id")
            if keep_id is None or remove_id is None:
                self._error("keep_id and remove_id (int) required", 400)
                return
            keep_id = int(keep_id)
            remove_id = int(remove_id)
            from infra.db import open_db
            with open_db(Path(str(self.server.db_path)), write=True) as conn:
                keep = conn.execute(
                    "SELECT id FROM kg_entities WHERE id=?", (keep_id,)
                ).fetchone()
                rem = conn.execute(
                    "SELECT id FROM kg_entities WHERE id=?", (remove_id,)
                ).fetchone()
                if keep is None or rem is None:
                    self._error(
                        f"KG entity not found: keep_id={keep_id} remove_id={remove_id}", 404
                    )
                    return

                # Step 1: conflicts — reassigning remove edges duplicates a keep edge
                conflicts = conn.execute(
                    """
                    SELECT r.id, r.source_id, r.target_id, r.relation, r.weight,
                           k.weight as keep_weight
                    FROM kg_edges r
                    JOIN kg_edges k ON (
                        (CASE WHEN r.source_id = ? THEN ? ELSE r.source_id END) = k.source_id
                        AND (CASE WHEN r.target_id = ? THEN ? ELSE r.target_id END) = k.target_id
                        AND r.relation = k.relation
                        AND k.id != r.id
                    )
                    WHERE r.source_id = ? OR r.target_id = ?
                    """,
                    (remove_id, keep_id, remove_id, keep_id, remove_id, remove_id),
                ).fetchall()

                # Delete conflicting remove-edges, summing weights onto the keep edge
                for row in conflicts:
                    remove_edge_id, _, _, _, remove_w, keep_w = row
                    new_weight = (keep_w or 0) + (remove_w or 0)
                    keep_edge_id = conn.execute(
                        "SELECT id FROM kg_edges WHERE source_id=? AND target_id=? AND relation=? AND id!=?",
                        (
                            keep_id if row[1] == remove_id else row[1],
                            keep_id if row[2] == remove_id else row[2],
                            row[3],
                            remove_edge_id,
                        ),
                    ).fetchone()[0]
                    conn.execute(
                        "UPDATE kg_edges SET weight=? WHERE id=?", (new_weight, keep_edge_id)
                    )
                    conn.execute("DELETE FROM kg_edges WHERE id=?", (remove_edge_id,))

                # Step 2: reassign remaining edges
                conn.execute(
                    "UPDATE kg_edges SET source_id=? WHERE source_id=?", (keep_id, remove_id)
                )
                conn.execute(
                    "UPDATE kg_edges SET target_id=? WHERE target_id=?", (keep_id, remove_id)
                )

                # Step 3: sum mentions
                conn.execute(
                    "UPDATE kg_entities SET mentions = mentions + "
                    "(SELECT COALESCE(mentions, 0) FROM kg_entities WHERE id=?) WHERE id=?",
                    (remove_id, keep_id),
                )

                # Step 4: delete removed entity
                conn.execute("DELETE FROM kg_entities WHERE id=?", (remove_id,))

                # Step 5: final dedup (keep heavier edge)
                conn.execute(
                    "DELETE FROM kg_edges WHERE id NOT IN ("
                    "  SELECT MAX(id) FROM kg_edges "
                    "  GROUP BY source_id, target_id, relation"
                    ")"
                )

                conn.commit()
                self._write_json(
                    {"merged": True, "keep_id": keep_id, "remove_id": remove_id}
                )
        except Exception as e:
            logger.warning("_handle_kg_merge: %s", e)
            self._error(f"Failed to merge KG entities: {e}", 500)

    def _handle_archive_stale(self) -> None:
        try:
            body = self._read_json_body() or {}
            min_fitness = float(body.get("min_fitness", 0.3))
            min_age_days = int(body.get("min_age_days", 90))
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=30.0)
            try:
                cols = conn.execute("PRAGMA table_info(memories)").fetchall()
                col_defs = ", ".join(f"{c[1]} {c[2]}" for c in cols)
                conn.execute(f"CREATE TABLE IF NOT EXISTS memory_archive ({col_defs})")
                stale = conn.execute(
                    "SELECT id FROM memories "
                    "WHERE COALESCE(fitness_score, 0.5) < ? "
                    "AND created_at < datetime('now', ?)",
                    (min_fitness, f"-{min_age_days} days"),
                ).fetchall()
                archived = 0
                for (mid,) in stale:
                    row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
                    if row:
                        col_names = [c[1] for c in cols]
                        placeholders = ",".join("?" for _ in col_names)
                        conn.execute(
                            f"INSERT OR IGNORE INTO memory_archive ({','.join(col_names)}) VALUES ({placeholders})",
                            row,
                        )
                        conn.execute("DELETE FROM memories WHERE id=?", (mid,))
                        archived += 1
                conn.commit()
                self._write_json({"archived": archived, "status": "ok"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_archive_stale: broad except swallowed: %s", e)
            self._error(f"Failed to archive stale memories: {e}", 500)

    # ── Coordination handlers ──────────────────────────────────────────────
    def _handle_create_task(self) -> None:
        try:
            body = self._read_json_body() or {}
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=10.0)
            try:
                now = time.time()
                status = "active" if body.get("assigned_to") else "pending"
                conn.execute(
                    "INSERT INTO shared_tasks (project_id, task_type, description, assigned_to, status, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'dashboard', ?, ?)",
                    (body.get("project_id", "default"), body.get("task_type"), body.get("description"),
                     body.get("assigned_to"), status, now, now),
                )
                conn.commit()
                self._write_json({"status": "ok"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_create_task: broad except swallowed: %s", e)
            self._error(f"Failed to create task: {e}", 500)

    def _handle_update_task(self, task_id: int) -> None:
        try:
            body = self._read_json_body() or {}
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=10.0)
            try:
                conn.execute(
                    "UPDATE shared_tasks SET status=?, assigned_to=?, updated_at=? WHERE id=?",
                    (body.get("status"), body.get("assigned_to"), time.time(), task_id),
                )
                conn.commit()
                self._write_json({"status": "ok"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_update_task: broad except swallowed: %s", e)
            self._error(f"Failed to update task: {e}", 500)

    def _handle_acquire_lock(self) -> None:
        try:
            body = self._read_json_body() or {}
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=10.0)
            try:
                now = time.time()
                conn.execute(
                    "INSERT OR REPLACE INTO file_locks (file_path, locked_by, locked_at, expires_at) VALUES (?, ?, ?, ?)",
                    (body.get("file_path"), body.get("locked_by", "dashboard"), now, now + int(body.get("ttl", 300))),
                )
                conn.commit()
                self._write_json({"status": "ok"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_acquire_lock: broad except swallowed: %s", e)
            self._error(f"Failed to acquire lock: {e}", 500)

    def _handle_release_lock(self, query_params: dict) -> None:
        try:
            file_path = query_params.get("file_path", [""])[0]
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=10.0)
            try:
                conn.execute("DELETE FROM file_locks WHERE file_path=?", (file_path,))
                conn.commit()
                self._write_json({"status": "ok"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_release_lock: broad except swallowed: %s", e)
            self._error(f"Failed to release lock: {e}", 500)

    def _handle_send_message(self) -> None:
        try:
            body = self._read_json_body() or {}
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=10.0)
            try:
                now = time.time()
                conn.execute(
                    "INSERT INTO agent_messages (from_agent, to_agent, message_type, payload, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?)",
                    (body.get("from_agent", "dashboard"), body.get("to_agent"), body.get("message_type"),
                     body.get("payload"), now),
                )
                conn.commit()
                self._write_json({"status": "ok"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_send_message: broad except swallowed: %s", e)
            self._error(f"Failed to send message: {e}", 500)

    def _handle_update_project_state(self) -> None:
        try:
            body = self._read_json_body() or {}
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=10.0)
            try:
                now = time.time()
                conn.execute(
                    "INSERT OR REPLACE INTO project_state (project_id, key, value, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (body.get("project_id"), body.get("key"), body.get("value"), body.get("updated_by", "dashboard"), now),
                )
                conn.commit()
                self._write_json({"status": "ok"})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_update_project_state: broad except swallowed: %s", e)
            self._error(f"Failed to update project state: {e}", 500)

    def _handle_ws_handshake(self) -> None:
        """RFC 6455 WebSocket Handshake and protocol upgrade."""
        if not self._require_auth_ws():
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "Sec-WebSocket-Key required")
            return

        guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        accept = base64.b64encode(hashlib.sha1((key + guid).encode("utf-8")).digest()).decode("utf-8")
        
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        sock = self.connection
        sock.settimeout(None)

        client_id = f"{self.client_address[0]}:{self.client_address[1]}"
        logger.info(f"WebSocket connected: {client_id}")
        self.server.register_ws_client(client_id, sock)

        try:
            # WebSocket framing loop
            while True:
                header = sock.recv(2)
                if not header or len(header) < 2:
                    break
                
                byte1, byte2 = header[0], header[1]
                opcode = byte1 & 0x0F
                masked = (byte2 & 0x80) != 0
                payload_len = byte2 & 0x7F

                if opcode == 8:  # Close
                    logger.info(f"WebSocket closed by client: {client_id}")
                    break

                if payload_len == 126:
                    ext_len = sock.recv(2)
                    payload_len = int.from_bytes(ext_len, byteorder="big")
                elif payload_len == 127:
                    ext_len = sock.recv(8)
                    payload_len = int.from_bytes(ext_len, byteorder="big")

                if masked:
                    masking_key = sock.recv(4)
                else:
                    masking_key = b""

                payload_data = b""
                while len(payload_data) < payload_len:
                    chunk = sock.recv(payload_len - len(payload_data))
                    if not chunk:
                        break
                    payload_data += chunk

                if len(payload_data) < payload_len:
                    break

                if masked:
                    payload = bytearray(payload_data[i] ^ masking_key[i % 4] for i in range(len(payload_data)))
                else:
                    payload = bytearray(payload_data)

                if opcode == 9:  # Ping, reply with Pong
                    pong_frame = bytearray([0x8A])
                    if len(payload) <= 125:
                        pong_frame.append(len(payload))
                    elif len(payload) <= 65535:
                        pong_frame.append(126)
                        pong_frame.extend(len(payload).to_bytes(2, byteorder="big"))
                    else:
                        pong_frame.append(127)
                        pong_frame.extend(len(payload).to_bytes(8, byteorder="big"))
                    sock.sendall(bytes(pong_frame) + payload)
                    continue

                if opcode == 1:  # Text frame
                    try:
                        text = payload.decode("utf-8")
                        req = json.loads(text)
                        action = req.get("action")
                        if action == "ping":
                            self.server.send_ws_message(sock, json.dumps({"event": "pong"}))
                        elif action == "search":
                            query = req.get("query", "")
                            limit = req.get("limit", 10)
                            client = MemoryClient(db_path=self.server.db_path)
                            res = client.search(query, limit=limit)
                            
                            # Serialize results
                            results_list = [
                                {
                                    "id": r.id,
                                    "content": r.content,
                                    "score": r.score,
                                    "tags": r.tags,
                                    "category": r.category,
                                    "created_at": r.created_at,
                                }
                                for r in res.results
                            ]
                            self.server.send_ws_message(sock, json.dumps({
                                "event": "search_result",
                                "query": query,
                                "results": results_list
                            }))
                    except Exception as e:
                        logger.warning(f"Error processing WS text message: {e}")

        except ConnectionResetError:
            pass
        except Exception as e:
            logger.warning(f"WebSocket read error on {client_id}: {e}")
        finally:
            self.server.unregister_ws_client(client_id)
            logger.info(f"WebSocket disconnected: {client_id}")

    @property
    def cloud_store(self):
        from pathlib import Path
        if not hasattr(self.server, "_cloud_store"):
            from infra_cloud.store import CloudStateStore
            db_path = Path(self.server.db_path).parent / "cloud_state.db"
            self.server._cloud_store = CloudStateStore(db_path)
        return self.server._cloud_store

    def _handle_gateway_proxy(
        self, deployment_subpath: str, method: str, query_string: str = "",
        body: bytes | None = None,
    ) -> None:
        """Proxy /gateway/<deployment_id>/<path> to the deployment's own REST API.

        Uses GatewayRouter for lookup, usage metering, and limit enforcement.
        """
        from infra_cloud.gateway import GatewayRouter

        parts = deployment_subpath.split("/", 1)
        deployment_id = parts[0]
        sub_path = parts[1] if len(parts) > 1 else ""

        router = GatewayRouter(self.cloud_store)

        # Forward the original request headers (minus hop-by-hop).
        fwd_headers = {}
        for key in ("Authorization", "Content-Type", "Accept"):
            val = self.headers.get(key)
            if val:
                fwd_headers[key] = val

        result = router.route(
            deployment_id=deployment_id,
            sub_path=sub_path,
            method=method,
            headers=fwd_headers,
            body=body,
        )

        status = result.get("status", 502)
        resp_headers = result.get("headers", {})
        resp_body = result.get("body", b"")

        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() not in ("transfer-encoding", "connection"):
                self.send_header(k, v)
        self.end_headers()
        if isinstance(resp_body, dict):
            resp_body = json.dumps(resp_body).encode("utf-8")
        elif isinstance(resp_body, str):
            resp_body = resp_body.encode("utf-8")
        self.wfile.write(resp_body)

    def _handle_cloud_list_deployments(self, query_params: dict) -> None:
        try:
            cust_ids = query_params.get("customer_id")
            cust_id = cust_ids[0] if cust_ids else "cust_1"
            deps = self.cloud_store.list_deployments(cust_id)
            self._write_json({"deployments": deps})
        except Exception as e:
            logger.warning("_handle_cloud_list_deployments: %s", e)
            self._error(f"Failed to list deployments: {e}", 500)

    def _handle_cloud_get_usage(self, query_params: dict) -> None:
        try:
            dep_ids = query_params.get("deployment_id")
            if not dep_ids:
                self._error("deployment_id required", 400)
                return
            dep_id = dep_ids[0]
            dep = self.cloud_store.get_deployment(dep_id)
            if not dep:
                self._error("deployment not found", 404)
                return
            
            # Fetch sub & plan
            subs = self.cloud_store.list_subscriptions(dep_id)
            active_sub = None
            plan = None
            if subs:
                for s in subs:
                    if s["status"] == "active":
                        active_sub = s
                        break
                if not active_sub:
                    active_sub = subs[0]
            
            if active_sub:
                plan = self.cloud_store.get_plan(active_sub["plan_id"])
            if not plan:
                plan = self.cloud_store.get_plan("free")
            
            usage = self.cloud_store.get_usage(dep_id)
            invoices = self.cloud_store.list_invoices(dep["customer_id"])
            
            self._write_json({
                "deployment": dep,
                "subscription": active_sub,
                "plan": plan,
                "usage": usage,
                "invoices": invoices
            })
        except Exception as e:
            logger.warning("_handle_cloud_get_usage: %s", e)
            self._error(f"Failed to get usage stats: {e}", 500)

    def _handle_cloud_checkout(self) -> None:
        """Create a real Stripe Checkout Session for plan upgrade/downgrade.

        Requires env vars:
          STRIPE_SECRET_KEY   — Stripe API secret key
          STRIPE_WEBHOOK_SECRET — Stripe webhook signing secret (for verification)

        The checkout session uses ``client_reference_id`` = deployment_id and
        ``metadata.plan_id`` so the webhook handler can activate the subscription.
        """
        try:
            body = self._read_json_body() or {}
            dep_id = body.get("deployment_id")
            plan_id = body.get("plan_id")
            if not dep_id or not plan_id:
                self._error("deployment_id and plan_id required", 400)
                return

            plan = self.cloud_store.get_plan(plan_id)
            if not plan:
                self._error(f"unknown plan: {plan_id}", 400)
                return

            # Resolve Stripe Price ID: env var override > DB column
            import os
            price_id = os.environ.get(f"STRIPE_PRICE_{plan_id.upper()}")
            if not price_id:
                price_id = self.cloud_store.get_stripe_price_id(plan_id)
            if not price_id or "placeholder" in (price_id or ""):
                self._error(
                    f"Stripe Price ID not configured for plan '{plan_id}'. "
                    f"Set STRIPE_PRICE_{plan_id.upper()} env var or update the plans table.",
                    503,
                )
                return

            stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
            if not stripe_key:
                self._error("Stripe not configured: set STRIPE_SECRET_KEY", 503)
                return

            import stripe
            stripe.api_key = stripe_key

            dep = self.cloud_store.get_deployment(dep_id)
            customer_email = ""
            if dep:
                cust = self.cloud_store.get_customer(dep.get("customer_id", ""))
                if cust:
                    customer_email = cust.get("email", "")

            session = stripe.checkout.Session.create(
                mode="subscription",
                line_items=[{"price": price_id, "quantity": 1}],
                client_reference_id=dep_id,
                metadata={"plan_id": plan_id, "deployment_id": dep_id},
                **({"customer_email": customer_email} if customer_email else {}),
                success_url=body.get("success_url", "https://app.agentic-memory.dev/billing?success=1"),
                cancel_url=body.get("cancel_url", "https://app.agentic-memory.dev/billing?canceled=1"),
            )

            self._write_json({
                "checkout_url": session.url,
                "session_id": session.id,
                "status": "ok",
            })
        except Exception as e:
            logger.warning("_handle_cloud_checkout: %s", e)
            self._error(f"Checkout failed: {e}", 500)

    def _handle_cloud_signup(self) -> None:
        """Create a new customer + deployment + optionally provision memory.db.

        POST /api/v1/cloud/signup
        Body: {"email": "...", "name": "...", "plan_id": "free", "mode": "cloud_hosted"}

        Modes:
          - "cloud_hosted": provisions memory.db with schema (default)
          - "self_hosted":  skips DB provisioning (user manages their own DB)
        """
        import uuid
        import os
        try:
            body = self._read_json_body() or {}
            email = body.get("email", "").strip()
            name = body.get("name", "").strip() or email
            plan_id = body.get("plan_id", "free")
            mode = body.get("mode", "cloud_hosted")
            if not email:
                self._error("email is required", 400)
                return
            if mode not in ("cloud_hosted", "self_hosted"):
                self._error("mode must be 'cloud_hosted' or 'self_hosted'", 400)
                return

            plan = self.cloud_store.get_plan(plan_id)
            if not plan:
                self._error(f"unknown plan: {plan_id}", 400)
                return

            customer_id = f"cust_{uuid.uuid4().hex[:12]}"
            self.cloud_store.create_customer(
                customer_id=customer_id, email=email, name=name,
            )

            deployment_id = f"dep_{uuid.uuid4().hex[:12]}"
            tenant_id = f"tenant_{uuid.uuid4().hex[:8]}"
            mem_db_path = None

            if mode == "cloud_hosted":
                # Provision the memory DB directory
                base_dir = Path(self.server.db_path).parent / "deployments" / deployment_id
                base_dir.mkdir(parents=True, exist_ok=True)
                mem_db_path = str(base_dir / "memory.db")

                # Initialize the memory DB with the schema via the migration runner
                try:
                    from infra.migration_runner import run_migrations
                    run_migrations(mem_db_path)
                except Exception as mig_exc:
                    logger.error("signup memory DB init FAILED: %s", mig_exc)
                    self._error(f"Deployment created but DB init failed: {mig_exc}", 500)
                    return

            # Create the deployment row
            self.cloud_store.create_deployment(
                deployment_id=deployment_id,
                customer_id=customer_id,
                tenant_id=tenant_id,
                label=name,
                db_path=mem_db_path,
                api_base=f"http://127.0.0.1:{self.server.port}",
            )

            # Create default subscription
            sub_id = f"sub_{uuid.uuid4().hex[:8]}"
            self.cloud_store.create_subscription(
                subscription_id=sub_id,
                deployment_id=deployment_id,
                plan_id=plan_id,
                status="active",
                current_period_end=time.time() + 30 * 86400,
            )

            self._write_json({
                "customer_id": customer_id,
                "deployment_id": deployment_id,
                "tenant_id": tenant_id,
                "plan_id": plan_id,
                "mode": mode,
                "db_path": mem_db_path,
                "status": "active",
            }, 201)
        except Exception as e:
            logger.warning("_handle_cloud_signup: %s", e)
            self._error(f"Signup failed: {e}", 500)

    def _handle_cloud_stripe_webhook(self) -> None:
        """Handle real Stripe webhooks with signature verification.

        Verifies the webhook signature using ``STRIPE_WEBHOOK_SECRET`` before
        processing.  Handles ``checkout.session.completed`` to activate
        subscriptions and ``customer.subscription.updated`` / ``deleted`` for
        plan changes and cancellations.
        """
        import os
        import stripe

        webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", 0)))

        # Signature verification (required in production)
        sig_header = self.headers.get("Stripe-Signature", "")
        if webhook_secret and sig_header:
            try:
                event = stripe.Webhook.construct_event(
                    raw_body, sig_header, webhook_secret,
                )
            except (stripe.error.SignatureVerificationError, ValueError) as e:
                logger.warning("Stripe webhook signature verification failed: %s", e)
                self._error("Invalid webhook signature", 400)
                return
        else:
            # No webhook secret configured — parse raw JSON (dev/test only)
            try:
                event = json.loads(raw_body.decode("utf-8"))
            except Exception:
                self._error("Invalid JSON payload", 400)
                return

        event_type = event.get("type", "")

        if event_type == "checkout.session.completed":
            obj = event.get("data", {}).get("object", {})
            deployment_id = obj.get("client_reference_id")
            plan_id = obj.get("metadata", {}).get("plan_id")
            stripe_sub_id = obj.get("subscription")
            if deployment_id and plan_id and stripe_sub_id:
                import uuid
                sub_id = f"sub_{uuid.uuid4().hex[:8]}"
                self.cloud_store.create_subscription(
                    subscription_id=sub_id,
                    deployment_id=deployment_id,
                    plan_id=plan_id,
                    stripe_sub_id=stripe_sub_id,
                    status="active",
                    current_period_end=obj.get("current_period_end", time.time() + 30 * 86400),
                )

                inv_id = f"inv_{uuid.uuid4().hex[:8]}"
                dep = self.cloud_store.get_deployment(deployment_id)
                customer_id = dep["customer_id"] if dep else "cust_1"
                amount = obj.get("amount_total", 0)
                self.cloud_store.create_invoice(
                    invoice_id=inv_id,
                    customer_id=customer_id,
                    amount_cents=amount,
                    subscription_id=sub_id,
                    status="paid",
                )
                self._write_json({"status": "ok", "message": "subscription activated"})
                return

        elif event_type == "customer.subscription.updated":
            obj = event.get("data", {}).get("object", {})
            stripe_sub_id = obj.get("id")
            status = obj.get("status")
            if stripe_sub_id and status:
                # Update subscription status in cloud_state.db
                with self.cloud_store._connect() as conn:
                    conn.execute(
                        "UPDATE subscriptions SET status=? WHERE stripe_sub_id=?",
                        (status, stripe_sub_id),
                    )
                self._write_json({"status": "ok", "message": f"subscription {status}"})
                return

        elif event_type == "customer.subscription.deleted":
            obj = event.get("data", {}).get("object", {})
            stripe_sub_id = obj.get("id")
            if stripe_sub_id:
                with self.cloud_store._connect() as conn:
                    conn.execute(
                        "UPDATE subscriptions SET status='canceled' WHERE stripe_sub_id=?",
                        (stripe_sub_id,),
                    )
                self._write_json({"status": "ok", "message": "subscription canceled"})
                return

        self._write_json({"status": "ignored"})

    def _handle_audit_logs(self, query_params: dict) -> None:
        """GET /api/v1/audit/logs — query memory_audit_log via REST.

        Params: hours (default 24), tool (filter), errors_only, limit (default 200)
        """
        try:
            from infra._lazy_imports import connection_pool, safe_close_db

            hours = int(query_params.get("hours", ["24"])[0])
            tool_filter = query_params.get("tool", [None])[0]
            errors_only = query_params.get("errors_only", ["false"])[0].lower() == "true"
            limit = int(query_params.get("limit", ["200"])[0])

            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                where = [f"ts >= strftime('%s','now')-{hours * 3600}"]
                params: list = []
                if tool_filter:
                    where.append("tool LIKE ?")
                    params.append(f"%{tool_filter}%")
                if errors_only:
                    where.append("error IS NOT NULL")
                where_sql = " AND ".join(where)

                rows = conn.execute(
                    f"SELECT ts, tool, latency_ms, results_count, error, args "
                    f"FROM memory_audit_log WHERE {where_sql} "
                    f"ORDER BY ts DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()

                logs = [
                    {
                        "ts": r[0], "tool": r[1], "latency_ms": r[2],
                        "results_count": r[3], "error": r[4], "args": r[5],
                    }
                    for r in rows
                ]
                self._write_json({"logs": logs, "count": len(logs)})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_audit_logs: %s", e)
            self._error(f"Audit query failed: {e}", 500)


class APIServer(ThreadingHTTPServer):
    """REST and WebSocket server wrapper."""

    def __init__(
        self,
        db_path: str | Path,
        agent_id: str,
        host: str = "127.0.0.1",
        port: int = 9878,
        token: str = "",
        insecure_loopback: bool = False,
    ):
        self.db_path = Path(db_path)
        self.agent_id = agent_id
        self.host = host
        self.port = port
        self.token = token
        self.insecure_loopback = insecure_loopback

        # Phase 2: per-IP sliding-window rate limit. Disabled when <= 0.
        # Configured via MEMORY_API_RATE_LIMIT (requests) and
        # MEMORY_API_RATE_WINDOW (seconds, default 60).
        self.rate_limit = int(os.environ.get("MEMORY_API_RATE_LIMIT", "0") or "0")
        self.rate_window = int(os.environ.get("MEMORY_API_RATE_WINDOW", "60") or "60")
        self._rate_buckets: Dict[str, list[float]] = {}
        self._rate_lock = threading.Lock()

        self._ws_clients: Dict[str, socket.socket] = {}

        # Phase 3: eagerly create cloud_state.db so the dashboard sidebar
        # and billing tab are available from first boot (not lazily on first
        # cloud API hit).
        try:
            from infra_cloud.store import CloudStateStore
            _cloud_db = Path(db_path).parent / "cloud_state.db"
            CloudStateStore(_cloud_db)
        except Exception as _cloud_exc:
            logger.debug("cloud_state.db init skipped: %s", _cloud_exc)
        self._ws_lock = threading.Lock()
        self._ws_send_lock = threading.Lock()
        
        self._thread: Optional[threading.Thread] = None
        self._outbox_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        super().__init__((host, port), APIRequestHandler)

    def register_ws_client(self, client_id: str, sock: socket.socket) -> None:
        with self._ws_lock:
            self._ws_clients[client_id] = sock

    def unregister_ws_client(self, client_id: str) -> None:
        with self._ws_lock:
            self._ws_clients.pop(client_id, None)

    def send_ws_message(self, sock: socket.socket, message: str) -> bool:
        """Thread-safe WebSocket message frame transmission."""
        try:
            payload = message.encode("utf-8")
            length = len(payload)
            header = bytearray([0x81])  # FIN=1, Opcode=1 (Text)
            if length <= 125:
                header.append(length)
            elif length <= 65535:
                header.append(126)
                header.extend(length.to_bytes(2, byteorder="big"))
            else:
                header.append(127)
                header.extend(length.to_bytes(8, byteorder="big"))
            
            with self._ws_send_lock:
                sock.sendall(bytes(header) + payload)
            return True
        except Exception as _wp_exc:
            logger.warning("send_ws_message: broad except swallowed: %s", _wp_exc)
            return False

    def broadcast(self, message: str) -> None:
        """Sends frame message to all active WebSocket clients."""
        with self._ws_lock:
            dead_clients = []
            for client_id, sock in self._ws_clients.items():
                ok = self.send_ws_message(sock, message)
                if not ok:
                    dead_clients.append(client_id)
            for c in dead_clients:
                self._ws_clients.pop(c, None)

    def start(self) -> None:
        """Launch server in background thread."""
        self._stop_event.clear()
        
        # Start SQLite Outbox Broadcaster
        self._outbox_thread = threading.Thread(target=self._outbox_loop, daemon=True)
        self._outbox_thread.start()
        
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"API Server running on http://{self.host}:{self.port}")

    def stop(self) -> None:
        """Gracefully shut down the server."""
        self._stop_event.set()
        self.shutdown()
        self.server_close()
        
        # Close all WebSocket client sockets
        with self._ws_lock:
            for sock in self._ws_clients.values():
                try:
                    sock.close()
                except Exception as _wp_exc:
                    logger.warning("stop: broad except swallowed: %s", _wp_exc)
                    pass
            self._ws_clients.clear()

        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._outbox_thread:
            self._outbox_thread.join(timeout=5.0)
            self._outbox_thread = None
        logger.info("API Server stopped")

    def _outbox_loop(self) -> None:
        """Polls SQLite memory_events outbox and broadcasts them."""
        last_seen_id = 0
        try:
            from infra._lazy_imports import connection_pool, safe_close_db

            conn = connection_pool.get(str(self.db_path), timeout=5.0)
            try:
                row = conn.execute("SELECT MAX(id) FROM memory_events").fetchone()
                if row and row[0] is not None:
                    last_seen_id = row[0]
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning(f"Failed to query max event id: {e}")

        while not self._stop_event.is_set():
            try:
                time.sleep(0.2)

                from infra._lazy_imports import connection_pool, safe_close_db

                conn = connection_pool.get(str(self.db_path), timeout=5.0)
                try:
                    rows = conn.execute(
                        "SELECT id, event_type, note_id, payload, created_at "
                        "FROM memory_events WHERE id > ? ORDER BY id ASC",
                        (last_seen_id,),
                    ).fetchall()

                    for row in rows:
                        event_id, event_type, note_id, payload_str, created_at = row
                        last_seen_id = event_id

                        try:
                            payload = json.loads(payload_str)
                        except Exception as _wp_exc:
                            logger.warning("_outbox_loop: broad except swallowed: %s", _wp_exc)
                            payload = payload_str

                        msg = json.dumps({
                            "event": "memory_event",
                            "data": {
                                "id": event_id,
                                "event_type": event_type,
                                "note_id": note_id,
                                "payload": payload,
                                "created_at": created_at,
                            },
                        })
                        self.broadcast(msg)
                finally:
                    safe_close_db(conn)
            except Exception as e:
                logger.debug(f"Outbox polling error: {e}")


def start_server_from_config(db_path: str | Path) -> Optional[APIServer]:
    """Create and start APIServer based on configuration settings."""
    from infra._lazy_imports import get_config
    cfg = get_config()
    if not getattr(cfg, "api_enable_server", False):
        return None

    from save_pipeline import _crdt_agent_id
    agent_id = _crdt_agent_id()
    server = APIServer(
        db_path=db_path,
        agent_id=agent_id,
        host=getattr(cfg, "api_listen_host", "127.0.0.1"),
        port=getattr(cfg, "api_listen_port", 9878),
        token=getattr(cfg, "api_token", ""),
        insecure_loopback=getattr(cfg, "api_insecure_loopback", False),
    )
    server.start()
    return server
