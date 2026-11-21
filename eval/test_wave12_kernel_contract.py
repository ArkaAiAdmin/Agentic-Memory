import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest import mock

from agentic_memory.client import MemoryClient
from eval._fixtures import bootstrap_temp_db_clean
from infra.api_server import APIServer
from infra.config import APIConfig, _build_config_from_toml
from infra.memory_common import connection_pool
from infra.write_journal import (
    _get_journal_conn,
    dequeue_pending,
    enqueue_write,
    init_journal_db,
    mark_dead_letter,
)
from save.pipeline import SaveRequest, materialize_journal_entry
from search.orchestrator import search_memories


def test_default_port_9876_and_env_override():
    cfg_default = APIConfig()
    assert cfg_default.listen_port == 9876

    with mock.patch.dict(os.environ, {"MEMORY_API_PORT": "9901"}, clear=False):
        env_removed = os.environ.copy()
        env_removed.pop("MEMORY_API_LISTEN_PORT", None)
        with mock.patch.dict(os.environ, env_removed, clear=True):
            cfg = _build_config_from_toml({})
            assert cfg.api.listen_port == 9901


def test_api_server_category_project_metadata_session_and_compact():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "memory.db"
        old_db = os.environ.get("MEMORY_DB_PATH")
        old_rerank = os.environ.get("MEMORY_RERANKER_DISABLED")
        old_journal = os.environ.get("MEMORY_WRITE_JOURNAL_ENABLED")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        os.environ["MEMORY_RERANKER_DISABLED"] = "true"
        os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = "0"
        connection_pool.clear()
        server = None
        try:
            bootstrap_temp_db_clean(db_path)
            for cat in ("lessons", "decisions", "sessions", "projects", "skill_usage"):
                (tmp_path / "memory" / cat).mkdir(parents=True, exist_ok=True)

            server = APIServer(
                db_path=db_path,
                agent_id="test-agent",
                host="127.0.0.1",
                port=0,
                token="test-tok",
            )
            headers = {"Authorization": "Bearer test-tok"}

            # POST /api/v1/memories preserves skill_usage category, project, and metadata
            status, created = server.handle_request_direct(
                method="POST",
                path="/api/v1/memories",
                body={
                    "content": "Skill usage benchmark note",
                    "category": "skill_usage",
                    "tags": ["wave12", "bench"],
                    "project": "ami-ide",
                    "metadata": {"tool": "pytest", "duration_ms": 42},
                },
                headers=headers,
            )
            assert status in (200, 201), created
            note_id = created["id"]
            assert created["status"] == "success"

            # GET single note returns category, project, metadata
            status, fetched = server.handle_request_direct(
                method="GET",
                path=f"/api/v1/memories/{note_id}",
                headers=headers,
            )
            assert status == 200, fetched
            assert fetched["category"] == "skill_usage"
            assert fetched["project"] == "ami-ide"
            assert fetched["metadata"]["duration_ms"] == 42
            assert fetched["metadata"]["tool"] == "pytest"

            # POST /api/v1/memories/search preserves category & project filtering and metadata
            status, searched = server.handle_request_direct(
                method="POST",
                path="/api/v1/memories/search",
                body={
                    "query": "benchmark",
                    "mode": "fts",
                    "category": "skill_usage",
                    "project": "ami-ide",
                },
                headers=headers,
            )
            assert status == 200, searched
            assert searched["count"] >= 1
            assert searched["results"][0]["project"] == "ami-ide"
            assert searched["results"][0]["metadata"]["tool"] == "pytest"

            # Searching a different project excludes it
            status, searched_other = server.handle_request_direct(
                method="POST",
                path="/api/v1/memories/search",
                body={
                    "query": "benchmark",
                    "mode": "fts",
                    "project": "other-project",
                },
                headers=headers,
            )
            assert status == 200, searched_other
            assert searched_other["count"] == 0

            # PUT updates metadata & project
            status, updated = server.handle_request_direct(
                method="PUT",
                path=f"/api/v1/memories/{note_id}",
                body={
                    "project": "ami-core",
                    "metadata": {"updated": True},
                },
                headers=headers,
            )
            assert status == 200, updated
            assert updated["status"] == "updated"

            status, refetched = server.handle_request_direct(
                method="GET",
                path=f"/api/v1/memories/{note_id}",
                headers=headers,
            )
            assert status == 200, refetched
            assert refetched["project"] == "ami-core"
            assert refetched["metadata"]["updated"] is True

            # POST /api/v1/memories/session/start returns full briefing contract
            status, started = server.handle_request_direct(
                method="POST",
                path="/api/v1/memories/session/start",
                body={"query": "benchmark"},
                headers=headers,
            )
            assert status == 200, started
            assert isinstance(started["session_id"], str) and len(started["session_id"]) > 0
            assert isinstance(started["context"], str)
            assert isinstance(started["active_threads"], list)
            assert isinstance(started["recent_memories"], list)
            assert isinstance(started["project_facts"], list)

            # POST /api/v1/maintenance/compact returns non-empty context string
            status, compacted = server.handle_request_direct(
                method="POST",
                path="/api/v1/maintenance/compact",
                body={"session_id": started["session_id"]},
                headers=headers,
            )
            assert status == 200, compacted
            assert isinstance(compacted["context"], str) and len(compacted["context"]) > 0
        finally:
            if server is not None:
                server.server_close()
            connection_pool.clear()

            if old_db is not None:
                os.environ["MEMORY_DB_PATH"] = old_db
            else:
                os.environ.pop("MEMORY_DB_PATH", None)
            if old_rerank is not None:
                os.environ["MEMORY_RERANKER_DISABLED"] = old_rerank
            else:
                os.environ.pop("MEMORY_RERANKER_DISABLED", None)
            if old_journal is not None:
                os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = old_journal
            else:
                os.environ.pop("MEMORY_WRITE_JOURNAL_ENABLED", None)


