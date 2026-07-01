"""Tests for P3 — Maintenance, AgentMemory, SyncManager."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from infra.memory_common import connection_pool


def _fresh_db() -> Path:
    p = Path(tempfile.mkdtemp(prefix="sdk_p3_")) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(p)
    connection_pool.clear()
    return p


# ── P3a: Maintenance ──────────────────────────────────────────────


class TestMaintenanceInit(unittest.TestCase):
    def test_default_db_path(self):
        from agentic_memory import Maintenance

        m = Maintenance()
        self.assertIsInstance(m.db_path, Path)
        self.assertTrue(str(m.db_path).endswith(".db"))

    def test_explicit_db_path(self):
        from agentic_memory import Maintenance

        m = Maintenance(db_path="/tmp/test_maint.db")
        self.assertEqual(str(m.db_path), "/tmp/test_maint.db")
        self.assertEqual(str(m._memory_dir), "/tmp")


class TestMaintenanceRebuild(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("config.GLOBAL_SCRIPTS_DIR", new_callable=MagicMock)
    @patch("subprocess.run")
    def test_rebuild_success(self, mock_run, mock_scripts):
        from agentic_memory import Maintenance, MaintenanceResult

        mock_scripts.__truediv__.return_value.exists.return_value = True
        mock_run.return_value.stdout = "index rebuilt"
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        m = Maintenance(db_path=str(self.db))
        with patch("infra.cache.clear_all_caches"):
            result = m.rebuild("active")
        self.assertIsInstance(result, MaintenanceResult)
        self.assertTrue(result.success)
        self.assertEqual(result.operation, "rebuild")

    @patch("config.GLOBAL_SCRIPTS_DIR", new_callable=MagicMock)
    def test_rebuild_script_missing(self, mock_scripts):
        from agentic_memory import Maintenance, MaintenanceResult

        mock_scripts.__truediv__.return_value.exists.return_value = False
        m = Maintenance(db_path=str(self.db))
        result = m.rebuild()
        self.assertIsInstance(result, MaintenanceResult)
        self.assertFalse(result.success)
        self.assertIn("not found", result.message)

    @patch("config.GLOBAL_SCRIPTS_DIR", new_callable=MagicMock)
    @patch("subprocess.run")
    def test_rebuild_failure(self, mock_run, mock_scripts):
        from agentic_memory import Maintenance, MaintenanceResult

        mock_scripts.__truediv__.return_value.exists.return_value = True
        mock_run.side_effect = Exception("subprocess crashed")
        m = Maintenance(db_path=str(self.db))
        result = m.rebuild()
        self.assertIsInstance(result, MaintenanceResult)
        self.assertFalse(result.success)


class TestMaintenanceCheckIntegrity(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("memory_integrity.check_index_integrity")
    def test_integrity_pass(self, mock_check):
        from agentic_memory import Maintenance, IntegrityReport

        mock_check.return_value = {"ok": True, "findings": []}
        m = Maintenance(db_path=str(self.db))
        report = m.check_integrity()
        self.assertIsInstance(report, IntegrityReport)
        self.assertTrue(report.passed)
        self.assertEqual(report.errors, [])

    @patch("memory_integrity.check_index_integrity")
    def test_integrity_fail(self, mock_check):
        from agentic_memory import Maintenance, IntegrityReport

        mock_check.return_value = {
            "ok": False,
            "findings": [{"severity": "error", "message": "FK violation detected"}],
        }
        m = Maintenance(db_path=str(self.db))
        report = m.check_integrity(deep=True)
        self.assertIsInstance(report, IntegrityReport)
        self.assertFalse(report.passed)
        self.assertGreaterEqual(len(report.errors), 1)

    @patch("memory_integrity.check_index_integrity")
    def test_integrity_exception(self, mock_check):
        from agentic_memory import Maintenance, IntegrityReport

        mock_check.side_effect = ValueError("DB locked")
        m = Maintenance(db_path=str(self.db))
        report = m.check_integrity()
        self.assertIsInstance(report, IntegrityReport)
        self.assertFalse(report.passed)


class TestMaintenanceAudit(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    def test_audit_empty(self):
        from agentic_memory import Maintenance

        m = Maintenance(db_path=str(self.db))
        result = m.audit()
        self.assertIn("total_memories", result)
        self.assertEqual(result["total_memories"], 0)


class TestMaintenanceRun(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("mcp_maintenance.memory_maintenance")
    def test_run_operation(self, mock_maint):
        from agentic_memory import Maintenance

        mock_maint.return_value = '{"status": "ok"}'
        m = Maintenance(db_path=str(self.db))
        result = m.run("heartbeat")
        self.assertIsInstance(result, str)

    @patch("mcp_maintenance.memory_maintenance")
    def test_run_raises_on_failure(self, mock_maint):
        from agentic_memory import Maintenance
        from agentic_memory.exceptions import MaintenanceError

        mock_maint.side_effect = ValueError("bad op")
        m = Maintenance(db_path=str(self.db))
        with self.assertRaises(MaintenanceError):
            m.run("nonexistent_op")


class TestMaintenanceConsolidate(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("config.GLOBAL_SCRIPTS_DIR", new_callable=MagicMock)
    @patch("subprocess.run")
    def test_consolidate(self, mock_run, mock_scripts):
        from agentic_memory import Maintenance, MaintenanceResult

        mock_scripts.__truediv__.return_value.exists.return_value = True
        mock_run.return_value.stdout = "consolidated"
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        m = Maintenance(db_path=str(self.db))
        with patch("infra.cache.clear_all_caches"):
            result = m.consolidate()
        self.assertIsInstance(result, MaintenanceResult)
        self.assertTrue(result.success)


# ── P3b: AgentMemory ──────────────────────────────────────────────


class TestAgentMemoryInit(unittest.TestCase):
    @patch("agent_context.init_agent")
    def test_init_defaults(self, mock_init):
        from agentic_memory import AgentMemory

        mock_init.return_value.agent_id = "test-agent"
        mock_init.return_value.display_name = "test-agent"
        mock_init.return_value.namespace = "agents/test-agent"

        am = AgentMemory(agent_id="test-agent", db_path="/tmp/test_agent.db")
        self.assertEqual(am._agent_id, "test-agent")
        mock_init.assert_called_once_with(
            agent_id="test-agent",
            display_name="test-agent",
            parent_agent=None,
        )

    @patch("agent_context.init_agent")
    def test_init_with_parent(self, mock_init):
        from agentic_memory import AgentMemory

        mock_init.return_value.agent_id = "child"
        mock_init.return_value.namespace = "agents/parent/child"

        am = AgentMemory(
            agent_id="child",
            display_name="Child Agent",
            parent_agent="parent",
            db_path="/tmp/test_agent.db",
        )
        self.assertEqual(am._display_name, "Child Agent")
        self.assertEqual(am._parent_agent, "parent")


class TestAgentMemorySaveSearch(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("agent_context.init_agent")
    @patch("agent_context.agent_save")
    def test_save(self, mock_save, mock_init):
        from agentic_memory import AgentMemory

        mock_init.return_value.agent_id = "test-agent"
        mock_init.return_value.display_name = "test-agent"
        mock_init.return_value.namespace = "agents/test-agent"
        mock_save.return_value = "note-123"

        am = AgentMemory(agent_id="test-agent", db_path=str(self.db))
        note_id = am.save("hello", category="agents", tags=["test"])
        self.assertEqual(note_id, "note-123")
        mock_save.assert_called_once()

    @patch("agent_context.init_agent")
    @patch("agent_context.agent_search")
    def test_search(self, mock_search, mock_init):
        from agentic_memory import AgentMemory, SearchResults

        mock_init.return_value.agent_id = "test-agent"
        mock_init.return_value.display_name = "test-agent"
        mock_init.return_value.namespace = "agents/test-agent"
        mock_search.return_value = {
            "results": [
                {
                    "id": "n1",
                    "content": "found it",
                    "final_score": 0.95,
                    "tags": [],
                    "category": "agents",
                    "created_at": "2026-01-01",
                    "pinned": False,
                    "importance": 3,
                }
            ]
        }

        am = AgentMemory(agent_id="test-agent", db_path=str(self.db))
        results = am.search("test query")
        self.assertIsInstance(results, SearchResults)
        self.assertEqual(len(results.results), 1)
        self.assertEqual(results.results[0].content, "found it")

    @patch("agent_context.init_agent")
    def test_client_property(self, mock_init):
        from agentic_memory import AgentMemory, MemoryClient

        mock_init.return_value.agent_id = "test-agent"
        mock_init.return_value.display_name = "test-agent"
        mock_init.return_value.namespace = "agents/test-agent"

        am = AgentMemory(agent_id="test-agent", db_path=str(self.db))
        client = am.client
        self.assertIsInstance(client, MemoryClient)

    @patch("agent_context.init_agent")
    def test_info_property(self, mock_init):
        from agentic_memory import AgentMemory, AgentInfo

        mock_init.return_value.agent_id = "my-agent"
        mock_init.return_value.display_name = "My Agent"
        mock_init.return_value.namespace = "agents/my-agent"

        am = AgentMemory(
            agent_id="my-agent",
            display_name="My Agent",
            parent_agent="parent",
            db_path=str(self.db),
        )
        info = am.info
        self.assertIsInstance(info, AgentInfo)
        self.assertEqual(info.agent_id, "my-agent")
        self.assertEqual(info.parent_agent, "parent")


class TestAgentMemoryListClear(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        self._patcher = patch("agent_context.init_agent")
        mock_init = self._patcher.start()
        mock_init.return_value.agent_id = "test-agent"
        mock_init.return_value.display_name = "test-agent"
        mock_init.return_value.namespace = "agents/test-agent"
        self.addCleanup(self._patcher.stop)

    def test_list_default_namespace(self):
        from agentic_memory import AgentMemory

        am = AgentMemory(agent_id="test-agent", db_path=str(self.db))

        with patch("agent_context.get_agent") as mock_get:
            mock_get.return_value.namespace = "default"
            results = am.list(limit=10)
            self.assertIsInstance(results, list)

    def test_clear_default_namespace_returns_zero(self):
        from agentic_memory import AgentMemory

        am = AgentMemory(agent_id="test-agent", db_path=str(self.db))

        with patch("agent_context.get_agent") as mock_get:
            mock_get.return_value.namespace = "default"
            n = am.clear()
            self.assertEqual(n, 0)


class TestAgentMemoryStaticMethods(unittest.TestCase):
    @patch("agent_context.list_agents")
    @patch("agent_context.init_agent")
    def test_list_agents(self, mock_init, mock_list):
        from agentic_memory import AgentMemory, AgentInfo

        mock_init.return_value.agent_id = "test"
        mock_init.return_value.display_name = "test"
        mock_init.return_value.namespace = "agents/test"
        mock_list.return_value = {
            "agent-1": {
                "display_name": "Agent One",
                "parent_agent": "",
                "namespace": "agents/agent-1",
            }
        }

        agents = AgentMemory.list_agents()
        self.assertEqual(len(agents), 1)
        self.assertIsInstance(agents[0], AgentInfo)

    @patch("agent_context.clear_agent")
    @patch("agent_context.init_agent")
    def test_reset(self, mock_init, mock_clear):
        from agentic_memory import AgentMemory

        mock_init.return_value.agent_id = "test"
        mock_init.return_value.display_name = "test"
        mock_init.return_value.namespace = "agents/test"

        am = AgentMemory(agent_id="test", db_path="/tmp/test_agent.db")
        am.reset()
        mock_clear.assert_called_once()

    @patch("agent_context.init_agent")
    def test_context_manager(self, mock_init):
        from agentic_memory import AgentMemory

        mock_init.return_value.agent_id = "test"
        mock_init.return_value.display_name = "test"
        mock_init.return_value.namespace = "agents/test"

        with patch("agent_context.clear_agent") as mock_clear:
            with AgentMemory(agent_id="test", db_path="/tmp/test_agent.db") as am:
                self.assertIsNotNone(am)
            mock_clear.assert_called_once()


# ── P3c: SyncManager ──────────────────────────────────────────────


class TestSyncManagerInit(unittest.TestCase):
    def test_default_db_path(self):
        from agentic_memory import SyncManager

        sm = SyncManager()
        self.assertIsInstance(sm._db_path, Path)

    def test_explicit_db_path(self):
        from agentic_memory import SyncManager

        sm = SyncManager(db_path="/tmp/test_sync.db")
        self.assertEqual(str(sm._db_path), "/tmp/test_sync.db")


class TestSyncManagerSync(unittest.TestCase):
    @patch("crdt_merge.crdt_sync_all")
    @patch("save.crdt_helpers._crdt_agent_id")
    def test_sync_success(self, mock_crdt_id, mock_sync):
        from agentic_memory import SyncManager

        mock_crdt_id.return_value = "local-agent"
        mock_sync.return_value = {
            "applied": 2,
            "conflicted": 0,
            "rejected": 0,
            "total": 2,
        }

        sm = SyncManager(db_path="/tmp/test_sync.db")
        result = sm.sync(
            "remote-agent",
            {
                "note-1": ["content1", "file1.md", 1, '{"a":1}', 1],
                "note-2": ["content2", "file2.md", 2, '{"a":2}', 2],
            },
        )
        self.assertEqual(result["applied"], 2)
        self.assertEqual(result["total"], 2)

    @patch("crdt_merge.crdt_sync_all")
    @patch("save.crdt_helpers._crdt_agent_id")
    def test_sync_invalid_data_raises(self, mock_crdt_id, mock_sync):
        from agentic_memory import SyncManager
        from agentic_memory.exceptions import SyncError

        mock_crdt_id.return_value = "local-agent"

        sm = SyncManager(db_path="/tmp/test_sync.db")
        with self.assertRaises(SyncError):
            sm.sync("remote-agent", {"bad-note": "not-a-list"})

    @patch("crdt_merge.crdt_sync_all")
    @patch("save.crdt_helpers._crdt_agent_id")
    def test_sync_string_result(self, mock_crdt_id, mock_sync):
        from agentic_memory import SyncManager

        mock_crdt_id.return_value = "local-agent"
        mock_sync.return_value = '{"applied": 1, "total": 1}'

        sm = SyncManager(db_path="/tmp/test_sync.db")
        result = sm.sync("remote-agent", {"n1": ["c", "f", 0, "{}", 0]})
        self.assertEqual(result["applied"], 1)


class TestSyncManagerStatus(unittest.TestCase):
    @patch("_lazy_imports.get_config")
    def test_status_no_peers(self, mock_config):
        from agentic_memory import SyncManager

        mock_config.return_value.sync_peers = []
        mock_config.return_value.sync_enable_server = False

        sm = SyncManager()
        status = sm.status()
        self.assertEqual(status["peers"], [])
        self.assertFalse(status["sync_enabled"])


class TestSyncManagerShare(unittest.TestCase):
    @patch("memory_sharing.MULTI_AGENT_ENABLED", True)
    @patch("memory_sharing.share_memory")
    def test_share_success(self, mock_share):
        from agentic_memory import SyncManager

        mock_share.return_value = {"ok": True, "success": True}

        sm = SyncManager(db_path="/tmp/test_sync.db")
        result = sm.share("note-1", "agent-b")
        self.assertTrue(result)

    @patch("memory_sharing.MULTI_AGENT_ENABLED", False)
    @patch("memory_sharing.share_memory")
    def test_share_disabled(self, mock_share):
        from agentic_memory import SyncManager

        sm = SyncManager(db_path="/tmp/test_sync.db")
        result = sm.share("note-1", "agent-b")
        self.assertFalse(result)

    @patch("memory_sharing.MULTI_AGENT_ENABLED", True)
    @patch("memory_sharing.share_memory")
    def test_share_failure_raises(self, mock_share):
        from agentic_memory import SyncManager
        from agentic_memory.exceptions import SyncError

        mock_share.side_effect = RuntimeError("network error")

        sm = SyncManager(db_path="/tmp/test_sync.db")
        with self.assertRaises(SyncError):
            sm.share("note-1", "agent-b")


class TestSyncManagerListShared(unittest.TestCase):
    @patch("memory_sharing.MULTI_AGENT_ENABLED", True)
    @patch("memory_sharing.list_shared_memories")
    def test_list_shared(self, mock_list):
        from agentic_memory import SyncManager

        mock_list.return_value = [{"id": "s1", "content": "shared note"}]

        sm = SyncManager(db_path="/tmp/test_sync.db")
        results = sm.list_shared(agent_id="agent-b", limit=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "s1")

    @patch("memory_sharing.MULTI_AGENT_ENABLED", False)
    def test_list_shared_disabled(self):
        from agentic_memory import SyncManager

        sm = SyncManager(db_path="/tmp/test_sync.db")
        results = sm.list_shared()
        self.assertEqual(results, [])


class TestSyncManagerImportShared(unittest.TestCase):
    @patch("memory_sharing.MULTI_AGENT_ENABLED", True)
    @patch("memory_sharing.import_shared_memory")
    def test_import_shared(self, mock_import):
        from agentic_memory import SyncManager

        mock_import.return_value = {"ok": True}

        sm = SyncManager(db_path="/tmp/test_sync.db")
        result = sm.import_shared("s1", "agent-c")
        self.assertTrue(result)

    @patch("memory_sharing.MULTI_AGENT_ENABLED", False)
    def test_import_shared_disabled(self):
        from agentic_memory import SyncManager

        sm = SyncManager(db_path="/tmp/test_sync.db")
        result = sm.import_shared("s1", "agent-c")
        self.assertFalse(result)


class TestSyncManagerAutoShare(unittest.TestCase):
    @patch("memory_sharing.MULTI_AGENT_ENABLED", True)
    @patch("memory_sharing.auto_share_high_value")
    def test_auto_share(self, mock_auto):
        from agentic_memory import SyncManager

        mock_auto.return_value = {"scanned": 10, "shared": 3}

        sm = SyncManager(db_path="/tmp/test_sync.db")
        result = sm.auto_share(min_importance=4)
        self.assertEqual(result["shared"], 3)

    @patch("memory_sharing.MULTI_AGENT_ENABLED", True)
    @patch("memory_sharing.list_share_candidates")
    def test_auto_share_dry_run(self, mock_candidates):
        from agentic_memory import SyncManager

        mock_candidates.return_value = [{"id": "n1"}]

        sm = SyncManager(db_path="/tmp/test_sync.db")
        result = sm.auto_share(dry_run=True)
        self.assertTrue(result["dry_run"])

    @patch("memory_sharing.MULTI_AGENT_ENABLED", False)
    def test_auto_share_disabled(self):
        from agentic_memory import SyncManager

        sm = SyncManager(db_path="/tmp/test_sync.db")
        result = sm.auto_share()
        self.assertFalse(result["enabled"])


if __name__ == "__main__":
    unittest.main()
