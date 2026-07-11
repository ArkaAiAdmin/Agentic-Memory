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
        # CORS
        origin = self.headers.get("Origin", "")
        if origin and (not API_CORS_ORIGINS or origin in API_CORS_ORIGINS):
            self.send_header("Access-Control-Allow-Origin", origin)
        elif not API_CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

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
        if not auth.startswith("Bearer "):
            self._error("Authorization required: Bearer <token>", 401)
            return False
        bearer = auth[7:]

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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # WebSocket Upgrade Check
        is_ws = (self.headers.get("Upgrade", "").lower() == "websocket" and 
                 "upgrade" in self.headers.get("Connection", "").lower())
        
        if is_ws and (path == "/ws" or path == "/api/v1/streaming"):
            self._handle_ws_handshake()
            return

        # Regular REST API Routes
        if path == "/health" or path == "":
            self._handle_health()
        elif path == "/api/v1/memories":
            if not self._require_auth():
                return
            self._handle_list_memories(parse_qs(parsed.query))
        elif path == "/api/v1/memories/search":
            if not self._require_auth():
                return
            self._handle_search_memories(parse_qs(parsed.query))
        elif path == "/api/v1/memories/stats":
            if not self._require_auth():
                return
            self._handle_stats()
        elif path.startswith("/api/v1/memories/"):
            if not self._require_auth():
                return
            note_id = path[len("/api/v1/memories/"):]
            self._handle_get_memory(note_id)
        elif path == "/api/v1/kg/nodes":
            if not self._require_auth():
                return
            self._handle_kg_nodes(parse_qs(parsed.query))
        elif path == "/api/v1/kg/edges":
            if not self._require_auth():
                return
            self._handle_kg_edges(parse_qs(parsed.query))
        else:
            self._error("Not found", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if not self._require_auth():
            return

        if path == "/api/v1/memories":
            self._handle_add_memory()
        elif path == "/api/v1/memories/search":
            self._handle_search_memories_post()
        elif path == "/api/v1/memories/clear":
            self._handle_clear_memories()
        elif path == "/api/v1/maintenance/rebuild":
            self._handle_rebuild()
        elif path == "/api/v1/maintenance/compact":
            self._handle_compact()
        elif path == "/api/v1/maintenance/integrity":
            self._handle_integrity()
        elif path == "/api/v1/compliance/gdpr/erase":
            self._handle_gdpr_erase()
        else:
            self._error("Not found", 404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if not self._require_auth():
            return

        if path.startswith("/api/v1/memories/"):
            note_id = path[len("/api/v1/memories/"):]
            self._handle_delete_memory(note_id)
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
            # TODO: Replace with principal extraction hook for multi-tenant auth.
            is_global = req.get("is_global", False)
            pinned = req.get("pinned", False)

            client = MemoryClient(db_path=self.server.db_path)
            note_id = client.save(
                content=content,
                tags=tags,
                category=category,
                is_global=is_global,
                pinned=pinned,
            )
            self._write_json({"id": note_id, "status": "success"}, 201)
        except Exception as e:
            logger.warning("_handle_add_memory: broad except swallowed: %s", e)
            self._error(f"Failed to add memory: {e}", 500)

    def _handle_delete_memory(self, note_id: str) -> None:
        try:
            client = MemoryClient(db_path=self.server.db_path)
            success = client.delete(note_id)
            if success:
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
                    conn=conn,
                    principal_id=getattr(self, "_principal_id", "api"),
                    data_subject_sub=data_subject_sub,
                    tenant_id=tenant_id,
                )
            self._write_json(result)
        except Exception as e:
            logger.warning("_handle_gdpr_erase: %s", e)
            self._error(f"GDPR erase failed: {e}", 500)

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
                    "SELECT source_id, target_id, relation_type, weight, properties FROM kg_edges LIMIT ?",
                    (limit,),
                ).fetchall()
                edges = [
                    {
                        "source": r[0],
                        "target": r[1],
                        "relation": r[2],
                        "weight": r[3],
                        "properties": json.loads(r[4]) if r[4] else {}
                    }
                    for r in rows
                ]
                self._write_json({"edges": edges})
            finally:
                safe_close_db(conn)
        except Exception as e:
            logger.warning("_handle_kg_edges: broad except swallowed: %s", e)
            self._error(f"Failed to list KG edges: {e}", 500)

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
        
        self._ws_clients: Dict[str, socket.socket] = {}
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