def test_l2_03_memory_search_tool_call_project_and_tags_fallback():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "memory.db"
        old_db = os.environ.get("MEMORY_DB_PATH")
        old_rerank = os.environ.get("MEMORY_RERANKER_DISABLED")
        old_journal = os.environ.get("MEMORY_WRITE_JOURNAL_ENABLED")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        os.environ["MEMORY_RERANKER_DISABLED"] = "true"
        os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = "0"
        connection_pool.clear()
        server = None
        try:
            bootstrap_temp_db_clean(db_path)
            for cat in ("lessons", "decisions", "sessions", "projects", "skill_usage"):
                (tmp_path / "memory" / cat).mkdir(parents=True, exist_ok=True)

            server = APIServer(
                db_path=db_path,
                agent_id="test-agent",
                host="127.0.0.1",
                port=0,
                token="test-tok",
            )
            headers = {"Authorization": "Bearer test-tok"}

            # Create higher-ranked/more notes in another project + untagged notes
            for idx in range(6):
                st, _ = server.handle_request_direct(
                    method="POST",
                    path="/api/v1/memories",
                    body={
                        "content": f"quantum search fallback noise item {idx} quantum",
                        "category": "skill_usage",
                        "tags": ["other-tag"],
                        "project": "other-proj",
                    },
                    headers=headers,
                )
                assert st in (200, 201)

            st, _ = server.handle_request_direct(
                method="POST",
                path="/api/v1/memories",
                body={
                    "content": "quantum search fallback wrong tag in target project",
                    "category": "skill_usage",
                    "tags": ["noise"],
                    "project": "ami-ide",
                },
                headers=headers,
            )
            assert st in (200, 201)

            st, target_created = server.handle_request_direct(
                method="POST",
                path="/api/v1/memories",
                body={
                    "content": "quantum search fallback exact target note",
                    "category": "skill_usage",
                    "tags": ["wave12"],
                    "project": "ami-ide",
                },
                headers=headers,
            )
            assert st in (200, 201)
            target_id = target_created["id"]

            # Exercise POST /api/v1/tools/call with tool="memory_search", project & tags
            status, tool_resp = server.handle_request_direct(
                method="POST",
                path="/api/v1/tools/call",
                body={
                    "tool": "memory_search",
                    "arguments": {
                        "query": "quantum",
                        "mode": "fts",
                        "category": "skill_usage",
                        "tags": ["wave12"],
                        "project": "ami-ide",
                        "limit": 2,
                        "light": True,
                        "rerank": False,
                    },
                },
                headers=headers,
            )
            assert status == 200, tool_resp
            assert tool_resp["tool"] == "memory_search"
            result_text = tool_resp["result"]["result"]
            assert target_id in result_text
            assert "exact target note" in result_text
            assert "noise item" not in result_text
            assert "wrong tag" not in result_text

            # Non-matching project with category + tags returns empty cleanly (no SQL error)
            status, empty_resp = server.handle_request_direct(
                method="POST",
                path="/api/v1/tools/call",
                body={
                    "tool": "memory_search",
                    "arguments": {
                        "query": "quantum",
                        "mode": "fts",
                        "category": "skill_usage",
                        "tags": ["wave12"],
                        "project": "nonexistent-proj",
                        "limit": 2,
                    },
                },
                headers=headers,
            )
            assert status == 200, empty_resp
            assert target_id not in str(empty_resp["result"])
        finally:
            if server is not None:
                server.server_close()
            connection_pool.clear()

            if old_db is not None:
                os.environ["MEMORY_DB_PATH"] = old_db
            else:
                os.environ.pop("MEMORY_DB_PATH", None)
            if old_rerank is not None:
                os.environ["MEMORY_RERANKER_DISABLED"] = old_rerank
            else:
                os.environ.pop("MEMORY_RERANKER_DISABLED", None)
            if old_journal is not None:
                os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = old_journal
            else:
                os.environ.pop("MEMORY_WRITE_JOURNAL_ENABLED", None)


