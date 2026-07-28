"""REST API client for agentic-memory dashboard.

Thin wrapper around infra/api_server.py endpoints.
Replaces direct conn.execute() calls with HTTP calls.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "http://127.0.0.1:9878"


def resolve_api_token(memory_dir: str | None = None) -> str:
    """Resolve the configured REST API token for dashboard auto-login.

    Resolution order:
      1. ``MEMORY_API_TOKEN`` env var (explicit, wins).
      2. ``<memory_dir>/.api_token`` file (persisted at server start, mode 0600).
      3. Empty string (no token configured).

    The file path defaults to the resolved dashboard DB's parent directory,
    falling back to ``~/.config/agentic-memory/memory``.
    """
    env_token = os.environ.get("MEMORY_API_TOKEN", "").strip()
    if env_token:
        return env_token
    if memory_dir is None:
        memory_dir = os.environ.get(
            "MEMORY_DIR", str(Path.home() / ".config" / "agentic-memory" / "memory")
        )
    token_file = Path(memory_dir) / ".api_token"
    try:
        return token_file.read_text().strip()
    except OSError:
        return ""


class ApiClient:
    """Thin wrapper around infra/api_server.py REST API."""

    def __init__(self, base_url: str = _DEFAULT_BASE, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})

    # ── Low-level ─────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _get(self, path: str, params: dict | None = None) -> Any:
        r = self._session.get(self._url(path), params=params, timeout=0.5)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict | None = None) -> Any:
        r = self._session.post(self._url(path), json=data, timeout=0.5)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, data: dict | None = None) -> Any:
        r = self._session.put(self._url(path), json=data, timeout=0.5)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> Any:
        r = self._session.delete(self._url(path), timeout=0.5)
        r.raise_for_status()
        return r.json()

    # ── Auth (Phase 2: dashboard identity layer) ───────────────────────────

    def login(self, token: str) -> dict:
        """Exchange an API token for a JWT session cookie.

        On success the cookie is stored on the underlying ``requests.Session``,
        so all subsequent calls are authenticated via the cookie — no need to
        keep the raw token in memory or resend it as a Bearer header.
        """
        r = self._session.post(
            self._url("/api/v1/auth/login"),
            json={"token": token},
            timeout=0.5,
        )
        r.raise_for_status()
        return r.json()

    def logout(self) -> dict:
        """Clear the session cookie."""
        r = self._session.post(self._url("/api/v1/auth/logout"), json={}, timeout=0.5)
        r.raise_for_status()
        return r.json()

    @property
    def authenticated(self) -> bool:
        """True if a session cookie is currently held."""
        return bool(self._session.cookies.get("am_token"))

    # ── Health ────────────────────────────────────────────────────────────

    def health(self) -> dict:
        return self._get("/health")

    # ── Memories ──────────────────────────────────────────────────────────

    def list_memories(
        self, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        try:
            result = self._get("/api/v1/memories", params={"limit": limit, "offset": offset})
            return result.get("memories", [])
        except Exception as exc:
            _classified = _classify(exc)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                q_res = self.query(
                    "SELECT * FROM memories WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    [limit, offset],
                )
                return q_res.get("results", [])
            raise

    def get_memory(self, note_id: str) -> dict:
        try:
            return self._get(f"/api/v1/memories/{note_id}")
        except Exception as exc:
            _classified = _classify(exc)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                q_res = self.query("SELECT * FROM memories WHERE id = ?", [note_id])
                rows = q_res.get("results", [])
                return rows[0] if rows else {}
            raise

    def create_memory(
        self,
        content: str,
        category: str = "sdk",
        tags: list[str] | None = None,
        pinned: bool = False,
        is_global: bool = False,
    ) -> dict:
        data: dict[str, Any] = {
            "content": content,
            "category": category,
        }
        if tags:
            data["tags"] = tags
        data["pinned"] = pinned
        data["is_global"] = is_global
        return self._post("/api/v1/memories", data=data)

    def delete_memory(self, note_id: str) -> dict:
        return self._delete(f"/api/v1/memories/{note_id}")

    def update_memory(
        self,
        note_id: str,
        content: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        pinned: bool | None = None,
        is_global: bool | None = None,
        importance: int | None = None,
        tier: str | None = None,
        title_slug: str | None = None,
    ) -> dict:
        """Update an existing memory via PUT /api/v1/memories/{id}."""
        data: dict[str, Any] = {}
        for key, val in (
            ("content", content),
            ("category", category),
            ("tags", tags),
            ("pinned", pinned),
            ("is_global", is_global),
            ("importance", importance),
            ("tier", tier),
            ("title_slug", title_slug),
        ):
            if val is not None:
                data[key] = val
        return self._put(f"/api/v1/memories/{note_id}", data=data)

    def search_memories(
        self,
        query: str,
        limit: int = 10,
        rerank: bool = True,
        tags: list[str] | None = None,
    ) -> list[dict]:
        data: dict[str, Any] = {"query": query, "limit": limit, "rerank": rerank}
        if tags:
            data["tags"] = tags
        try:
            result = self._post("/api/v1/memories/search", data=data)
            return result.get("results", [])
        except Exception as exc:
            _classified = _classify(exc)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                q_res = self.query(
                    "SELECT * FROM memories WHERE content LIKE ? AND deleted_at IS NULL LIMIT ?",
                    [f"%{query}%", limit],
                )
                return q_res.get("results", [])
            raise

    def clear_memories(self) -> dict:
        return self._post("/api/v1/memories/clear")

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        try:
            return self._get("/api/v1/memories/stats")
        except Exception as exc:
            _classified = _classify(exc)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                return {
                    "total_memories": _try_count_api("memories"),
                    "entities": _try_count_api("kg_entities"),
                    "facts": _try_count_api("kg_facts"),
                    "edges": _try_count_api("kg_edges"),
                }
            raise

    # ── Knowledge Graph ───────────────────────────────────────────────────

    def kg_nodes(self, limit: int = 100) -> list[dict]:
        try:
            result = self._get("/api/v1/kg/nodes", params={"limit": limit})
            return result.get("nodes", [])
        except Exception as exc:
            _classified = _classify(exc)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                res = self.query("SELECT * FROM kg_entities LIMIT ?", [limit])
                return res.get("results", [])
            raise

    def kg_edges(self, limit: int = 100) -> list[dict]:
        try:
            result = self._get("/api/v1/kg/edges", params={"limit": limit})
            return result.get("edges", [])
        except Exception as exc:
            _classified = _classify(exc)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                res = self.query("SELECT * FROM kg_edges LIMIT ?", [limit])
                return res.get("results", [])
            raise

    # ── Maintenance ───────────────────────────────────────────────────────

    def rebuild_index(self) -> dict:
        return self._post("/api/v1/maintenance/rebuild")

    def compact(self) -> dict:
        return self._post("/api/v1/maintenance/compact")

    def integrity_check(self) -> dict:
        return self._post("/api/v1/maintenance/integrity")

    # ── Query (generic read-only) ─────────────────────────────────────────

    def query(self, sql: str, params: list | None = None) -> dict:
        try:
            return self._post("/api/v1/query", data={"sql": sql, "params": params or []})
        except Exception as exc:
            _classified = _classify(exc)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                conn = _get_db()
                try:
                    conn.row_factory = sqlite3.Row
                    cur = conn.execute(sql, params or [])
                    rows = cur.fetchall()
                    return {"results": [dict(r) for r in rows]}
                except Exception as db_exc:
                    logger.warning("Local SQLite query fallback failed: %s", db_exc)
                    return {"results": []}
                finally:
                    conn.close()
            raise

    # ── Categories ─────────────────────────────────────────────────────────

    def categories(self) -> list[str]:
        try:
            result = self._get("/api/v1/memories/categories")
            return result.get("categories", [])
        except Exception as exc:
            _classified = _classify(exc)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                return _list_column("memories", "category")
            raise

    # ── RBAC ───────────────────────────────────────────────────────────────

    def rbac_init(self) -> dict:
        return self._post("/api/v1/rbac/init")

    def rbac_create_principal(
        self,
        pid: str,
        kind: str = "agent",
        display_name: str = "",
        tenant_id: str = "default",
    ) -> dict:
        return self._post(
            "/api/v1/rbac/principals",
            data={
                "id": pid,
                "kind": kind,
                "display_name": display_name or pid,
                "tenant_id": tenant_id,
            },
        )

    def rbac_create_role(
        self,
        rid: str,
        description: str = "",
        tenant_id: str = "default",
    ) -> dict:
        return self._post(
            "/api/v1/rbac/roles",
            data={"id": rid, "description": description, "tenant_id": tenant_id},
        )

    def rbac_grant(self, principal_id: str, role_id: str) -> dict:
        return self._post(
            "/api/v1/rbac/bindings",
            data={"principal_id": principal_id, "role_id": role_id},
        )

    def rbac_revoke(self, principal_id: str, role_id: str) -> dict:
        return self._delete(
            f"/api/v1/rbac/bindings?principal_id={principal_id}&role_id={role_id}"
        )

    # ── ACL ────────────────────────────────────────────────────────────────

    def acl_add_rule(
        self,
        principal_id: str,
        resource_id: str,
        action: str,
        effect: str = "allow",
    ) -> dict:
        return self._post(
            "/api/v1/acl/rules",
            data={
                "principal_id": principal_id,
                "resource_id": resource_id,
                "action": action,
                "effect": effect,
            },
        )

    def acl_delete_rule(self, principal_id: str, resource_id: str, action: str) -> dict:
        return self._delete(
            f"/api/v1/acl/rules?principal_id={principal_id}&resource_id={resource_id}&action={action}"
        )

    # ── GDPR ───────────────────────────────────────────────────────────────

    def gdpr_erase(self, data_subject_sub: str) -> dict:
        return self._post(
            "/api/v1/compliance/gdpr/erase",
            data={"data_subject_sub": data_subject_sub},
        )

    def kg_dedup(self) -> dict:
        return self._post("/api/v1/kg/dedup")

    def kg_prune(self, entity_ids: list) -> dict:
        return self._post("/api/v1/kg/prune", data={"entity_ids": entity_ids})

    def kg_merge(self, keep_id: int, remove_id: int) -> dict:
        return self._post(
            "/api/v1/kg/merge",
            data={"keep_id": keep_id, "remove_id": remove_id},
        )

    def delete_kg_entity(self, entity_id: str | int) -> dict:
        return self._delete(f"/api/v1/kg/entities/{entity_id}")

    def delete_kg_edge(self, edge_id: str | int) -> dict:
        return self._delete(f"/api/v1/kg/edges/{edge_id}")

    def add_kg_edge(self, source_id: int, target_id: int, relation: str,
                    weight: float = 1.0, properties: dict | None = None) -> dict:
        return self._post(
            "/api/v1/kg/edges",
            data={
                "source_id": source_id,
                "target_id": target_id,
                "relation": relation,
                "weight": weight,
                "properties": properties or {},
            },
        )

    def update_kg_entity(self, entity_id: int, entity_type: str) -> dict:
        return self._put(
            f"/api/v1/kg/entities/{entity_id}",
            data={"entity_type": entity_type},
        )

    def archive_stale(self, min_fitness: float = 0.3, min_age_days: int = 90) -> dict:
        return self._post(
            "/api/v1/memories/archive-stale",
            data={"min_fitness": min_fitness, "min_age_days": min_age_days},
        )

    # ── Coordination ──────────────────────────────────────────────────────
    def create_task(self, project_id: str, task_type: str, description: str,
                    assigned_to: str | None = None) -> dict:
        return self._post(
            "/api/v1/coordination/tasks",
            data={
                "project_id": project_id,
                "task_type": task_type,
                "description": description,
                "assigned_to": assigned_to,
            },
        )

    def update_task(self, task_id: int, status: str, assigned_to: str | None = None) -> dict:
        return self._put(
            f"/api/v1/coordination/tasks/{task_id}",
            data={"status": status, "assigned_to": assigned_to},
        )

    def release_lock(self, file_path: str) -> dict:
        return self._delete(f"/api/v1/coordination/locks?file_path={file_path}")

    def acquire_lock(self, file_path: str, locked_by: str, ttl: int) -> dict:
        return self._post(
            "/api/v1/coordination/locks",
            data={"file_path": file_path, "locked_by": locked_by, "ttl": ttl},
        )

    def send_message(self, from_agent: str, to_agent: str, message_type: str, payload: str | None = None) -> dict:
        return self._post(
            "/api/v1/coordination/messages",
            data={
                "from_agent": from_agent,
                "to_agent": to_agent,
                "message_type": message_type,
                "payload": payload,
            },
        )

    def update_project_state(self, project_id: str, key: str, value: str, updated_by: str) -> dict:
        return self._post(
            "/api/v1/coordination/state",
            data={
                "project_id": project_id,
                "key": key,
                "value": value,
                "updated_by": updated_by,
            },
        )

    # ── Cloud & Billing (Phase 5) ──────────────────────────────────────────

    def get_cloud_usage(self, deployment_id: str) -> dict:
        return self._get("/api/v1/cloud/usage", {"deployment_id": deployment_id})

    def create_cloud_checkout(self, deployment_id: str, plan_id: str) -> dict:
        return self._post("/api/v1/cloud/checkout", {"deployment_id": deployment_id, "plan_id": plan_id})

    def cloud_signup(self, email: str, name: str = "", plan_id: str = "free") -> dict:
        return self._post("/api/v1/cloud/signup", {"email": email, "name": name, "plan_id": plan_id})

    # ── Audit (Phase 1 verification) ──────────────────────────────────────

    def get_audit_logs(
        self, hours: int = 24, tool: str = "", errors_only: bool = False, limit: int = 200,
    ) -> list[dict]:
        params: dict[str, Any] = {"hours": hours, "limit": limit}
        if tool:
            params["tool"] = tool
        if errors_only:
            params["errors_only"] = "true"
        result = self._get("/api/v1/audit/logs", params=params)
        return result.get("logs", [])



# ── Module-level helpers ──────────────────────────────────────────────────
# These provide a uniform interface for dashboard tabs to access the API
# client with graceful fallback to direct DB access when the API is
# unavailable (e.g. in tests or standalone runs).

def _get_db():
    """Open a SQLite connection to the dashboard DB (fallback path).

    SECURITY: the fallback connection is strictly READ-ONLY (``mode=ro``).
    The dashboard must never write to the memory DB directly — all writes go
    through the REST API (which enforces auth, RBAC, audit, and the saga). A
    writable fallback would bypass Hard Rule 1 (all writes via save_memory)
    and risk the shared-journal single-writer invariant (Hard Rule 13).
    """
    import sqlite3
    from dashboard import DB
    if not _local_fallback_allowed():
        raise RuntimeError(
            "Local DB fallback is disabled. The dashboard requires the REST API "
            "(auth + RBAC + audit). Set DASHBOARD_ALLOW_LOCAL_FALLBACK=1 to opt in "
            "for standalone/test runs."
        )
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)


def _local_fallback_allowed() -> bool:
    """Local direct-DB fallback is opt-in only.

    Default OFF. Enabled by DASHBOARD_ALLOW_LOCAL_FALLBACK=1 (tests / offline
    runs). When off, every dashboard read must go through the authenticated
    REST API — there is no unauthenticated direct-file path.
    """
    return os.environ.get("DASHBOARD_ALLOW_LOCAL_FALLBACK", "0") == "1"


class _AuthError(Exception):
    """Raised when the API rejects the request (401/403). Never downgrade."""


def _classify(exc: Exception) -> Exception:
    """Convert an requests HTTP 401/403 into _AuthError; pass others through."""
    from requests import HTTPError
    if isinstance(exc, HTTPError):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in (401, 403):
            return _AuthError(f"API auth failed ({code})")
    return exc


def _api() -> "ApiClient | None":
    """Get the active agent's API client from session state, or None."""
    try:
        import streamlit as st
        return getattr(st.session_state, "api_client", None)
    except Exception:
        return None


