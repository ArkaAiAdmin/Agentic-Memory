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
import re
import socket
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs

from agentic_memory.client import MemoryClient

from infra.api_token import validate_api_token
from infra.db_migrations import SCHEMA_VERSION

logger = logging.getLogger(__name__)

try:
    from importlib.metadata import version as _get_version
    PACKAGE_VERSION = _get_version("agentic-memory")
except Exception:
    PACKAGE_VERSION = "1.1.0"

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

    def _cors_headers(self, origin: str) -> list[tuple[str, str]]:
        """Return CORS header tuples for the given origin."""
        headers = [
            ("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Idempotency-Key, Sec-WebSocket-Protocol, Sec-WebSocket-Key, Sec-WebSocket-Version"),
            ("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS"),
            ("Access-Control-Allow-Credentials", "true"),
        ]
        is_local = (
            origin.startswith("http://localhost")
            or origin.startswith("http://127.0.0.1")
            or origin.startswith("http://[::1]")
            or origin.startswith("tauri://")
            or origin.startswith("http://tauri.localhost")
            or origin.startswith("https://tauri.localhost")
            or origin.startswith("asset://")
        )
        if origin and (is_local or (API_CORS_ORIGINS and origin in API_CORS_ORIGINS)):
            headers.append(("Access-Control-Allow-Origin", origin))
        return headers

    def _write_json(self, data: dict | list, status_code: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        try:
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
            for hdr, val in self._cors_headers(origin):
                self.send_header(hdr, val)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
        self._tenant_id = None
        peer = self.client_address[0]
        if getattr(self.server, "insecure_loopback", False) and _is_loopback(peer):
            self._principal_id = getattr(self.server, "agent_id", "") or os.environ.get("MEMORY_AGENT_ID", "ami")
            self._tenant_id = "default"
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
                        self._principal = type("_Principal", (), {"id": pid, "tenant_id": claims.get("tenant_id", "default")})()
                        self._tenant_id = claims.get("tenant_id", "default")
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
        from infra.authorizer import timing_safe_compare
        if not timing_safe_compare(bearer, token):
            self._error("Invalid token", 401)
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
                self._tenant_id = getattr(principal, "tenant_id", None) or "default"
        except Exception:
            pass
        if not self._principal_id:
            self._principal_id = getattr(self.server, "agent_id", "") or os.environ.get("MEMORY_AGENT_ID", "ami")
            self._tenant_id = "default"
        return True

    def _require_auth_ws(self) -> bool:
        """Auth check for WebSocket upgrades.

        Phase 2: tries JWT validation first (SSO-issued tokens), then
        falls back to static token comparison via
        ``Sec-WebSocket-Protocol`` (browser clients) or
        ``Authorization: Bearer`` (programmatic clients).

        The ``Sec-WebSocket-Protocol`` subprotocol is the primary WS auth
        channel for the IDE webview: browser WebSockets cannot set custom
        headers, so the token travels in the RFC 6455 subprotocol
        negotiation and MUST be echoed in the 101 response or the browser
        aborts the handshake. Auth-in-URL (``?token=``) is deliberately
        not supported: it leaks credentials into access logs and proxies.

        The loopback auth bypass is gated behind ``insecure_loopback``.
        """
        self._principal = None
        self._principal_id = None
        peer = self.client_address[0]
        if getattr(self.server, "insecure_loopback", False) and _is_loopback(peer):
            return True

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
            if getattr(self.server, "insecure_loopback", False) and _is_loopback(peer):
                return True
            self._error(
                "Auth required: set MEMORY_API_TOKEN or start the server with "
                "insecure_loopback=True for local dev only",
                401,
            )
            return False

        from infra.authorizer import timing_safe_compare

        if auth.startswith("Bearer ") and timing_safe_compare(auth[7:], token):
            self._resolve_ws_principal(auth[7:])
            return True

        # Primary browser channel: RFC 6455 Sec-WebSocket-Protocol.
        ws_protocols = self.headers.get("Sec-WebSocket-Protocol", "")
        if ws_protocols:
            for candidate in (p.strip() for p in ws_protocols.split(",")):
                if timing_safe_compare(candidate, token):
                    self._resolve_ws_principal(candidate)
                    self._ws_subprotocol = candidate
                    return True
            # Fail closed: subprotocols were offered but none matched.
            self._error("Unauthorized: Sec-WebSocket-Protocol token mismatch", 401)
            return False
        self._error(
            "Unauthorized: provide token via Sec-WebSocket-Protocol or Authorization header",
            401,
        )
        return False

    def _resolve_ws_principal(self, raw_token: str) -> None:
        """Resolve principal from a WS bearer token and store on self."""
        from infra.authorizer import timing_safe_compare
        server_token = getattr(self.server, "token", "") or os.environ.get("MEMORY_API_TOKEN", "")
        if server_token and timing_safe_compare(raw_token, server_token):
            self._principal_id = "legacy"
            self._principal = None
            return
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
        for hdr, val in self._cors_headers(origin):
            self.send_header(hdr, val)
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
        elif path == "/api/v1/memories/categories":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_categories()
        elif path.startswith("/api/v1/memories/"):
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            note_id = path[len("/api/v1/memories/"):]
            self._handle_get_memory(note_id)
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
        elif path == "/api/v1/kg/explore":
            if not self._require_auth():
                return
            if self._rate_limited(key=getattr(self, "_principal_id", None)):
                self._error("Rate limit exceeded", 429)
                return
            self._handle_kg_explore(parse_qs(parsed.query))
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
        elif path == "/api/v1/tools/call":
            self._handle_tool_call()
        elif path == "/api/v1/memories/session/start":
            self._handle_session_start()
        elif path == "/api/v1/memories/session/end":
            self._handle_session_end()
        elif path.startswith("/api/v1/memories/") and path.endswith("/supersede"):
            note_id = path[len("/api/v1/memories/"):-len("/supersede")]
            self._handle_supersede_memory(note_id)
        elif path.startswith("/api/v1/memories/") and path.endswith("/restore"):
            note_id = path[len("/api/v1/memories/"):-len("/restore")]
            self._handle_restore_memory(note_id)
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

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if not self._require_auth():
            return

        if self._rate_limited(key=getattr(self, "_principal_id", None)):
            self._error("Rate limit exceeded", 429)
            return

        if path.startswith("/api/v1/memories/"):
            note_id = path[len("/api/v1/memories/"):]
            self._handle_update_memory(note_id)
        else:
            self._error("Not found", 404)

    # Handlers
    def _handle_health(self) -> None:
        note_count = 0
        db_ok = True
        err_msg = None
        db_path_str = Path(self.server.db_path).name
        journal_pending = 0
        dead_letter = 0
        try:
            client = MemoryClient(db_path=self.server.db_path)
            note_count = client.stats().memories
        except Exception as _wp_exc:
            logger.warning("_handle_health probe failed: %s", _wp_exc)
            db_ok = False
            err_msg = "Database health probe failed"

        # Inspect write_journal in journal.db if present
        try:
            journal_db = Path(self.server.db_path).parent / "journal.db"
            if journal_db.exists():
                import sqlite3
                with sqlite3.connect(str(journal_db), timeout=1.0) as jconn:
                    p_row = jconn.execute("SELECT count(*) FROM write_journal WHERE status = 'pending'").fetchone()
                    if p_row:
                        journal_pending = p_row[0]
                    f_row = jconn.execute("SELECT count(*) FROM write_journal WHERE status = 'failed'").fetchone()
                    if f_row:
                        dead_letter = f_row[0]
        except Exception as _j_exc:
            logger.debug("_handle_health journal inspect skipped: %s", _j_exc)

        if not db_ok:
            self._write_json({
                "status": "unhealthy",
                "package_version": PACKAGE_VERSION,
                "schema_version": SCHEMA_VERSION,
                "db_ok": False,
                "db_path": db_path_str,
                "journal_pending": journal_pending,
                "dead_letter": dead_letter,
                "error": err_msg,
            }, 503)
            return

        status = "degraded" if dead_letter > 0 else "healthy"
        self._write_json({
            "status": status,
            "package_version": PACKAGE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "db_ok": True,
            "db_path": db_path_str,
            "journal_pending": journal_pending,
            "dead_letter": dead_letter,
            "note_count": note_count,
        })

    def _handle_list_memories(self, query_params: dict) -> None:
        try:
            try:
                limit = int(query_params.get("limit", ["50"])[0])
                offset = int(query_params.get("offset", ["0"])[0])
            except (ValueError, TypeError, IndexError):
                self._error("Invalid limit or offset parameter", 400)
                return
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
            query = query_params.get("query", [""])[0] or query_params.get("q", [""])[0]
            if not query:
                self._error("Missing required query parameter", 400)
                return
            try:
                limit = int(query_params.get("limit", ["10"])[0])
            except (ValueError, TypeError, IndexError):
                self._error("Invalid limit parameter", 400)
                return
            rerank = query_params.get("rerank", ["false"])[0].lower() == "true"
            light_param = query_params.get("light", [None])[0]
            light = light_param.lower() == "true" if light_param else (not rerank)
            tags_str = query_params.get("tags", [None])[0]
            tags = tags_str.split(",") if tags_str else None
            # Default fts: sub-100ms BM25. Hybrid costs ~8s query-parse
            # (semantic expansion) + fusion, and auto-escalates to the
            # deep cross-encoder on reasoning-shaped queries (measured
            # 14-23s). Callers opt in explicitly with ?mode=hybrid.
            mode = query_params.get("mode", ["fts"])[0]
            with getattr(self.server, "_db_lock", threading.Lock()):
                client = MemoryClient(db_path=self.server.db_path)
                results = client.search(query, limit=limit, rerank=rerank, tags=tags, mode=mode, light=light)
            
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
            self._write_json({
                "results": results_list,
                "count": len(results_list),
                "query": query,
            })
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
            try:
                limit = int(req.get("limit", 10))
            except (ValueError, TypeError):
                self._error("Invalid limit field in request body", 400)
                return
            rerank = req.get("rerank", False)
            light = req.get("light", not rerank)
            tags = req.get("tags", None)
            # Default fts — see GET handler note above.
            mode = req.get("mode", "fts")
            include_global = req.get("include_global", True)
            with getattr(self.server, "_db_lock", threading.Lock()):
                client = MemoryClient(db_path=self.server.db_path)
                results = client.search(
                    query,
                    limit=limit,
                    rerank=rerank,
                    tags=tags,
                    mode=mode,
                    light=light,
                    include_global=include_global,
                )
            
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
            self._write_json({
                "results": results_list,
                "count": len(results_list),
                "query": query,
            })
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
                    "SELECT id, content, tags, category, created_at, updated_at, deleted_at, importance "
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
                    "importance": row[7],
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
        from infra.authorizer import timing_safe_compare
        legacy = getattr(self.server, "token", "") or os.environ.get("MEMORY_API_TOKEN", "")
        if legacy and timing_safe_compare(token, legacy):
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

        if principal_id is None and not (legacy and timing_safe_compare(token, legacy)):
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
            importance = 3
            if "importance" in req:
                try:
                    imp_val = int(req["importance"])
                    if not (1 <= imp_val <= 5):
                        raise ValueError()
                    importance = imp_val
                except (ValueError, TypeError):
                    self._write_json(
                        {
                            "error": "Error [INVALID_PARAMS]: importance must be an integer between 1 and 5",
                            "code": "INVALID_PARAMS",
                            "field": "importance",
                        },
                        400,
                    )
                    return
            title_slug = req.get("title_slug", "")

            # CHANGE 5: tenant write-path validation. The authenticated
            # principal's tenant is authoritative; it is threaded through to
            # the save pipeline so the row is scoped to the principal's tenant
            # (the pipeline refuses to re-derive a different tenant).
            _principal_tenant = "default"
            try:
                if getattr(self, "_principal", None) is not None:
                    _principal_tenant = getattr(
                        self._principal, "tenant_id", "default"
                    ) or "default"
            except Exception:
                pass

            # Idempotency support: check X-Idempotency-Key header or body field
            idempotency_key = (
                self.headers.get("X-Idempotency-Key", "").strip()
                or str(req.get("idempotency_key", "")).strip()
                or None
            )
            from infra.idempotency import get_idempotent_result, set_idempotent_result
            if idempotency_key:
                cached = get_idempotent_result(idempotency_key, tenant_id=_principal_tenant)
                if cached:
                    self._write_json(cached, 201)
                    return

            with getattr(self.server, "_db_lock", threading.Lock()):
                client = MemoryClient(db_path=self.server.db_path)
                note_id = client.save(
                    content=content,
                    tags=tags,
                    category=category,
                    is_global=is_global,
                    pinned=pinned,
                    importance=importance,
                    title_slug=title_slug,
                    tenant_id=_principal_tenant,
                )
            if isinstance(note_id, str):
                if note_id.startswith("Error [AUTHORIZATION_DENIED]"):
                    self._error(note_id, 403)
                    return
                elif note_id.startswith("Error ["):
                    self._error(note_id, 400)
                    return
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
            resp_payload = {"id": note_id, "status": "success"}
            if idempotency_key:
                set_idempotent_result(idempotency_key, resp_payload, tenant_id=_principal_tenant)
            self._write_json(resp_payload, 201)
        except (ValueError, TypeError) as e:
            self._error(f"Validation error: {e}", 400)
        except PermissionError as e:
            self._error(f"Access denied: {e}", 403)
        except Exception as e:
            logger.warning("_handle_add_memory error: %s", e)
            self._error(f"Failed to add memory: {e}", 500)

    def _handle_update_memory(self, note_id: str) -> None:
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            content = req.get("content")
            tags = req.get("tags")
            pinned = req.get("pinned")
            importance = None
            if "importance" in req and req["importance"] is not None:
                try:
                    imp_val = int(req["importance"])
                    if not (1 <= imp_val <= 5):
                        raise ValueError()
                    importance = imp_val
                except (ValueError, TypeError):
                    self._write_json(
                        {
                            "error": "Error [INVALID_PARAMS]: importance must be an integer between 1 and 5",
                            "code": "INVALID_PARAMS",
                            "field": "importance",
                        },
                        400,
                    )
                    return

            with getattr(self.server, "_db_lock", threading.Lock()):
                client = MemoryClient(db_path=self.server.db_path)
                client.update(
                    note_id,
                    content=content,
                    tags=tags,
                    pinned=pinned,
                    importance=importance,
                )
            # Audit: tag as dashboard REST call
            try:
                from infra.audit import enqueue_audit
                enqueue_audit(
                    db_path=str(self.server.db_path),
                    tool="dashboard_update",
                    args={"note_id": note_id, "tags": tags, "pinned": pinned},
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
            from infra.authorizer import mcp_authorize
            from agent_context import temporary_agent_context

            principal = getattr(self, "_principal", None)
            if principal is not None:
                principal_id = principal.id
                tenant_id = getattr(principal, "tenant_id", "default") or "default"
                if not mcp_authorize(principal_id, "delete", "memory", str(self.server.db_path), tenant_id=tenant_id):
                    self._error("Access denied: missing authorization to delete memory", 403)
                    return
            else:
                principal_id = (
                    getattr(self, "_principal_id", None)
                    or getattr(self.server, "agent_id", "")
                    or os.environ.get("MEMORY_AGENT_ID", "")
                    or "ami"
                )
                tenant_id = getattr(self, "_tenant_id", None) or "default"
            with temporary_agent_context(principal_id):
                client = MemoryClient(db_path=self.server.db_path)
                success = client.delete(note_id, tenant_id=tenant_id)
            if success:
                # Audit: tag as dashboard REST call
                try:
                    from infra.audit import enqueue_audit
                    enqueue_audit(
                        db_path=str(self.server.db_path),
                        tool="dashboard_delete",
                        args={"note_id": note_id},
                        results_count=1,
                        principal_id=principal_id,
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

            # Defeat stacked queries: mask string literals FIRST, then comments, then check for unquoted semicolons
            def _mask_literals_and_comments(s: str) -> str:
                # Mask string literals first so in-string comments like 'foo -- bar; baz' are not treated as comments
                out = re.sub(r"'(''|[^'])*'", lambda m: " " * len(m.group(0)), s)
                out = re.sub(r'"(""|[^"])*"', lambda m: " " * len(m.group(0)), out)
                out = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), out, flags=re.DOTALL)
                out = re.sub(r"--[^\r\n]*", lambda m: " " * len(m.group(0)), out)
                return out

            masked_sql = _mask_literals_and_comments(sql)
            if re.search(r";\s*\S", masked_sql):
                self._error("Only a single SQL statement is allowed", 400)
                return

            single_sql = sql.strip().rstrip(";").strip()

            # M52: Strict SELECT-only guard
            if not re.match(r"^\s*SELECT\b", single_sql, re.IGNORECASE):
                self._error("Only simple SELECT queries allowed", 403)
                return

            # Strip string literals so code-level keywords aren't matched inside literal values
            sql_code_only = re.sub(r"'(''|[^'])*'", "''", single_sql)
            sql_code_only = re.sub(r'"(""|[^"])*"', '""', sql_code_only)

            # Strip comments for keyword inspection
            sql_clean = re.sub(r"/\*.*?\*/", " ", sql_code_only, flags=re.DOTALL)
            sql_clean = re.sub(r"--[^\r\n]*", " ", sql_clean).strip()
            sql_no_comments = re.sub(r"/\*.*?\*/", "", sql_code_only, flags=re.DOTALL)
            sql_no_comments = re.sub(r"--[^\r\n]*", "", sql_no_comments).strip()

            # Token-aware keyword check using word boundaries
            _forbidden_keywords = (
                "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
                "REPLACE", "TRUNCATE", "ATTACH", "DETACH", "PRAGMA", "VACUUM",
                "REINDEX", "ANALYZE", "UNION", "INTERSECT", "EXCEPT", "INTO",
            )
            for kw in _forbidden_keywords:
                if (re.search(rf"\b{kw}\b", sql_clean, re.IGNORECASE) or
                        re.search(rf"\b{kw}\b", sql_no_comments, re.IGNORECASE)):
                    self._error(f"SQL statement contains disallowed keyword: {kw}", 403)
                    return

            # Block PRAGMA functions (e.g. pragma_table_info) to prevent schema inspection bypasses
            if (re.search(r"\bpragma_[a-zA-Z0-9_]+\b", sql_clean, re.IGNORECASE) or
                    re.search(r"\bpragma_[a-zA-Z0-9_]+\b", sql_no_comments, re.IGNORECASE)):
                self._error("Access to PRAGMA functions is forbidden", 403)
                return

            # System, security, and internal state tables blocked from read-queries
            _blocked_tables = {
                "sqlite_master", "sqlite_schema", "sqlite_temp_master",
                "principals", "principal_identities", "principal_roles_audit",
                "roles", "role_bindings", "policies", "acl_overrides",
                "api_tokens", "users", "tenants", "secrets",
                "idem_token_key", "sso_idp_cache",
                "saga_log", "saga_audit_log",
                "file_locks", "system_locks", "distributed_locks",
                "coordination_audit", "memory_audit_log",
            }
            for tbl in _blocked_tables:
                if (re.search(rf"\b{tbl}\b", sql_clean, re.IGNORECASE) or
                        re.search(rf"\b{tbl}\b", sql_no_comments, re.IGNORECASE)):
                    self._error("Access to system or sensitive tables is forbidden", 403)
                    return

            params = req.get("params", [])

            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            created_views: list[str] = []
            try:
                import sqlite3
                from infra.authorizer import _is_cross_tenant_admin
                principal_id = getattr(self, "_principal_id", None)
                is_admin = bool(principal_id and _is_cross_tenant_admin(conn, principal_id))

                _TENANT_TABLES = {
                    "memories", "memory_chunks", "memory_embeddings", "chunks",
                    "kg_entities", "kg_edges", "kg_facts", "facts",
                    "sessions", "decision_threads", "thread_events",
                    "belief_assertions", "entailment_chains", "memory_skills",
                    "shared_memories", "shared_tasks", "agent_messages",
                }

                # High-severity defense: pre-drop any stale temporary views on pooled connection
                # to prevent cross-tenant view leaks across pooled connection reuse
                for tbl in _TENANT_TABLES:
                    try:
                        conn.execute(f"DROP VIEW IF EXISTS temp.{tbl}")
                    except Exception as drop_err:
                        logger.warning("Failed to drop pre-existing temp view %s: %s", tbl, drop_err)

                if not is_admin:
                    tenant_id = getattr(self, "_tenant_id", None) or "default"
                    if not re.match(r"^[a-zA-Z0-9_\-]+$", tenant_id):
                        self._error("Invalid tenant ID format", 400)
                        return
                    existing_tables = {
                        r[0].lower() for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    for tbl in _TENANT_TABLES:
                        if tbl in existing_tables:
                            conn.execute(
                                f"CREATE TEMP VIEW {tbl} AS "
                                f"SELECT * FROM main.{tbl} WHERE tenant_id = '{tenant_id}'"
                            )
                            created_views.append(tbl)

                def _query_authorizer(action, arg1, arg2, dbname, source):
                    if action not in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
                        return sqlite3.SQLITE_DENY
                    # Block PRAGMA table-valued and scalar functions
                    if (arg1 and isinstance(arg1, str) and arg1.lower().startswith("pragma_")) or \
                       (arg2 and isinstance(arg2, str) and arg2.lower().startswith("pragma_")):
                        return sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and isinstance(arg1, str) and arg1.lower() in _blocked_tables:
                        return sqlite3.SQLITE_DENY
                    # Case-insensitive tenant-table check to defeat case variations (e.g. main.MEMORIES, MAIN.memories)
                    if not is_admin and action == sqlite3.SQLITE_READ and isinstance(dbname, str) and dbname.lower() == "main" and isinstance(arg1, str) and arg1.lower() in _TENANT_TABLES:
                        if not isinstance(source, str) or source.lower() != arg1.lower():
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                conn.set_authorizer(_query_authorizer)

                cursor = conn.execute(single_sql, params)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                results = [dict(zip(columns, row)) for row in rows]
                self._write_json({"results": results, "count": len(results)})
            finally:
                try:
                    conn.set_authorizer(None)
                except Exception as auth_err:
                    logger.warning("Failed to reset query authorizer: %s", auth_err)
                for tbl in created_views:
                    try:
                        conn.execute(f"DROP VIEW IF EXISTS temp.{tbl}")
                    except Exception as drop_err:
                        logger.warning("Failed to drop temp view %s in cleanup: %s", tbl, drop_err)
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_query failed: %s", e)
            self._error("Query execution failed", 500)

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

    def _require_rbac_admin(self) -> bool:
        """CHANGE 4: gate mutating RBAC/ACL endpoints behind an admin authz
        check. Returns True when the caller is authorized to mutate RBAC/ACL
        state, False otherwise (and writes a 403 response)."""
        principal_id = getattr(self, "_principal_id", None)
        tenant_id = "default"
        try:
            if getattr(self, "_principal", None) is not None:
                tenant_id = getattr(self._principal, "tenant_id", "default") or "default"
        except Exception:
            pass
        try:
            from infra.authorizer import mcp_authorize

            # Authorize against the server's own DB (where RBAC tables live),
            # not the local memory dir — in tests and multi-DB deployments the
            # two can differ.
            auth_db = str(self.server.db_path)
            # RBAC/ACL administration is the control plane. A principal with
            # either the memory or operational super-admin role is permitted
            # (the default seed provides ``memory:admin`` and ``ops:admin``,
            # both of which carry the ``admin`` action on their resource).
            _allowed = mcp_authorize(
                principal_id, "admin", "memory", auth_db, tenant_id=tenant_id
            ) or mcp_authorize(
                principal_id, "admin", "ops", auth_db, tenant_id=tenant_id
            )
            if not _allowed:
                self._error(
                    "Forbidden: RBAC/ACL administration requires an admin role",
                    403,
                )
                return False
        except ImportError:
            # Authorizer unavailable: fail closed on admin endpoints.
            self._error("Forbidden: authorization subsystem unavailable", 403)
            return False
        return True

    def _handle_rbac_init(self) -> None:
        """POST /api/v1/rbac/init — seed default RBAC roles.

        CHANGE 4: bootstrap is allowed without an admin role ONLY when no
        principals exist yet (first-run setup). Once any principal exists,
        init requires admin authz like every other RBAC mutation.
        """
        try:
            from infra._lazy_imports import connection_pool, safe_close_db

            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                existing = conn.execute(
                    "SELECT COUNT(*) FROM principals"
                ).fetchone()
                has_principals = existing and existing[0] > 0
            finally:
                safe_close_db(conn)
            if has_principals and not self._require_rbac_admin():
                return
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
            if not self._require_rbac_admin():
                return
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
            if not self._require_rbac_admin():
                return
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
            if not self._require_rbac_admin():
                return
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
            if not self._require_rbac_admin():
                return
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
            if not self._require_rbac_admin():
                return
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
            if not self._require_rbac_admin():
                return
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
            try:
                raw_limit = int(query_params.get("limit", ["100"])[0])
                limit = max(1, min(raw_limit, 500))
            except (ValueError, TypeError):
                limit = 100
            from infra._lazy_imports import connection_pool, safe_close_db
            conn = connection_pool.get(str(self.server.db_path), timeout=5.0)
            try:
                rows = conn.execute(
                    "SELECT id, name, entity_type FROM kg_entities LIMIT ?",
                    (limit,),
                ).fetchall()
                nodes = [
                    {
                        "id": r[0],
                        "name": r[1],
                        "type": r[2] or "entity",
                        "properties": {}
                    }
                    for r in rows
                ]
                self._write_json({"nodes": nodes})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_kg_nodes: %s", e)
            self._error("Failed to list KG nodes", 500)

    def _handle_kg_edges(self, query_params: dict) -> None:
        try:
            try:
                raw_limit = int(query_params.get("limit", ["100"])[0])
                limit = max(1, min(raw_limit, 500))
            except (ValueError, TypeError):
                limit = 100
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
            logger.warning("_handle_kg_edges: %s", e)
            self._error("Failed to list KG edges", 500)

    def _handle_kg_create_edge(self) -> None:
        """Create a KG edge via the authenticated API (dashboard gap-detector)."""
        try:
            body = self._read_json_body() or {}
            source_id = body.get("source_id")
            target_id = body.get("target_id")
            relation = body.get("relation") or "related"
            weight = float(body.get("weight", 1.0))
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
        """Delete a KG entity by id.

        Coordinated write through ``open_db`` (file lock first, then conn —
        Hard Rule 9). After the raw delete we run ``repair_kg_orphans`` so the
        dependent ``kg_edges`` / ``kg_entities`` / ``backlinks`` rows left
        behind are removed — the same cleanup the saga rollback path performs
        (memory_integrity.repair_kg_orphans). repair runs after the write
        connection is released to avoid a nested open.
        """
        if not entity_id:
            self._error("entity id required", 400)
            return
        try:
            from infra.db import open_db
            from memory_integrity import repair_kg_orphans
            db_path = Path(str(self.server.db_path))
            with open_db(db_path, write=True) as conn:
                conn.execute(
                    "DELETE FROM kg_edges WHERE source_id=? OR target_id=?",
                    (entity_id, entity_id),
                )
                cur = conn.execute(
                    "DELETE FROM kg_entities WHERE id=?", (entity_id,)
                )
                conn.commit()
                if cur.rowcount == 0:
                    self._error(f"KG entity not found: {entity_id}", 404)
                    return
            # Drop orphaned dependent rows so the KG stays consistent.
            repair_kg_orphans(db_path)
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
            from memory_integrity import repair_kg_orphans
            db_path = Path(str(self.server.db_path))
            with open_db(db_path, write=True) as conn:
                cur = conn.execute("DELETE FROM kg_edges WHERE id=?", (edge_id,))
                conn.commit()
                if cur.rowcount == 0:
                    self._error(f"KG edge not found: {edge_id}", 404)
                    return
            repair_kg_orphans(db_path)
            self._write_json({"status": "deleted", "id": edge_id})
        except Exception as e:
            logger.warning("_handle_delete_kg_edge: %s", e)
            self._error(f"Failed to delete KG edge: {e}", 500)

    def _handle_kg_dedup(self) -> None:
        try:
            from infra.db import open_db
            from memory_integrity import repair_kg_orphans
            db_path = Path(str(self.server.db_path))
            with open_db(db_path, write=True) as conn:
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
            # Drop any rows orphaned by the dedup (Rule 1: saga-equivalent cleanup).
            repair_kg_orphans(db_path)
            self._write_json({"merged": merged, "status": "ok"})
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
            from memory_integrity import repair_kg_orphans
            db_path = Path(str(self.server.db_path))
            with open_db(db_path, write=True) as conn:
                ph = ",".join("?" for _ in entity_ids)
                conn.execute(f"DELETE FROM kg_entities WHERE id IN ({ph})", entity_ids)
                conn.execute(
                    f"DELETE FROM kg_edges WHERE source_id IN ({ph}) OR target_id IN ({ph})",
                    entity_ids + entity_ids,
                )
                conn.commit()
            # Drop any dependent rows left orphaned by the prune.
            repair_kg_orphans(db_path)
            self._write_json({"pruned": len(entity_ids)})
        except Exception as e:
            logger.warning("_handle_kg_prune: %s", e)
            self._error(f"Failed to prune KG entities: {e}", 500)

    def _handle_kg_merge(self) -> None:
        """Merge remove_id into keep_id: reassign edges, sum mentions, delete remove_id.

        Ports the exact SQL semantics from dashboard/tab_knowledge.py _merge_entities.
        KG mutations use ``open_db`` (file lock first, then conn — Hard Rule 9) and
        run ``repair_kg_orphans`` afterward so the single-writer invariant and
        orphan cleanup hold (the same cleanup the saga rollback path performs).
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
            from memory_integrity import repair_kg_orphans
            db_path = Path(str(self.server.db_path))
            with open_db(db_path, write=True) as conn:
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
                    keep_row = conn.execute(
                        "SELECT id FROM kg_edges WHERE source_id=? AND target_id=? AND relation=? AND id!=?",
                        (
                            keep_id if row[1] == remove_id else row[1],
                            keep_id if row[2] == remove_id else row[2],
                            row[3],
                            remove_edge_id,
                        ),
                    ).fetchone()
                    if keep_row is None:
                        # Keep edge vanished concurrently; skip the weight merge.
                        conn.execute("DELETE FROM kg_edges WHERE id=?", (remove_edge_id,))
                        continue
                    keep_edge_id = keep_row[0]
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
            # Drop orphaned rows left by the merge (Rule 1: saga-equivalent cleanup).
            repair_kg_orphans(db_path)
        except Exception as e:
            logger.warning("_handle_kg_merge: %s", e)
            self._error(f"Failed to merge KG entities: {e}", 500)

    def _handle_archive_stale(self) -> None:
        try:
            body = self._read_json_body() or {}
            min_fitness = float(body.get("min_fitness", 0.3))
            min_age_days = int(body.get("min_age_days", 90))
            from infra.db import open_db
            from memory_integrity import repair_kg_orphans
            from save.cleanup import cleanup_memory_relations
            db_path = Path(str(self.server.db_path))
            with open_db(db_path, write=True) as conn:
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
                        # Clean dependent rows (kg_facts/backlinks) the saga way
                        # before removing the memory, so the KG stays consistent.
                        cleanup_memory_relations(conn, mid)
                        col_names = [c[1] for c in cols]
                        placeholders = ",".join("?" for _ in col_names)
                        conn.execute(
                            f"INSERT OR IGNORE INTO memory_archive ({','.join(col_names)}) VALUES ({placeholders})",
                            row,
                        )
                        conn.execute("DELETE FROM memories WHERE id=?", (mid,))
                        archived += 1
                conn.commit()
            # Drop any KG rows orphaned by the archived memories.
            repair_kg_orphans(db_path)
            self._write_json({"archived": archived, "status": "ok"})
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
        origin = self.headers.get("Origin", "")
        if origin:
            is_local = (
                origin.startswith("http://localhost")
                or origin.startswith("http://127.0.0.1")
                or origin.startswith("http://[::1]")
                or origin.startswith("tauri://")
                or origin.startswith("http://tauri.localhost")
                or origin.startswith("https://tauri.localhost")
                or origin.startswith("asset://")
            )
            if not is_local and (not API_CORS_ORIGINS or origin not in API_CORS_ORIGINS):
                self.send_error(403, "Cross-origin WebSocket connections rejected")
                return
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
        # RFC 6455 §4.2.2: when the client offered subprotocols, the server
        # MUST select one and echo it, or the browser fails the handshake.
        # The subprotocol carries the auth token, so this is also the point
        # at which the authenticated channel is established.
        if getattr(self, "_ws_subprotocol", None):
            self.send_header("Sec-WebSocket-Protocol", self._ws_subprotocol)
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception:
            pass

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
                    try:
                        # Send close frame reply
                        close_frame = bytearray([0x88, 0x00])
                        sock.sendall(bytes(close_frame))
                    except Exception:
                        pass
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
                        req_id = req.get("id")
                        if action == "ping":
                            resp: dict[str, Any] = {"event": "pong"}
                            if req_id is not None:
                                resp["id"] = req_id
                            self.server.send_ws_message(sock, json.dumps(resp))
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
                            resp = {
                                "event": "search_result",
                                "query": query,
                                "results": results_list,
                            }
                            if req_id is not None:
                                resp["id"] = req_id
                            self.server.send_ws_message(sock, json.dumps(resp))
                        else:
                            resp = {
                                "event": "error",
                                "error": f"Unknown action: {action}",
                            }
                            if req_id is not None:
                                resp["id"] = req_id
                            self.server.send_ws_message(sock, json.dumps(resp))
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
        if self.server._cloud_store is None:
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

            checkout_kwargs: dict[str, Any] = {
                "mode": "subscription",
                "line_items": [{"price": price_id, "quantity": 1}],
                "client_reference_id": dep_id,
                "metadata": {"plan_id": plan_id, "deployment_id": dep_id},
                "success_url": body.get("success_url", "https://app.agentic-memory.dev/billing?success=1"),
                "cancel_url": body.get("cancel_url", "https://app.agentic-memory.dev/billing?canceled=1"),
            }
            if customer_email:
                checkout_kwargs["customer_email"] = customer_email

            session = stripe.checkout.Session.create(**checkout_kwargs)

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
                    from infra.db import open_db
                    from infra.migration_runner import run_migrations
                    with open_db(Path(mem_db_path), write=True) as conn:
                        run_migrations(conn)
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
        sig_header = self.headers.get("Stripe-Signature", "")

        if not webhook_secret:
            logger.error("Stripe webhook secret not configured")
            self._error("Webhook secret not configured", 500)
            return
        if not sig_header:
            self._error("Missing Stripe-Signature header", 400)
            return
        else:
            try:
                event = stripe.Webhook.construct_event(
                    raw_body, sig_header, webhook_secret,
                )
            except (stripe.error.SignatureVerificationError, ValueError) as e:
                logger.warning("Stripe webhook signature verification failed: %s", e)
                self._error("Invalid webhook signature", 400)
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

    # ── New REST endpoints for IDE transport ─────────────────────────────────

    def _handle_kg_explore(self, query_params: dict) -> None:
        """GET /api/v1/kg/explore — explore the knowledge graph."""
        try:
            query = query_params.get("query", [""])[0]
            action = query_params.get("action", ["explore"])[0]
            start = query_params.get("start", [""])[0]
            edge_patterns = query_params.get("edge_patterns", [""])[0]
            try:
                raw_depth = int(query_params.get("max_depth", ["2"])[0])
                max_depth = max(1, min(raw_depth, 10))
            except (ValueError, TypeError):
                max_depth = 2

            from mcp_surface.mcp_kg import memory_facts_list, memory_graph_stats
            from mcp_surface.mcp_kg_traversal import memory_graph_traverse
            from mcp_surface.mcp_kg import memory_graph_search

            if action == "explore":
                facts = memory_facts_list(limit=20)
                stats = memory_graph_stats()
                result_text = f"## KG Facts\n{facts}\n\n## Stats\n{stats}"
            elif action == "search" and query:
                result_text = memory_graph_search(query=query, limit=10, max_hops=max_depth)
            elif action == "traverse" and start:
                result_text = memory_graph_traverse(start=start, edge_patterns=edge_patterns)
            elif action == "stats":
                result_text = memory_graph_stats()
            else:
                self._error("Invalid KG explore parameters", 400)
                return

            self._write_json({"result": result_text})
        except Exception as e:
            logger.warning("_handle_kg_explore: %s", e)
            self._error("KG explore failed", 500)

    _EXPLICIT_READ_TOOLS = frozenset({
        "memory_search", "memory_semantic_search", "search_memories",
        "memory_recall_context", "memory_recall_stats",
        "memory_facts_list", "memory_facts_search", "memory_facts_stats",
        "memory_graph_search", "memory_graph_shortest_path", "memory_graph_stats",
        "memory_graph_traverse", "memory_graph_insights", "memory_graph_evolution",
        "memory_audit", "memory_audit_query",
        "memory_list_skills", "memory_list_federated_skills", "memory_list_revisions",
        "memory_list_threads", "memory_list_drift_alarms",
        "memory_profile_stats", "memory_profile_access", "memory_user_profile",
        "memory_shared_list", "memory_shared_stats",
        "memory_arc_stats", "memory_crdt_status",
        "memory_auto_save_status", "memory_auto_save_daemon_metrics",
        "memory_background_task_status", "memory_circuit_breaker_status",
        "memory_health_check", "memory_system_health", "memory_heartbeat",
        "memory_check_concept_drift", "memory_check_contradictions",
        "memory_check_embedding_model", "memory_check_integrity",
        "memory_compliance_check", "memory_pinned_decay_check",
        "memory_detect_contradictions", "memory_temporal_contradictions",
        "memory_daily_digest", "memory_dashboard",
        "memory_okf_export", "memory_quality_stats", "memory_quality_filter",
        "memory_retention_stats", "memory_review_schedule", "memory_review_beliefs",
        "memory_scan_injection", "memory_sdk_demo",
        "memory_session_admin_stats", "memory_summarization_stats", "memory_summarize",
        "memory_temporal_query", "memory_thread_context", "memory_tier_stats",
        "memory_whoami", "memory_admin_policy_hash", "memory_login_url",
        "memory_sso_idp_list", "memory_agent_list", "memory_pipeline_coverage",
    })

    @classmethod
    def _classify_tool_action(cls, tool_name: str, args: dict | None = None) -> str:
        """Classify tool execution as 'read' or 'write' for RBAC authorization."""
        args = args or {}
        if tool_name in cls._EXPLICIT_READ_TOOLS:
            return "read"
        if tool_name in ("memory_graph", "memory_share", "memory_profile", "memory_skills",
                         "memory_note", "memory_coordinate", "memory_curate_autosave",
                         "memory_metrics_server"):
            raw_act = args.get("action")
            act = str(raw_act).strip().lower() if raw_act is not None else ""
            if not act:
                # Canonical parameter defaults from MCP tool signatures
                _defaults = {
                    "memory_graph": "explore",
                    "memory_share": "list",
                    "memory_profile": "stats",
                    "memory_skills": "list",
                    "memory_note": "read",
                    "memory_coordinate": "get_project_state",
                    "memory_curate_autosave": "list",
                    "memory_metrics_server": "status",
                }
                act = _defaults.get(tool_name, "")

            if tool_name == "memory_graph" and act in ("explore", "search", "traverse", "shortest_path", "insights", "stats", "evolution"):
                return "read"
            if tool_name == "memory_share" and act in ("list", "status", "stats"):
                return "read"
            if tool_name == "memory_profile" and act in ("stats", "user", "agents", "skills", "arc", "get", "read", "view"):
                return "read"
            if tool_name == "memory_skills" and act in ("list", "get", "view", "search"):
                return "read"
            if tool_name == "memory_note" and act in ("read", "get", "view"):
                return "read"
            if tool_name == "memory_coordinate" and act in ("get_project_state", "list_tasks", "check_lock", "read_messages"):
                return "read"
            if tool_name == "memory_curate_autosave" and act in ("list", "read", "view"):
                return "read"
            if tool_name == "memory_metrics_server" and act in ("status", "check", "get"):
                return "read"
            return "write"
        if tool_name in ("memory_adaptive_retention", "memory_auto_summarize"):
            # Strict boolean check — "true"/1/[1] must not escalate to reader
            if args.get("dry_run") is True:
                return "read"
            return "write"
        _write_patterns = (
            "save", "update", "delete", "organize", "drop", "rollback",
            "edit", "record", "create", "note", "supersede", "restore",
            "share", "coordinate", "curate", "profile", "skills",
            "crdt", "rotate", "purge", "trash", "compact", "rebuild",
            "backfill", "consolidate", "dedup", "maintenance", "advanced",
            "init", "clear", "reset", "hook", "compile", "ingest",
            "reinforce", "resolve", "rewrite", "sync", "import", "add",
            "strip", "migration", "adaptive",
        )
        if any(w in tool_name for w in _write_patterns):
            return "write"
        return "write"

    def _handle_tool_call(self) -> None:
        """POST /api/v1/tools/call — generic tool dispatch passthrough."""
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            tool_name = req.get("tool", "")
            args = req.get("arguments") or req.get("args") or {}
            if not tool_name or not tool_name.startswith("memory_"):
                self._error("Invalid or missing tool name", 400)
                return
            if not isinstance(args, dict):
                self._error("args must be a dict", 400)
                return

            import mcp_surface.mcp_tools as tools

            tool_fn = getattr(tools, tool_name, None)
            if tool_fn is None:
                self._error(f"Unknown tool: {tool_name}", 404)
                return
            import inspect
            sig = inspect.signature(tool_fn)
            has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if has_var_keyword:
                valid_args = args
            else:
                valid_args = {k: v for k, v in args.items() if k in sig.parameters}

            principal_id = getattr(self, "_principal_id", None)
            tenant_id = getattr(self, "_tenant_id", "default")
            action = self._classify_tool_action(tool_name, valid_args)
            from infra.authorizer import mcp_authorize, log_authorization_decision
            allowed = mcp_authorize(
                principal_id=principal_id,
                action=action,
                resource="memory",
                db_path=str(self.server.db_path),
                tenant_id=tenant_id,
            )
            if not allowed:
                log_authorization_decision(
                    principal_id=principal_id,
                    action=action,
                    resource="memory",
                    allowed=False,
                    db_path=str(self.server.db_path),
                )
                self._error(f"Principal '{principal_id or 'anonymous'}' not authorized for '{action}' on 'memory'", 403)
                return

            result = tool_fn(**valid_args)
            if isinstance(result, str):
                if result.startswith("Error [AUTHORIZATION_DENIED]"):
                    self._error(result, 403)
                    return
                elif result.startswith("Error ["):
                    self._error(result, 400)
                    return
            try:
                parsed = json.loads(result) if isinstance(result, str) else result
            except (json.JSONDecodeError, TypeError):
                parsed = {"result": str(result)}

            self._write_json({"tool": tool_name, "result": parsed})
        except (TypeError, ValueError) as e:
            self._error(f"Tool argument error: {e}", 400)
        except PermissionError as e:
            self._error(f"Access denied: {e}", 403)
        except Exception as e:
            logger.warning("_handle_tool_call: %s", e)
            self._error(f"Tool call failed: {e}", 500)

    def _handle_session_start(self) -> None:
        """POST /api/v1/memories/session/start — start a session."""
        try:
            req = self._read_json_body()
        except ValueError:
            req = {}
        try:
            from mcp_surface.mcp_search import memory_session_start

            query = req.get("query", "")
            result = memory_session_start(query=query)
            try:
                parsed = json.loads(result) if isinstance(result, str) else result
            except (json.JSONDecodeError, TypeError):
                parsed = {"briefing": str(result)}
            self._write_json({"result": parsed})
        except Exception as e:
            logger.warning("_handle_session_start: %s", e)
            self._error(f"Session start failed: {e}", 500)

    def _handle_session_end(self) -> None:
        """POST /api/v1/memories/session/end — end a session."""
        try:
            req = self._read_json_body()
        except ValueError:
            req = {}
        try:
            from mcp_surface.mcp_tools import memory_session_end

            session_id = req.get("session_id", "")
            summary = req.get("summary", "")
            result = memory_session_end(session_id=session_id, summary=summary)
            try:
                parsed = json.loads(result) if isinstance(result, str) else result
            except (json.JSONDecodeError, TypeError):
                parsed = {"result": str(result)}
            self._write_json({"result": parsed})
        except Exception as e:
            logger.warning("_handle_session_end: %s", e)
            self._error(f"Session end failed: {e}", 500)

    def _handle_supersede_memory(self, note_id: str) -> None:
        """POST /api/v1/memories/{id}/supersede — supersede a memory."""
        try:
            req = self._read_json_body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            rationale = req.get("rationale", "")
            title_slug = req.get("title_slug", "")
            content = req.get("content", "")

            if not rationale:
                self._error("rationale is required for supersede", 400)
                return

            from mcp_surface.mcp_tools import memory_note

            result = memory_note(
                note_id=note_id,
                action="supersede",
                rationale=rationale,
                title_slug=title_slug or "",
                content=content,
                category=req.get("category", ""),
                tags=req.get("tags", None),
            )
            try:
                parsed = json.loads(result) if isinstance(result, str) else result
            except (json.JSONDecodeError, TypeError):
                parsed = {"result": str(result)}
            self._write_json({"note_id": note_id, "result": parsed})
        except Exception as e:
            logger.warning("_handle_supersede_memory: %s", e)
            self._error(f"Supersede failed: {e}", 500)

    def _handle_restore_memory(self, note_id: str) -> None:
        """POST /api/v1/memories/{id}/restore — restore a soft-deleted memory."""
        try:
            req = self._read_json_body()
        except ValueError:
            req = {}
        try:
            rationale = req.get("rationale", "Restored via API")

            from mcp_surface.mcp_tools import memory_note

            result = memory_note(
                note_id=note_id,
                action="restore",
                rationale=rationale,
            )
            try:
                parsed = json.loads(result) if isinstance(result, str) else result
            except (json.JSONDecodeError, TypeError):
                parsed = {"result": str(result)}
            self._write_json({"note_id": note_id, "result": parsed})
        except Exception as e:
            logger.warning("_handle_restore_memory: %s", e)
            self._error(f"Restore failed: {e}", 500)


def _write_kernel_discovery(port: int, token: str, pid: int) -> Optional[Path]:
    try:
        from infra.memory_config import get_memory_home
        home = get_memory_home()
        runtime_dir = home / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        discovery_file = runtime_dir / "kernel.json"

        payload = {
            "port": port,
            "token": token,
            "pid": pid,
            "package_version": PACKAGE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "started_at": int(time.time()),
        }

        tmp_file = runtime_dir / f".kernel_{pid}_{int(time.time())}.tmp"
        tmp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            tmp_file.chmod(0o600)
        except Exception:
            pass
        tmp_file.replace(discovery_file)
        try:
            discovery_file.chmod(0o600)
        except Exception:
            pass
        return discovery_file
    except Exception as e:
        logger.warning("Failed to write kernel discovery file: %s", e)
        return None


def _remove_kernel_discovery(pid: Optional[int] = None) -> None:
    try:
        from infra.memory_config import get_memory_home
        home = get_memory_home()
        discovery_file = home / "runtime" / "kernel.json"
        if discovery_file.exists():
            if pid is not None:
                try:
                    data = json.loads(discovery_file.read_text(encoding="utf-8"))
                    if data.get("pid") != pid:
                        return
                except Exception:
                    pass
            discovery_file.unlink(missing_ok=True)
    except Exception:
        pass


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
        self._discovery_file: Optional[Path] = None

        if self.token and not validate_api_token(self.token):
            strict_token = os.environ.get("MEMORY_API_STRICT_TOKEN", "").lower() in ("1", "true", "yes", "on")
            if strict_token:
                raise ValueError(
                    "api_server: API token does not meet minimum security requirements "
                    "(length >= 32, URL-safe chars) and MEMORY_API_STRICT_TOKEN=1 is set. "
                    "Refusing to start."
                )
            logger.warning(
                "api_server: API token does not meet minimum security requirements "
                "(length >= 32, URL-safe chars). Replace MEMORY_API_TOKEN with a "
                "strong random value for production."
            )

        # Phase 2: per-IP sliding-window rate limit. Disabled when <= 0.
        # Configured via MEMORY_API_RATE_LIMIT (requests) and
        # MEMORY_API_RATE_WINDOW (seconds, default 60). Defaults to 600 req/min (10 req/s).
        self.rate_limit = int(os.environ.get("MEMORY_API_RATE_LIMIT", "600") or "600")
        self.rate_window = int(os.environ.get("MEMORY_API_RATE_WINDOW", "60") or "60")
        self._rate_buckets: Dict[str, list[float]] = {}
        self._rate_lock = threading.Lock()

        self._ws_clients: Dict[str, socket.socket] = {}

        # Lazily-initialized cloud state store (see the cloud_store property
        # on the request handler). Declared here for type-checking.
        self._cloud_store: Any = None

        # Phase 3: eagerly create cloud_state.db so the dashboard sidebar
        # and billing tab are available from first boot (not lazily on first
        # cloud API hit).
        try:
            from infra_cloud.store import CloudStateStore
            _cloud_db = Path(db_path).parent / "cloud_state.db"
            CloudStateStore(_cloud_db)
        except Exception as _cloud_exc:
            logger.debug("cloud_state.db init skipped: %s", _cloud_exc)

        # Persist the API token to .api_token so sync_client can resolve it
        # even when MEMORY_API_TOKEN is not in the process environment.
        if self.token:
            try:
                _token_file = Path(db_path).parent / ".api_token"
                _token_file.write_text(self.token)
                _token_file.chmod(0o600)
            except Exception:
                pass

        self._ws_lock = threading.Lock()
        self._ws_send_lock = threading.Lock()
        self._db_lock = threading.Lock()
        
        self._thread: Optional[threading.Thread] = None
        self._outbox_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.allow_reuse_address = True
        super().__init__((host, port), APIRequestHandler)
        # Update self.port with actual assigned port (in case port was 0 / ephemeral)
        self.port = self.server_address[1]

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
        
        # Write runtime discovery file for dynamic port allocation
        self._discovery_file = _write_kernel_discovery(self.port, self.token, os.getpid())

        import atexit
        import signal
        import sys

        atexit.register(_remove_kernel_discovery, os.getpid())

        def _sig_cleanup(signum: int, frame: Any) -> None:
            _remove_kernel_discovery(os.getpid())
            sys.exit(0)

        try:
            signal.signal(signal.SIGTERM, _sig_cleanup)
            signal.signal(signal.SIGINT, _sig_cleanup)
        except (ValueError, RuntimeError):
            # Signal handling may only work in the main thread
            pass

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
        
        # Clean up discovery file
        _remove_kernel_discovery(pid=os.getpid())

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
            import sqlite3

            if self.db_path.exists():
                conn = sqlite3.connect(str(self.db_path), timeout=2.0)
                try:
                    conn.execute("PRAGMA query_only = ON")
                    row = conn.execute("SELECT MAX(id) FROM memory_events").fetchone()
                    if row and row[0] is not None:
                        last_seen_id = row[0]
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"Failed to query max event id: {e}")

        while not self._stop_event.is_set():
            try:
                time.sleep(0.2)
                if not self.db_path.exists():
                    continue

                import sqlite3

                conn = sqlite3.connect(str(self.db_path), timeout=2.0)
                try:
                    conn.execute("PRAGMA query_only = ON")
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
                    conn.close()
            except Exception as e:
                logger.debug(f"Outbox polling error: {e}")

    def handle_request_direct(
        self,
        method: str,
        path: str,
        body: dict | list | None = None,
        headers: dict | None = None,
    ) -> tuple[int, Any]:
        """In-memory direct HTTP execution without TCP sockets or network delays."""
        import io

        body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
        rfile = io.BytesIO()
        wfile = io.BytesIO()

        class _MockSocket:
            def makefile(self, mode, *a, **kw):
                if "r" in mode:
                    return rfile
                return wfile

            def getsockname(self):
                return ("127.0.0.1", 0)

            def getpeername(self):
                return ("127.0.0.1", 50000)

            def sendall(self, data):
                wfile.write(data)

            def close(self):
                pass

            def settimeout(self, t):
                pass

            def shutdown(self, how):
                pass

        req_headers = dict(headers or {})
        if self.token and "Authorization" not in req_headers:
            req_headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            req_headers["Content-Type"] = "application/json"
            req_headers["Content-Length"] = str(len(body_bytes))

        header_lines = "".join(f"{k}: {v}\r\n" for k, v in req_headers.items())
        raw_request = f"{method.upper()} {path} HTTP/1.1\r\n{header_lines}\r\n".encode("utf-8") + body_bytes
        rfile.write(raw_request)
        rfile.seek(0)

        try:
            APIRequestHandler(_MockSocket(), ("127.0.0.1", 50000), self)  # type: ignore[arg-type]
        except Exception as e:
            logger.warning("handle_request_direct exception: %s", e)

        output_bytes = wfile.getvalue()
        if not output_bytes:
            return 500, {"error": "empty response"}

        lines = output_bytes.split(b"\r\n\r\n", 1)
        header_part = lines[0].decode("utf-8", errors="replace")
        body_part = lines[1] if len(lines) > 1 else b""

        status_line = header_part.split("\r\n")[0]
        status_code = int(status_line.split(" ")[1]) if " " in status_line else 200
        try:
            res_data = json.loads(body_part.decode("utf-8"))
        except Exception:
            res_data = body_part.decode("utf-8", errors="replace")
        return status_code, res_data


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