def test_l2_06_list_and_search_project_pushdown_beyond_window():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "memory.db"
        old_db = os.environ.get("MEMORY_DB_PATH")
        old_rerank = os.environ.get("MEMORY_RERANKER_DISABLED")
        old_journal = os.environ.get("MEMORY_WRITE_JOURNAL_ENABLED")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        os.environ["MEMORY_RERANKER_DISABLED"] = "true"
        os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = "0"
        connection_pool.clear()
        try:
            bootstrap_temp_db_clean(db_path)
            mc = MemoryClient(db_path=db_path)

            # Insert 210 newer rows in another project and 5 older rows in target project
            # directly into SQLite + FTS so we exceed the legacy 200-row window quickly.
            conn = sqlite3.connect(str(db_path))
            try:
                for i in range(5):
                    nid = f"lessons/target-{i:03d}"
                    content = f"pushdown target memory {i}"
                    conn.execute(
                        "INSERT INTO memories (id, content, source_file, tags, category, created_at, tenant_id, metadata) "
                        "VALUES (?, ?, ?, '[]', 'lessons', ?, 'default', ?)",
                        (
                            nid,
                            content,
                            f"lessons/target-{i:03d}.md",
                            f"2026-01-01T00:00:{i:02d}Z",
                            json.dumps({"project": "target-proj", "idx": i}),
                        ),
                    )

                for i in range(210):
                    nid = f"lessons/other-{i:03d}"
                    content = f"pushdown target memory noise {i} pushdown target"
                    conn.execute(
                        "INSERT INTO memories (id, content, source_file, tags, category, created_at, tenant_id, metadata) "
                        "VALUES (?, ?, ?, '[]', 'lessons', ?, 'default', ?)",
                        (
                            nid,
                            content,
                            f"lessons/other-{i:03d}.md",
                            f"2026-02-01T{(i // 60):02d}:{(i % 60):02d}:00Z",
                            json.dumps({"project": "other-proj", "idx": i}),
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

            # list(project=..., offset=...) past 200-row window
            page1 = mc.list(limit=2, offset=0, project="target-proj", tenant_id="default")
            page2 = mc.list(limit=2, offset=2, project="target-proj", tenant_id="default")
            page3 = mc.list(limit=2, offset=4, project="target-proj", tenant_id="default")
            assert [m.id for m in page1] == ["lessons/target-004", "lessons/target-003"]
            assert [m.id for m in page2] == ["lessons/target-002", "lessons/target-001"]
            assert [m.id for m in page3] == ["lessons/target-000"]

            # search_memories(project=..., limit=...) finds target-proj even when 210 higher-ranked notes exist in other-proj
            searched = search_memories(
                db_path=db_path,
                query="pushdown target",
                limit=3,
                mode="fts",
                project="target-proj",
                light=True,
                rerank=False,
            )
            assert searched["count"] == 3
            assert all(r["metadata"].get("project") == "target-proj" for r in searched["results"])
        finally:
            connection_pool.clear()
            if old_db is not None:
                os.environ["MEMORY_DB_PATH"] = old_db
            else:
                os.environ.pop("MEMORY_DB_PATH", None)
            if old_rerank is not None:
                os.environ["MEMORY_RERANKER_DISABLED"] = old_rerank
            else:
                os.environ.pop("MEMORY_RERANKER_DISABLED", None)
            if old_journal is not None:
                os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = old_journal
            else:
                os.environ.pop("MEMORY_WRITE_JOURNAL_ENABLED", None)


def test_pg_1_write_journal_metadata_project_roundtrip_and_dead_letter():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "memory.db"
        journal_path = tmp_path / "journal.db"
        old_db = os.environ.get("MEMORY_DB_PATH")
        old_journal = os.environ.get("MEMORY_WRITE_JOURNAL_ENABLED")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = "1"
        connection_pool.clear()
        try:
            bootstrap_temp_db_clean(db_path)
            (tmp_path / "memory" / "lessons").mkdir(parents=True, exist_ok=True)
            init_journal_db(journal_path)

            # 1. Enqueue + materialize preserves metadata & project
            req = SaveRequest(
                content="CQRS journal metadata roundtrip verification",
                category="lessons",
                title_slug="cqrs-meta-roundtrip",
                tags=["cqrs", "pg1"],
                db_path=str(db_path),
                metadata={"tool": "pytest", "duration_ms": 17},
                project="ami-ide",
            )
            note_id = enqueue_write(journal_path, req, agent_id="test-agent")
            entries = dequeue_pending(journal_path, batch_size=5)
            assert len(entries) == 1
            assert json.loads(entries[0]["metadata"]) == {
                "tool": "pytest",
                "duration_ms": 17,
                "project": "ami-ide",
            }
            materialize_journal_entry(entries[0], tmp_path, journal_path)

            mc = MemoryClient(db_path=db_path)
            fetched = mc.get(note_id)
            assert fetched is not None
            assert fetched.metadata.get("project") == "ami-ide"
            assert fetched.metadata.get("tool") == "pytest"
            assert fetched.metadata.get("duration_ms") == 17

            # 2. mark_dead_letter() preserves metadata in journal_failed
            dl_req = SaveRequest(
                content="CQRS dead letter metadata preservation",
                category="lessons",
                title_slug="cqrs-dead-letter",
                tags=["dlq"],
                db_path=str(db_path),
                metadata={"trace_id": "tr-99"},
                project="ami-core",
            )
            enqueue_write(journal_path, dl_req, agent_id="test-agent")
            dl_entries = dequeue_pending(journal_path, batch_size=5)
            assert len(dl_entries) == 1
            mark_dead_letter(journal_path, dl_entries[0]["id"], "simulated permanent failure")

            jconn = _get_journal_conn(journal_path)
            failed_row = jconn.execute(
                "SELECT note_id, metadata, error FROM journal_failed WHERE note_id = ?",
                ("lessons/cqrs-dead-letter",),
            ).fetchone()
            assert failed_row is not None
            assert json.loads(failed_row["metadata"]) == {
                "trace_id": "tr-99",
                "project": "ami-core",
            }
            assert "simulated permanent failure" in failed_row["error"]
        finally:
            from agent_context import clear_agent
            from infra.config import reset_config
            clear_agent()
            connection_pool.clear()
            if old_db is not None:
                os.environ["MEMORY_DB_PATH"] = old_db
            else:
                os.environ.pop("MEMORY_DB_PATH", None)
            if old_journal is not None:
                os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = old_journal
            else:
                os.environ.pop("MEMORY_WRITE_JOURNAL_ENABLED", None)
            reset_config()


def test_pg_3_outbox_loop_canonical_memory_event_envelope():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "memory.db"
        connection_pool.clear()
        server = None
        try:
            bootstrap_temp_db_clean(db_path)
            server = APIServer(
                db_path=db_path,
                agent_id="test-agent",
                host="127.0.0.1",
                port=0,
                token="test-tok",
            )

            captured: list[dict] = []
            seen = threading.Event()

            def _capture_broadcast(msg: str) -> None:
                captured.append(json.loads(msg))
                seen.set()

            server.broadcast = _capture_broadcast  # type: ignore[method-assign]
            loop_thread = threading.Thread(target=server._outbox_loop, daemon=True)
            loop_thread.start()
            try:
                # Allow _outbox_loop to record initial MAX(id) before inserting outbox row
                threading.Event().wait(0.25)
                conn = sqlite3.connect(str(db_path))
                try:
                    conn.execute(
                        "INSERT INTO memory_events (event_type, note_id, payload) VALUES (?, ?, ?)",
                        ("memory_added", "lessons/outbox-pg3", json.dumps({"category": "lessons"})),
                    )
                    conn.commit()
                finally:
                    conn.close()

                assert seen.wait(timeout=3.0), "_outbox_loop did not broadcast memory_event within 3s"
                assert len(captured) >= 1
                envelope = captured[0]
                assert envelope["event"] == "memory_event"
                data = envelope["data"]
                assert data["event_type"] == "memory_added"
                assert data["type"] == "memory.saved"
                assert data["note_id"] == "lessons/outbox-pg3"
                assert data["payload"] == {"category": "lessons"}
            finally:
                server._stop_event.set()
                loop_thread.join(timeout=3.0)
        finally:
            if server is not None:
                server.server_close()
            connection_pool.clear()


def test_l2r2_01_fts_tag_fallback_with_existing_project_and_tenants():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "memory.db"
        old_db = os.environ.get("MEMORY_DB_PATH")
        old_rerank = os.environ.get("MEMORY_RERANKER_DISABLED")
        old_journal = os.environ.get("MEMORY_WRITE_JOURNAL_ENABLED")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        os.environ["MEMORY_RERANKER_DISABLED"] = "true"
        os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = "0"
        connection_pool.clear()
        server = None
        try:
            bootstrap_temp_db_clean(db_path)
            for cat in ("lessons", "decisions", "sessions", "projects", "skill_usage"):
                (tmp_path / "memory" / cat).mkdir(parents=True, exist_ok=True)

            server = APIServer(
                db_path=db_path,
                agent_id="test-agent",
                host="127.0.0.1",
                port=0,
                token="test-tok",
            )
            headers = {"Authorization": "Bearer test-tok"}

            st, created = server.handle_request_direct(
                method="POST",
                path="/api/v1/memories",
                body={
                    "content": "fallbackprobefromdefaulttenant unique text",
                    "category": "lessons",
                    "tags": ["existing-tag"],
                    "project": "ami-ide",
                },
                headers=headers,
            )
            assert st in (200, 201)
            default_id = created["id"]

            # Default tenant + existing project + unmatched tag triggers _fts_search tag fallback
            status, tool_resp = server.handle_request_direct(
                method="POST",
                path="/api/v1/tools/call",
                body={
                    "tool": "memory_search",
                    "arguments": {
                        "query": "fallbackprobefromdefaulttenant",
                        "mode": "fts",
                        "category": "lessons",
                        "tags": ["nonexistent-tag"],
                        "project": "ami-ide",
                        "limit": 5,
                    },
                },
                headers=headers,
            )
            assert status == 200, tool_resp
            assert default_id in tool_resp["result"]["result"]

            # Non-default tenant + existing project + unmatched tag must not swap SQL bindings
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "INSERT INTO memories (id, content, source_file, tags, category, created_at, tenant_id, metadata) "
                    "VALUES (?, ?, ?, ?, 'lessons', ?, 'tenant-b', ?)",
                    (
                        "lessons/tenant-b-note",
                        "fallbackprobefromdefaulttenant tenant b row",
                        "lessons/tenant-b-note.md",
                        json.dumps(["other-tag"]),
                        "2026-03-01T00:00:00Z",
                        json.dumps({"project": "ami-ide"}),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            tenant_res = search_memories(
                db_path=db_path,
                query="fallbackprobefromdefaulttenant",
                limit=5,
                mode="fts",
                tags=["nonexistent-tag"],
                project="ami-ide",
                tenant_id="tenant-b",
                include_global=False,
                light=True,
                rerank=False,
            )
            assert tenant_res["count"] == 1
            assert tenant_res["results"][0]["id"] == "lessons/tenant-b-note"
            assert tenant_res["results"][0]["metadata"].get("project") == "ami-ide"
        finally:
            if server is not None:
                server.server_close()
            connection_pool.clear()
            if old_db is not None:
                os.environ["MEMORY_DB_PATH"] = old_db
            else:
                os.environ.pop("MEMORY_DB_PATH", None)
            if old_rerank is not None:
                os.environ["MEMORY_RERANKER_DISABLED"] = old_rerank
            else:
                os.environ.pop("MEMORY_RERANKER_DISABLED", None)
            if old_journal is not None:
                os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = old_journal
            else:
                os.environ.pop("MEMORY_WRITE_JOURNAL_ENABLED", None)


def test_l2r2_02_project_pushdown_beyond_900_rows_and_empty_postfilter_output():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "memory.db"
        old_db = os.environ.get("MEMORY_DB_PATH")
        old_rerank = os.environ.get("MEMORY_RERANKER_DISABLED")
        old_journal = os.environ.get("MEMORY_WRITE_JOURNAL_ENABLED")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        os.environ["MEMORY_RERANKER_DISABLED"] = "true"
        os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = "0"
        connection_pool.clear()
        try:
            bootstrap_temp_db_clean(db_path)
            conn = sqlite3.connect(str(db_path))
            try:
                for i in range(950):
                    content = (
                        "needlebeyond900 target match in big project"
                        if i == 949
                        else f"filler row big project {i}"
                    )
                    conn.execute(
                        "INSERT INTO memories (id, content, source_file, tags, category, created_at, tenant_id, metadata) "
                        "VALUES (?, ?, ?, '[]', 'lessons', ?, 'default', ?)",
                        (
                            f"lessons/big-{i:04d}",
                            content,
                            f"lessons/big-{i:04d}.md",
                            "2026-03-01T00:00:00Z",
                            json.dumps({"project": "big-proj", "idx": i}),
                        ),
                    )
                for i in range(1200):
                    conn.execute(
                        "INSERT INTO memories (id, content, source_file, tags, category, created_at, tenant_id, metadata) "
                        "VALUES (?, ?, ?, '[]', 'lessons', ?, 'default', ?)",
                        (
                            f"lessons/noise-{i:04d}",
                            f"needlebeyond900 needlebeyond900 other project noise {i}",
                            f"lessons/noise-{i:04d}.md",
                            "2026-03-02T00:00:00Z",
                            json.dumps({"project": "other-proj", "idx": i}),
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

            res = search_memories(
                db_path=db_path,
                query="needlebeyond900",
                limit=5,
                mode="fts",
                project="big-proj",
                light=True,
                rerank=False,
            )
            assert res["count"] == 1
            assert res["results"][0]["id"] == "lessons/big-0949"
            assert res["results"][0]["metadata"].get("project") == "big-proj"

            # Force Phase 14 project post-filter elimination by injecting a non-matching project row from _fts_search
            wrong_row = [
                (
                    "lessons/noise-0000",
                    "needlebeyond900 needlebeyond900 other project noise 0",
                    "lessons/noise-0000.md",
                    "[]",
                    "2026-03-02T00:00:00Z",
                    -1.0,
                    None,
                    None,
                    None,
                    None,
                    json.dumps({"project": "other-proj"}),
                    1,
                    None,
                )
            ]
            with mock.patch("search.orchestrator._fts_search", return_value=wrong_row):
                filtered_empty = search_memories(
                    db_path=db_path,
                    query="needlebeyond900_phase14_filter_miss",
                    limit=5,
                    mode="fts",
                    project="big-proj",
                    light=True,
                    rerank=False,
                )
            assert filtered_empty["count"] == 0
            assert filtered_empty["output"].startswith("No memories matched")
        finally:
            connection_pool.clear()
            if old_db is not None:
                os.environ["MEMORY_DB_PATH"] = old_db
            else:
                os.environ.pop("MEMORY_DB_PATH", None)
            if old_rerank is not None:
                os.environ["MEMORY_RERANKER_DISABLED"] = old_rerank
            else:
                os.environ.pop("MEMORY_RERANKER_DISABLED", None)
            if old_journal is not None:
                os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = old_journal
            else:
                os.environ.pop("MEMORY_WRITE_JOURNAL_ENABLED", None)


def test_l2r3_01_conjoint_temporal_counting_preserve_project_metadata():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "memory.db"
        old_db = os.environ.get("MEMORY_DB_PATH")
        old_rerank = os.environ.get("MEMORY_RERANKER_DISABLED")
        old_journal = os.environ.get("MEMORY_WRITE_JOURNAL_ENABLED")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        os.environ["MEMORY_RERANKER_DISABLED"] = "true"
        os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = "0"
        connection_pool.clear()
        try:
            bootstrap_temp_db_clean(db_path)
            conn = sqlite3.connect(str(db_path))
            try:
                rows_to_insert = [
                    (
                        "lessons/auth-ami",
                        "auth token verification details",
                        "lessons/auth-ami.md",
                        "lessons",
                        "2026-04-01T00:00:00Z",
                        "2026-04-01T00:00:00Z",
                        {"project": "ami-ide"},
                    ),
                    (
                        "lessons/billing-ami",
                        "billing ledger reconciliation details",
                        "lessons/billing-ami.md",
                        "lessons",
                        "2026-04-01T00:01:00Z",
                        "2026-04-01T00:01:00Z",
                        {"project": "ami-ide"},
                    ),
                    (
                        "lessons/auth-other",
                        "auth token verification in unrelated project",
                        "lessons/auth-other.md",
                        "lessons",
                        "2026-04-02T00:00:00Z",
                        "2026-04-02T00:00:00Z",
                        {"project": "other-proj"},
                    ),
                    (
                        "sessions/policy-ami-1",
                        "session update: authpolicy is strict",
                        "sessions/policy-ami-1.md",
                        "sessions",
                        "2026-04-01T10:00:00Z",
                        "2026-04-01T10:00:00Z",
                        {"project": "ami-ide"},
                    ),
                    (
                        "sessions/policy-ami-2",
                        "session update: authpolicy is mfa",
                        "sessions/policy-ami-2.md",
                        "sessions",
                        "2026-04-02T10:00:00Z",
                        "2026-04-02T10:00:00Z",
                        {"project": "ami-ide"},
                    ),
                    (
                        "sessions/policy-other-newer",
                        "session update: authpolicy is disabled",
                        "sessions/policy-other-newer.md",
                        "sessions",
                        "2026-04-05T10:00:00Z",
                        "2026-04-05T10:00:00Z",
                        {"project": "other-proj"},
                    ),
                ]
                for nid, content, sfile, cat, c_at, o_at, meta in rows_to_insert:
                    conn.execute(
                        "INSERT INTO memories (id, content, source_file, tags, category, created_at, observed_at, tenant_id, metadata) "
                        "VALUES (?, ?, ?, '[]', ?, ?, ?, 'default', ?)",
                        (nid, content, sfile, cat, c_at, o_at, json.dumps(meta)),
                    )
                conn.commit()
            finally:
                conn.close()

            # 1. Conjunction query ("auth and billing") preserves project metadata
            conj = search_memories(
                db_path=db_path,
                query="auth and billing",
                limit=5,
                mode="fts",
                project="ami-ide",
                light=True,
                rerank=False,
            )
            assert conj["count"] >= 2
            conj_ids = {r["id"] for r in conj["results"]}
            assert "lessons/auth-ami" in conj_ids
            assert "lessons/billing-ami" in conj_ids
            assert all(r["metadata"].get("project") == "ami-ide" for r in conj["results"])

            # 2. Temporal recency query ignores newer other-proj session & preserves project metadata
            temp = search_memories(
                db_path=db_path,
                query="what is authpolicy now",
                limit=5,
                mode="fts",
                category="sessions",
                project="ami-ide",
                light=True,
                rerank=False,
            )
            assert temp["count"] >= 1
            assert temp["results"][0]["id"] == "sessions/policy-ami-2"
            assert all(r["metadata"].get("project") == "ami-ide" for r in temp["results"])

            # 3. Counting query counts only ami-ide values (2, not 3) and preserves project metadata
            cnt = search_memories(
                db_path=db_path,
                query="how many times was authpolicy updated",
                limit=5,
                mode="fts",
                category="sessions",
                project="ami-ide",
                light=True,
                rerank=False,
            )
            assert cnt["count"] >= 1
            assert any("Total distinct count: 2." in r["content"] for r in cnt["results"])
            assert all(r["metadata"].get("project") == "ami-ide" for r in cnt["results"])
        finally:
            connection_pool.clear()
            if old_db is not None:
                os.environ["MEMORY_DB_PATH"] = old_db
            else:
                os.environ.pop("MEMORY_DB_PATH", None)
            if old_rerank is not None:
                os.environ["MEMORY_RERANKER_DISABLED"] = old_rerank
            else:
                os.environ.pop("MEMORY_RERANKER_DISABLED", None)
            if old_journal is not None:
                os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = old_journal
            else:
                os.environ.pop("MEMORY_WRITE_JOURNAL_ENABLED", None)


def test_l2r3_02_embedding_fallback_and_fusion_project_pushdown():
    from search.phases.fusion import _enhance_with_chunks
    from search.phases.retrieve import _fallback_embedding_search

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "memory.db"
        connection_pool.clear()
        try:
            bootstrap_temp_db_clean(db_path)
            conn = sqlite3.connect(str(db_path))
            try:
                for nid, tags, proj in [
                    ("lessons/emb-hit-ok", ["wave12"], "ami-ide"),
                    ("lessons/emb-wrong-tag", ["other"], "ami-ide"),
                    ("lessons/emb-wrong-proj", ["wave12"], "other-proj"),
                ]:
                    conn.execute(
                        "INSERT INTO memories (id, content, source_file, tags, category, created_at, tenant_id, metadata) "
                        "VALUES (?, ?, ?, ?, 'lessons', '2026-04-01T00:00:00Z', 'default', ?)",
                        (nid, f"embedding content for {nid}", f"{nid}.md", json.dumps(tags), json.dumps({"project": proj})),
                    )
                conn.commit()
            finally:
                conn.close()

            class _StubEmbeddingSearch:
                def search(self, query, dbp, limit=10, category=""):
                    return [
                        {"id": "lessons/emb-hit-ok", "score": 0.95},
                        {"id": "lessons/emb-wrong-tag", "score": 0.94},
                        {"id": "lessons/emb-wrong-proj", "score": 0.99},
                    ]

            db = connection_pool.get(str(db_path))
            try:
                with (
                    mock.patch("infra._lazy_imports.get_embedding_search", return_value=_StubEmbeddingSearch()),
                    mock.patch("search.phases.retrieve._get_embedding_score_threshold", return_value=0.1),
                ):
                    emb_rows = _fallback_embedding_search(
                        db,
                        "embedding probe",
                        db_path,
                        5,
                        "",
                        "",
                        tag_filter_sql=" AND (m.tags LIKE ?)",
                        tag_filter_params=('%"wave12"%',),
                        project="ami-ide",
                    )
                assert [r[0] for r in emb_rows] == ["lessons/emb-hit-ok"]

                enhanced = _enhance_with_chunks(
                    db,
                    [],
                    "embedding",
                    5,
                    False,
                    "",
                    merged_chunks=[
                        ("lessons/emb-hit-ok", 0, "c1", -1.0, 1),
                        ("lessons/emb-wrong-proj", 0, "c2", -2.0, 1),
                    ],
                    project="ami-ide",
                )
                assert [r[0] for r in enhanced] == ["lessons/emb-hit-ok"]
            finally:
                connection_pool.put(db)
        finally:
            connection_pool.clear()