def _query_api(sql: str, params: list | None = None) -> "Any":
    """Run a read-only query via the active agent's API.

    Falls back to a READ-ONLY local DB connection ONLY on transport failure
    (API down) and only when DASHBOARD_ALLOW_LOCAL_FALLBACK=1. Auth failures
    (401/403) are surfaced, never silently downgraded to direct file access.
    """
    client = _api()
    if client is not None:
        try:
            resp = client.query(sql, params or [])
            if isinstance(resp, dict) and "results" in resp:
                import pandas as pd
                return pd.DataFrame(resp["results"])
            return resp
        except _AuthError:
            raise
        except Exception as _e:
            _classified = _classify(_e)
            if isinstance(_classified, _AuthError):
                raise _classified
            # Transport failure — fall back only if explicitly allowed.
            if _local_fallback_allowed():
                from dashboard import query
                return query(sql, params or [])
            raise RuntimeError(
                "Memory API unavailable and local fallback is disabled."
            ) from _e
    if _local_fallback_allowed():
        from dashboard import query
        return query(sql, params or [])
    raise RuntimeError("No API client available and local fallback is disabled.")


def _try_count_api(table_name: str, where: str = "") -> int:
    """Count rows via API; read-only local fallback on transport failure."""
    client = _api()
    if client is not None:
        try:
            sql = f"SELECT COUNT(*) as c FROM {table_name}"
            if where:
                sql += f" WHERE {where}"
            resp = client.query(sql)
            if isinstance(resp, dict) and "results" in resp and resp["results"]:
                return int(resp["results"][0].get("c", 0))
        except _AuthError:
            raise
        except Exception as _e:
            _classified = _classify(_e)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                from dashboard import try_count
                return try_count(table_name, where)
            raise RuntimeError(
                "Memory API unavailable and local fallback is disabled."
            ) from _e
    if _local_fallback_allowed():
        from dashboard import try_count
        return try_count(table_name, where)
    raise RuntimeError("No API client available and local fallback is disabled.")


