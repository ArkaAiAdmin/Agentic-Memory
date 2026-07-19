"""Regression tests for dashboard secure data-access (feat/dashboard-secure-multiagent).

Locks in the security guarantees:
  * local DB fallback is opt-in (DASHBOARD_ALLOW_LOCAL_FALLBACK=1) and READ-ONLY
  * auth failures (401/403) are never downgraded to the local DB
  * transport failures fall back only when fallback is enabled
  * ApiClient writes route to the correct REST endpoints
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import MagicMock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # eval/ -> repo root
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

REAL_DB = os.path.join(REPO_ROOT, "memory", "memory.db")

# _ReadOnlyError exists on newer CPython; older versions raise OperationalError.
_READ_ONLY_EXC = (sqlite3.OperationalError,)
if hasattr(sqlite3, "ReadOnlyError"):
    _READ_ONLY_EXC = _READ_ONLY_EXC + (sqlite3.ReadOnlyError,)  # type: ignore[attr-defined]


def _load_streamlit_module():
    """Inject a fake `streamlit` stub into sys.modules so `dashboard` imports cleanly."""
    if "streamlit" not in sys.modules:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "streamlit", os.path.join(os.path.dirname(__file__), "_stub_streamlit.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["streamlit"] = module
        spec.loader.exec_module(module)
    return sys.modules["streamlit"]


def _import_api_client():
    """Import dashboard.api_client with a fake streamlit already in place."""
    _load_streamlit_module()
    import dashboard.api_client as ac

    return ac


@pytest.fixture
def api_client(monkeypatch):
    """Provide a freshly imported dashboard.api_client with a fake streamlit."""
    ac = _import_api_client()
    import dashboard

    # Point the dashboard module's DB at the real read-only-openable file.
    monkeypatch.setattr(dashboard, "DB", REAL_DB)
    monkeypatch.delenv("DASHBOARD_ALLOW_LOCAL_FALLBACK", raising=False)
    import streamlit as st

    st.session_state.pop("api_client", None)
    yield ac
    st.session_state.pop("api_client", None)


def _make_client_http_error(status):
    import requests

    resp = MagicMock()
    resp.status_code = status
    http_err = requests.HTTPError(f"boom {status}")
    http_err.response = resp
    client = MagicMock()
    client.query.side_effect = http_err
    return client


def _make_client_transport_error():
    client = MagicMock()
    client.query.side_effect = ConnectionError("API down")
    return client


def _set_client(client):
    import streamlit as st

    st.session_state["api_client"] = client


# ── 1. Read-only fallback ────────────────────────────────────────────────
def test_get_db_readonly_when_fallback_enabled(api_client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ALLOW_LOCAL_FALLBACK", "1")

    conn = api_client._get_db()
    try:
        with pytest.raises(_READ_ONLY_EXC):
            conn.execute("CREATE TABLE IF NOT EXISTS __t__ (x INTEGER)")
    finally:
        conn.close()


def test_get_db_raises_when_fallback_disabled(api_client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_ALLOW_LOCAL_FALLBACK", raising=False)
    with pytest.raises(RuntimeError):
        api_client._get_db()


# ── 2. Fallback disabled by default ──────────────────────────────────────
def test_local_fallback_disabled_by_default(api_client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_ALLOW_LOCAL_FALLBACK", raising=False)
    assert api_client._local_fallback_allowed() is False


def test_get_conn_api_raises_when_no_client(api_client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_ALLOW_LOCAL_FALLBACK", raising=False)
    _set_client(None)
    with pytest.raises(RuntimeError):
        api_client._get_conn_api()


# ── 3. Auth failure never downgrades ─────────────────────────────────────
def test_auth_failure_reraises_with_fallback_disabled(api_client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_ALLOW_LOCAL_FALLBACK", raising=False)
    _set_client(_make_client_http_error(403))
    # Security guarantee: a 403 must NOT silently fall back to local DB, and must
    # be re-raised as _AuthError (not the raw requests.HTTPError).
    with pytest.raises(api_client._AuthError):
        api_client._query_api("SELECT 1")


def test_auth_failure_reraises_with_fallback_enabled(api_client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ALLOW_LOCAL_FALLBACK", "1")
    _set_client(_make_client_http_error(403))
    # Auth failure must NOT be downgraded to local DB even when fallback allowed.
    with pytest.raises(api_client._AuthError):
        api_client._query_api("SELECT 1")


# ── 4. Transport failure falls back only when allowed ────────────────────
def test_transport_failure_falls_back_when_allowed(api_client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ALLOW_LOCAL_FALLBACK", "1")
    _set_client(_make_client_transport_error())
    result = api_client._query_api("SELECT name FROM sqlite_master LIMIT 1")
    assert result is not None


def test_transport_failure_raises_when_not_allowed(api_client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_ALLOW_LOCAL_FALLBACK", raising=False)
    _set_client(_make_client_transport_error())
    # Security guarantee: no silent local fallback. Must raise RuntimeError
    # (classified), not the raw ConnectionError, when fallback is disabled.
    with pytest.raises(RuntimeError):
        api_client._query_api("SELECT 1")


# ── 5. ApiClient.update_memory routes to PUT ─────────────────────────────
def test_update_memory_puts_correct_body(api_client):
    import dashboard.api_client as ac

    recorded = {}

    def fake_put(self, path, data=None):
        recorded["path"] = path
        recorded["data"] = data
        return {"ok": True}

    orig = ac.ApiClient._put
    ac.ApiClient._put = fake_put
    try:
        client = ac.ApiClient()
        client.update_memory("x/y", tier="cold", pinned=True)
    finally:
        ac.ApiClient._put = orig

    assert recorded["path"] == "/api/v1/memories/x/y"
    assert recorded["data"]["tier"] == "cold"
    assert recorded["data"]["pinned"] is True
    assert "content" not in recorded["data"]
    assert "category" not in recorded["data"]


# ── 6. ApiClient.delete_kg_entity/edge routes correctly ──────────────────
def test_delete_kg_entity_routes(api_client):
    import dashboard.api_client as ac

    recorded = {}

    def fake_delete(self, path):
        recorded["path"] = path
        return {"ok": True}

    orig = ac.ApiClient._delete
    ac.ApiClient._delete = fake_delete
    try:
        ac.ApiClient().delete_kg_entity(42)
    finally:
        ac.ApiClient._delete = orig

    assert recorded["path"] == "/api/v1/kg/entities/42"


def test_delete_kg_edge_routes(api_client):
    import dashboard.api_client as ac

    recorded = {}

    def fake_delete(self, path):
        recorded["path"] = path
        return {"ok": True}

    orig = ac.ApiClient._delete
    ac.ApiClient._delete = fake_delete
    try:
        ac.ApiClient().delete_kg_edge(7)
    finally:
        ac.ApiClient._delete = orig

    assert recorded["path"] == "/api/v1/kg/edges/7"


def test_add_kg_edge_routes(api_client):
    import dashboard.api_client as ac

    recorded = {}

    def fake_post(self, path, data=None):
        recorded["path"] = path
        recorded["data"] = data
        return {"id": 99, "status": "ok"}

    orig = ac.ApiClient._post
    ac.ApiClient._post = fake_post
    try:
        ac.ApiClient().add_kg_edge(1, 2, "co-occurs", 0.5)
    finally:
        ac.ApiClient._post = orig

    assert recorded["path"] == "/api/v1/kg/edges"
    assert recorded["data"]["source_id"] == 1
    assert recorded["data"]["target_id"] == 2
    assert recorded["data"]["relation"] == "co-occurs"
    assert recorded["data"]["weight"] == 0.5


def test_kg_prune_routes(api_client):
    import dashboard.api_client as ac

    recorded = {}

    def fake_post(self, path, data=None):
        recorded["path"] = path
        recorded["data"] = data
        return {"pruned": 3}

    orig = ac.ApiClient._post
    ac.ApiClient._post = fake_post
    try:
        ac.ApiClient().kg_prune([1, 2, 3])
    finally:
        ac.ApiClient._post = orig

    assert recorded["path"] == "/api/v1/kg/prune"
    assert recorded["data"]["entity_ids"] == [1, 2, 3]


def test_kg_merge_routes(api_client):
    import dashboard.api_client as ac

    recorded = {}

    def fake_post(self, path, data=None):
        recorded["path"] = path
        recorded["data"] = data
        return {"status": "ok"}

    orig = ac.ApiClient._post
    ac.ApiClient._post = fake_post
    try:
        ac.ApiClient().kg_merge(10, 20)
    finally:
        ac.ApiClient._post = orig

    assert recorded["path"] == "/api/v1/kg/merge"
    assert recorded["data"]["keep_id"] == 10
    assert recorded["data"]["remove_id"] == 20


def test_update_kg_entity_routes(api_client):
    import dashboard.api_client as ac

    recorded = {}

    def fake_put(self, path, data=None):
        recorded["path"] = path
        recorded["data"] = data
        return {"status": "ok"}

    orig = ac.ApiClient._put
    ac.ApiClient._put = fake_put
    try:
        ac.ApiClient().update_kg_entity(42, entity_type="topic")
    finally:
        ac.ApiClient._put = orig

    assert recorded["path"] == "/api/v1/kg/entities/42"
    assert recorded["data"]["entity_type"] == "topic"
    assert "label" not in recorded["data"]
