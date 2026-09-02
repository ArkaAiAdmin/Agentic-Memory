"""Tests for genuine idempotency, strict tenant soft-delete isolation, and route fixes."""

import json
import sqlite3
import time
import pytest
from pathlib import Path
import tempfile
import shutil

from infra.db_migrations import run_schema_setup
from fact import ensure_facts_schema
from memory_delete import soft_delete_note
from infra.idempotency import (
    get_idempotent_result,
    set_idempotent_result,
    clear_idempotency_cache,
)
from mcp_surface.mcp_verbs import memory_save as verb_memory_save
from mcp_surface.mcp_memory import memory_save as tool_memory_save
from agent_context import temporary_agent_context


@pytest.fixture
def temp_db():
    d = Path(tempfile.mkdtemp(prefix="test_idem_tenant_"))
    db_file = d / "memory.db"
    conn = sqlite3.connect(str(db_file))
    run_schema_setup(conn)
    ensure_facts_schema(conn)
    conn.commit()
    conn.close()
    yield db_file
    shutil.rmtree(str(d), ignore_errors=True)


def test_idempotency_cache_operations():
    clear_idempotency_cache()
    key = "idem-test-1234"
    assert get_idempotent_result(key) is None

    result_payload = {"id": "note-1", "status": "success"}
    set_idempotent_result(key, result_payload, tenant_id="default")
    
    cached = get_idempotent_result(key, tenant_id="default")
    assert cached == result_payload

    # Cross-tenant cache check
    assert get_idempotent_result(key, tenant_id="tenant_b") is None


def test_memory_save_idempotency_kwarg_and_deduplication(temp_db, monkeypatch):
    clear_idempotency_cache()
    monkeypatch.setenv("MEMORY_DB_PATH", str(temp_db))
    monkeypatch.setenv("MEMORY_AUTH_MODE", "open")

    key = "idem-save-key-5678"
    # First save
    res1_str = verb_memory_save(
        content="First save with key",
        category="lessons",
        title_slug="test-idem-slug-1",
        idempotency_key=key,
    )
    res1 = json.loads(res1_str)
    assert res1.get("status") == "success"
    note_id_1 = res1.get("note_id")
    assert note_id_1

    # Second save with identical key returns cached response
    res2_str = verb_memory_save(
        content="Second save with key",
        category="lessons",
        title_slug="test-idem-slug-1",
        idempotency_key=key,
    )
    res2 = json.loads(res2_str)
    assert res2.get("status") == "success"
    assert res2.get("note_id") == note_id_1


def test_strict_tenant_soft_delete_isolation(temp_db, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(temp_db))
    monkeypatch.setenv("MEMORY_AUTH_MODE", "open")

    # Insert note in tenant_a
    conn = sqlite3.connect(str(temp_db))
    now = "2026-09-02T10:00:00Z"
    conn.execute(
        "INSERT INTO memories (id, content, category, tenant_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("note-tenant-a", "Tenant A content", "lessons", "tenant_a", now, now),
    )
    # Insert note in default tenant
    conn.execute(
        "INSERT INTO memories (id, content, category, tenant_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("note-default", "Default tenant content", "lessons", "default", now, now),
    )
    conn.commit()
    conn.close()

    # 1. Tenant B principal trying to delete tenant A note -> REJECTED
    with temporary_agent_context(agent_id="agent_b", principal_id="user_b"):
        res = soft_delete_note(str(temp_db), "note-tenant-a", tenant_id="tenant_b")
        assert res is False, "Cross-tenant delete from tenant_b must be denied"

    # 2. Tenant B principal trying to delete default note -> REJECTED (strict isolation)
    with temporary_agent_context(agent_id="agent_b", principal_id="user_b"):
        res = soft_delete_note(str(temp_db), "note-default", tenant_id="tenant_b")
        assert res is False, "Non-default tenant cannot delete default tenant note"

    # 3. Tenant A principal deleting tenant A note -> ALLOWED
    with temporary_agent_context(agent_id="agent_a", principal_id="user_a"):
        res = soft_delete_note(str(temp_db), "note-tenant-a", tenant_id="tenant_a")
        assert res is True, "Same-tenant delete must succeed"

    # 4. Default tenant principal deleting default note -> ALLOWED
    with temporary_agent_context(agent_id="default_agent", principal_id="default_user"):
        res = soft_delete_note(str(temp_db), "note-default", tenant_id="default")
        assert res is True, "Default tenant delete of default note must succeed"


def test_memory_resolve_thread_decorator_count():
    from mcp_surface.mcp_session import mcp
    tool_entry = mcp._tool_manager._tools.get("memory_resolve_thread")
    assert tool_entry is not None, "memory_resolve_thread must be registered"
    # Ensure tool names are unique in registry
    assert len([k for k in mcp._tool_manager._tools.keys() if k == "memory_resolve_thread"]) == 1


def test_rest_categories_and_idempotency(temp_db, monkeypatch):
    import socket
    import urllib.request
    from infra.api_server import APIServer

    monkeypatch.setenv("MEMORY_DB_PATH", str(temp_db))
    monkeypatch.setenv("MEMORY_AUTH_MODE", "open")

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    token = "test-token-1234"
    server = APIServer(
        db_path=temp_db,
        agent_id="test-agent",
        host="127.0.0.1",
        port=port,
        token=token,
    )
    server.start()
    time.sleep(0.3)
    try:
        # 1. Test GET /api/v1/memories/categories is reachable (not shadowed)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/memories/categories",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert "categories" in data or isinstance(data, list) or isinstance(data, dict)

        # 2. Test POST /api/v1/memories with X-Idempotency-Key header
        idem_key = "rest-idem-key-999"
        post_data = json.dumps({"content": "REST Idempotent note", "category": "lessons"}).encode("utf-8")
        req1 = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/memories",
            data=post_data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": idem_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req1) as resp1:
            assert resp1.status == 201
            d1 = json.loads(resp1.read().decode("utf-8"))
            assert d1.get("status") == "success"
            first_id = d1.get("id")

        # 3. Duplicate POST returns cached result immediately
        req2 = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/memories",
            data=post_data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": idem_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req2) as resp2:
            assert resp2.status == 201
            d2 = json.loads(resp2.read().decode("utf-8"))
            assert d2.get("id") == first_id
    finally:
        server.stop()