def _table_exists_api(table_name: str) -> bool:
    """Check table existence via API; read-only local fallback on transport failure."""
    client = _api()
    if client is not None:
        try:
            resp = client.query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                [table_name],
            )
            if isinstance(resp, dict) and "results" in resp:
                return len(resp["results"]) > 0
        except _AuthError:
            raise
        except Exception as _e:
            _classified = _classify(_e)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                conn = _get_db()
                try:
                    r = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        (table_name,),
                    ).fetchone()
                    return r is not None
                finally:
                    conn.close()
            raise RuntimeError(
                "Memory API unavailable and local fallback is disabled."
            ) from _e
    if _local_fallback_allowed():
        conn = _get_db()
        try:
            r = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            return r is not None
        finally:
            conn.close()
    raise RuntimeError("No API client available and local fallback is disabled.")


def _list_column_api(table_name: str, column: str) -> list:
    """List distinct column values via API; read-only local fallback on transport failure."""
    client = _api()
    if client is not None:
        try:
            resp = client.query(
                f"SELECT DISTINCT {column} FROM {table_name} WHERE {column} IS NOT NULL"
            )
            if isinstance(resp, dict) and "results" in resp:
                return [r.get(column) for r in resp["results"] if r.get(column)]
        except _AuthError:
            raise
        except Exception as _e:
            _classified = _classify(_e)
            if isinstance(_classified, _AuthError):
                raise _classified
            if _local_fallback_allowed():
                return _list_column(table_name, column)
            raise RuntimeError(
                "Memory API unavailable and local fallback is disabled."
            ) from _e
    if _local_fallback_allowed():
        return _list_column(table_name, column)
    raise RuntimeError("No API client available and local fallback is disabled.")


def _get_conn_api():
    """Return the active agent's API client (preferred).

    The dashboard never hands out a raw writable connection. When the API is
    unavailable and local fallback is explicitly enabled, a READ-ONLY
    connection is returned; otherwise an error is raised.
    """
    client = _api()
    if client is not None:
        return client
    if _local_fallback_allowed():
        return _get_db()
    raise RuntimeError("No API client available and local fallback is disabled.")


def _list_column(table_name: str, column: str) -> list:
    """List distinct non-null values in a column (read-only fallback path)."""
    conn = _get_db()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM {table_name} WHERE {column} IS NOT NULL ORDER BY {column}"
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []
    finally:
        conn.close()
