"""REST API client for agentic-memory dashboard.

Thin wrapper around infra/api_server.py endpoints.
Replaces direct conn.execute() calls with HTTP calls.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from dashboard import DB, get_conn, query, try_count

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "http://127.0.0.1:9878"


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
        r = self._session.get(self._url(path), params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict | None = None) -> Any:
        r = self._session.post(self._url(path), json=data, timeout=30)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, data: dict | None = None) -> Any:
        r = self._session.put(self._url(path), json=data, timeout=30)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> Any:
        r = self._session.delete(self._url(path), timeout=30)
        r.raise_for_status()
        return r.json()

    # ── Health ────────────────────────────────────────────────────────────

    def health(self) -> dict:
        return self._get("/health")

    # ── Memories ──────────────────────────────────────────────────────────

    def list_memories(
        self, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        result = self._get("/api/v1/memories", params={"limit": limit, "offset": offset})
        return result.get("memories", [])

    def get_memory(self, note_id: str) -> dict:
        return self._get(f"/api/v1/memories/{note_id}")

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
        result = self._post("/api/v1/memories/search", data=data)
        return result.get("results", [])

    def clear_memories(self) -> dict:
        return self._post("/api/v1/memories/clear")

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return self._get("/api/v1/memories/stats")

    # ── Knowledge Graph ───────────────────────────────────────────────────

    def kg_nodes(self, limit: int = 100) -> list[dict]:
        result = self._get("/api/v1/kg/nodes", params={"limit": limit})
        return result.get("nodes", [])

    def kg_edges(self, limit: int = 100) -> list[dict]:
        result = self._get("/api/v1/kg/edges", params={"limit": limit})
        return result.get("edges", [])

    # ── Maintenance ───────────────────────────────────────────────────────

    def rebuild_index(self) -> dict:
        return self._post("/api/v1/maintenance/rebuild")

    def compact(self) -> dict:
        return self._post("/api/v1/maintenance/compact")

    def integrity_check(self) -> dict:
        return self._post("/api/v1/maintenance/integrity")

    # ── Query (generic read-only) ─────────────────────────────────────────

    def query(self, sql: str, params: list | None = None) -> dict:
        return self._post("/api/v1/query", data={"sql": sql, "params": params or []})

    # ── Categories ─────────────────────────────────────────────────────────

    def categories(self) -> list[str]:
        result = self._get("/api/v1/memories/categories")
        return result.get("categories", [])

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


# ── Module-level helpers ──────────────────────────────────────────────────
# These provide a uniform interface for dashboard tabs to access the API
# client with graceful fallback to direct DB access when the API is
# unavailable (e.g. in tests or standalone runs).

def _get_db():
    """Open a SQLite connection to the dashboard DB (fallback path)."""
    import sqlite3
    return sqlite3.connect(str(DB), timeout=10)


def _list_column(table_name: str, column: str) -> list:
    """List distinct non-null values in a column (fallback path)."""
    try:
        conn = _get_db()
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM {table_name} WHERE {column} IS NOT NULL ORDER BY {column}"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def _api() -> "ApiClient | None":
    """Get the API client from session state, or None if unavailable."""
    try:
        import streamlit as st
        return getattr(st.session_state, "api_client", None)
    except Exception:
        return None


def _query_api(sql: str, params: list | None = None) -> "Any":
    """Run a read-only query via API; fall back to local DB on failure."""
    client = _api()
    if client:
        try:
            resp = client.query(sql, params or [])
            if isinstance(resp, dict) and "results" in resp:
                import pandas as pd
                return pd.DataFrame(resp["results"])
            return resp
        except Exception:
            pass
    # Fallback
    import dashboard
    return query(sql, params or [])


def _try_count_api(table_name: str, where: str = "") -> int:
    """Count rows via API; fall back to local DB on failure."""
    client = _api()
    if client:
        try:
            sql = f"SELECT COUNT(*) as c FROM {table_name}"
            if where:
                sql += f" WHERE {where}"
            resp = client.query(sql)
            if isinstance(resp, dict) and "results" in resp and resp["results"]:
                return resp["results"][0].get("c", 0)
        except Exception:
            pass
    import dashboard
    return try_count(table_name, where)


def _table_exists_api(table_name: str) -> bool:
    """Check if a table exists via API; fall back to local DB."""
    client = _api()
    if client:
        try:
            resp = client.query(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
            if isinstance(resp, dict) and "results" in resp:
                return len(resp["results"]) > 0
        except Exception:
            pass
    import dashboard
    try:
        conn = _get_db()
        r = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        conn.close()
        return r is not None
    except Exception:
        return False


def _list_column_api(table_name: str, column: str) -> list:
    """List distinct values in a column via API; fall back to local DB."""
    client = _api()
    if client:
        try:
            resp = client.query(f"SELECT DISTINCT {column} FROM {table_name} WHERE {column} IS NOT NULL")
            if isinstance(resp, dict) and "results" in resp:
                return [r.get(column) for r in resp["results"]]
        except Exception:
            pass
    import dashboard
    return _list_column(table_name, column)


def _get_conn_api():
    """Get a DB connection; prefer API-backed where possible, else local."""
    client = _api()
    if client:
        return client
    import dashboard
    return _get_db()
