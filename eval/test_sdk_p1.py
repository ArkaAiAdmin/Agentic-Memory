"""Tests for P1 — SDK foundation (exceptions, models, utils, client, __init__)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from memory_common import connection_pool


def _fresh_db() -> Path:
    p = Path(tempfile.mkdtemp(prefix="sdk_p1_")) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(p)
    connection_pool.clear()
    return p


# ── P1a: exceptions ───────────────────────────────────────────────


class TestExceptions(unittest.TestCase):
    def test_can_import_all(self):
        from agentic_memory.exceptions import (
            AgenticMemoryError,
            ConnectionError,
            NotFoundError,
        )

        self.assertIsNotNone(AgenticMemoryError)
        self.assertIsNotNone(ConnectionError)
        self.assertIsNotNone(NotFoundError)

    def test_hierarchy(self):
        from agentic_memory.exceptions import (
            AgenticMemoryError,
            ConnectionError,
            ValidationError,
            NotFoundError,
        )

        self.assertTrue(issubclass(ConnectionError, AgenticMemoryError))
        self.assertTrue(issubclass(ValidationError, AgenticMemoryError))
        self.assertTrue(issubclass(NotFoundError, AgenticMemoryError))

    def test_can_raise_and_catch_base(self):
        from agentic_memory.exceptions import AgenticMemoryError, ValidationError

        with self.assertRaises(AgenticMemoryError):
            raise ValidationError("bad input")
        try:
            raise ValidationError("test")
        except AgenticMemoryError as e:
            self.assertIn("test", str(e))

    def test_all_exceptions_distinct(self):
        from agentic_memory.exceptions import (
            ConnectionError,
            NotFoundError,
            ValidationError,
            IntegrityError,
            MaintenanceError,
            SyncError,
            PermissionError,
            CircuitBreakerOpen,
            ConfigError,
        )

        names = {
            e.__name__
            for e in (
                ConnectionError,
                NotFoundError,
                ValidationError,
                IntegrityError,
                MaintenanceError,
                SyncError,
                PermissionError,
                CircuitBreakerOpen,
                ConfigError,
            )
        }
        self.assertEqual(len(names), 9)


# ── P1b: models ───────────────────────────────────────────────────


class TestModels(unittest.TestCase):
    def test_memory_result_defaults(self):
        from agentic_memory.models import MemoryResult

        r = MemoryResult(id="n1", content="hello")
        self.assertEqual(r.id, "n1")
        self.assertEqual(r.content, "hello")
        self.assertEqual(r.score, 0.0)
        self.assertEqual(r.tags, [])
        self.assertEqual(r.importance, 3)
        self.assertFalse(r.pinned)

    def test_search_results_empty(self):
        from agentic_memory.models import SearchResults

        sr = SearchResults()
        self.assertEqual(sr.results, [])
        self.assertEqual(sr.total, 0)
        self.assertEqual(sr.synthesis, "")

    def test_entity(self):
        from agentic_memory.models import Entity

        e = Entity(id="e1", name="Python", entity_type="language")
        self.assertEqual(e.name, "Python")
        self.assertEqual(e.description, "")

    def test_relation(self):
        from agentic_memory.models import Relation

        r = Relation(id="r1", source="e1", target="e2", relation_type="uses")
        self.assertEqual(r.weight, 1.0)

    def test_fact(self):
        from agentic_memory.models import Fact

        f = Fact(id="f1", subject="Alice", predicate="likes", obj="Python")
        self.assertEqual(f.confidence, 1.0)
        self.assertFalse(f.locked)

    def test_stats(self):
        from agentic_memory.models import Stats

        s = Stats(memories=10, vector_keys=5, chunks=3)
        self.assertEqual(s.memories, 10)
        self.assertEqual(s.facts, 0)
        self.assertEqual(s.entities, 0)

    def test_integrity_report(self):
        from agentic_memory.models import IntegrityReport

        r = IntegrityReport(passed=False, errors=["corrupt index"])
        self.assertFalse(r.passed)
        self.assertIn("corrupt", r.errors[0])

    def test_maintenance_result(self):
        from agentic_memory.models import MaintenanceResult

        mr = MaintenanceResult(operation="rebuild", success=True, message="ok")
        self.assertEqual(mr.operation, "rebuild")
        self.assertTrue(mr.success)

    def test_agent_info(self):
        from agentic_memory.models import AgentInfo

        ai = AgentInfo(agent_id="bot-1", display_name="Bot 1")
        self.assertEqual(ai.agent_id, "bot-1")
        self.assertEqual(ai.parent_agent, "")


# ── P1c: utils ────────────────────────────────────────────────────


class TestUtils(unittest.TestCase):
    def test_resolve_db_path_explicit(self):
        from agentic_memory.utils import resolve_db_path

        p = resolve_db_path("/tmp/test.db")
        self.assertEqual(p, Path("/tmp/test.db"))

    def test_resolve_db_path_from_env(self):
        from agentic_memory.utils import resolve_db_path

        db = _fresh_db()
        resolved = resolve_db_path()
        self.assertEqual(resolved, db)

    def test_resolve_db_path_fallback(self):
        from agentic_memory.utils import resolve_db_path

        os.environ.pop("MEMORY_DB_PATH", None)
        # Will use _lazy_imports.get_config() default
        resolved = resolve_db_path()
        self.assertIsInstance(resolved, Path)
        self.assertTrue(str(resolved).endswith(".db"))

    def test_parse_search_results_string_json(self):
        from agentic_memory.utils import parse_search_results

        raw = '{"results": [{"id": "n1", "content": "hello"}]}'
        items = parse_search_results(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "n1")

    def test_parse_search_results_dict(self):
        from agentic_memory.utils import parse_search_results

        raw = {"results": [{"id": "n1"}]}
        items = parse_search_results(raw)
        self.assertEqual(len(items), 1)

    def test_parse_search_results_list(self):
        from agentic_memory.utils import parse_search_results

        raw = [{"id": "n1"}]
        items = parse_search_results(raw)
        self.assertEqual(len(items), 1)

    def test_parse_search_results_empty(self):
        from agentic_memory.utils import parse_search_results

        self.assertEqual(parse_search_results(""), [])
        self.assertEqual(parse_search_results({}), [])
        self.assertEqual(parse_search_results(None), [])

    def test_get_db_connection_and_close(self):
        from agentic_memory.utils import get_db_connection, safe_close_db

        db = _fresh_db()
        conn = get_db_connection(db)
        self.assertIsNotNone(conn)
        result = conn.execute("SELECT 1").fetchone()
        self.assertEqual(result[0], 1)
        safe_close_db(conn)


# ── P1d: client ───────────────────────────────────────────────────


class TestClientCRUD(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    def test_save_returns_string(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        note_id = mc.save("test content")
        self.assertIsInstance(note_id, str)
        self.assertTrue(len(note_id) > 0)

    def test_save_with_tags(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        note_id = mc.save("tagged content", tags=["alpha", "beta"])
        self.assertIsInstance(note_id, str)

    def test_save_empty_content_raises(self):
        from agentic_memory import MemoryClient
        from agentic_memory.exceptions import ValidationError

        mc = MemoryClient(db_path=str(self.db))
        with self.assertRaises(ValidationError):
            mc.save("")
        with self.assertRaises(ValidationError):
            mc.save("   ")

    def test_save_bad_importance_raises(self):
        from agentic_memory import MemoryClient
        from agentic_memory.exceptions import ValidationError

        mc = MemoryClient(db_path=str(self.db))
        with self.assertRaises(ValidationError):
            mc.save("test", importance=0)
        with self.assertRaises(ValidationError):
            mc.save("test", importance=6)

    def test_search_returns_typed_results(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        mc.save("the quick brown fox")
        mc.save("jumps over the lazy dog")
        results = mc.search("fox")
        self.assertIsInstance(results.results, list)
        if results.results:
            r = results.results[0]
            self.assertIsInstance(r.id, str)
            self.assertIsInstance(r.content, str)

    def test_search_with_synthesis(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        mc.save("Python is a programming language")
        results = mc.search("Python", synthesize=True)
        self.assertIsInstance(results.synthesis, str)

    def test_get_existing(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        note_id = mc.save("hello world")
        result = mc.get(note_id)
        self.assertIsNotNone(result)
        self.assertEqual(result.content, "hello world")

    def test_get_missing(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        result = mc.get("nonexistent-id")
        self.assertIsNone(result)

    def test_delete_soft(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        note_id = mc.save("delete me")
        result = mc.delete(note_id)
        self.assertTrue(result)
        self.assertIsNone(mc.get(note_id))

    def test_restore(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        note_id = mc.save("restore me")
        mc.delete(note_id)
        restored = mc.restore(note_id)
        self.assertTrue(restored)
        self.assertIsNotNone(mc.get(note_id))

    def test_list(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        mc.save("first")
        mc.save("second")
        items = mc.list()
        self.assertGreaterEqual(len(items), 2)

    def test_list_with_category_filter(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        mc.save("proj note", category="projects")
        mc.save("lesson note", category="lessons")
        projects = mc.list(category="projects")
        self.assertGreaterEqual(len(projects), 1)
        self.assertEqual(projects[0].category, "projects")

    def test_stats(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        mc.save("stats test")
        stats = mc.stats()
        self.assertGreaterEqual(stats.memories, 1)
        self.assertIsInstance(stats.vector_keys, int)

    def test_context_manager(self):
        from agentic_memory import MemoryClient

        with MemoryClient(db_path=str(self.db)) as mc:
            note_id = mc.save("context test")
            self.assertIsInstance(note_id, str)

    def test_scan_injection(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        result = mc.scan_injection("normal text")
        self.assertIsInstance(result, dict)

    def test_quality_stats(self):
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(self.db))
        qs = mc.quality_stats()
        self.assertIsInstance(qs, dict)


# ── P1e: __init__ re-exports ──────────────────────────────────────


class TestInitReexports(unittest.TestCase):
    def test_memory_client_importable(self):
        from agentic_memory import MemoryClient

        self.assertIsNotNone(MemoryClient)

    def test_all_models_importable(self):
        from agentic_memory import (
            MemoryResult,
            SearchResults,
            Entity,
            Relation,
            Fact,
            Stats,
            IntegrityReport,
        )

        self.assertIsNotNone(MemoryResult)
        self.assertIsNotNone(SearchResults)
        self.assertIsNotNone(Entity)
        self.assertIsNotNone(Relation)
        self.assertIsNotNone(Fact)
        self.assertIsNotNone(Stats)
        self.assertIsNotNone(IntegrityReport)

    def test_all_exceptions_importable(self):
        from agentic_memory import (
            AgenticMemoryError,
            ConnectionError,
            NotFoundError,
            ValidationError,
        )

        self.assertIsNotNone(AgenticMemoryError)
        self.assertIsNotNone(ConnectionError)
        self.assertIsNotNone(NotFoundError)
        self.assertTrue(issubclass(ValidationError, AgenticMemoryError))

    def test_legacy_backward_compat(self):
        from agentic_memory import Memory, AgentMemory

        self.assertIsNotNone(Memory)
        self.assertIsNotNone(AgentMemory)

    def test_legacy_memory_works(self):
        from agentic_memory import Memory

        db = _fresh_db()
        m = Memory(db_path=str(db))
        note_id = m.add("legacy test")
        self.assertIsInstance(note_id, str)
        results = m.search("legacy")
        self.assertIsInstance(results, list)

    def test_main_importable(self):
        from agentic_memory import main

        self.assertIsNotNone(main)


if __name__ == "__main__":
    unittest.main()
