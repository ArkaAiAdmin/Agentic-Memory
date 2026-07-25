"""Tests for P2 — KnowledgeGraph + TemporalKG."""

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
    p = Path(tempfile.mkdtemp(prefix="sdk_p2_")) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(p)
    connection_pool.clear()
    return p


# ── P2a: KnowledgeGraph ───────────────────────────────────────────


class TestKnowledgeGraphInit(unittest.TestCase):
    def test_default_db_path(self):
        from agentic_memory import KnowledgeGraph

        kg = KnowledgeGraph()
        self.assertIsInstance(kg._db_path, Path)
        self.assertTrue(str(kg._db_path).endswith(".db"))

    def test_explicit_db_path(self):
        from agentic_memory import KnowledgeGraph

        kg = KnowledgeGraph(db_path="/tmp/test_kg.db")
        self.assertEqual(str(kg._db_path), "/tmp/test_kg.db")


class TestKnowledgeGraphSearch(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("knowledge_graph.graph_search_db")
    def test_search_returns_entities(self, mock_search):
        from agentic_memory import KnowledgeGraph, Entity

        mock_search.return_value = {
            "entities": [
                {
                    "id": 1,
                    "name": "Python",
                    "entity_type": "language",
                    "description": "A programming language",
                    "mention_count": 5,
                }
            ]
        }
        kg = KnowledgeGraph(db_path=str(self.db))
        results = kg.search("Python")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Entity)
        self.assertEqual(results[0].name, "Python")
        self.assertEqual(results[0].entity_type, "language")

    @patch("knowledge_graph.graph_search_db")
    def test_search_empty(self, mock_search):
        from agentic_memory import KnowledgeGraph

        mock_search.return_value = {"entities": []}
        kg = KnowledgeGraph(db_path=str(self.db))
        results = kg.search("nonexistent")
        self.assertEqual(results, [])

    @patch("knowledge_graph.graph_search_db")
    def test_search_string_json_response(self, mock_search):
        from agentic_memory import KnowledgeGraph, Entity

        mock_search.return_value = (
            '{"entities": [{"id": 1, "name": "Go", "entity_type": "language"}]}'
        )
        kg = KnowledgeGraph(db_path=str(self.db))
        results = kg.search("Go")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Entity)


class TestKnowledgeGraphSearchFacts(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("fact_extraction.facts_search_db")
    def test_search_facts(self, mock_search):
        from agentic_memory import KnowledgeGraph, Fact

        mock_search.return_value = [
            {
                "id": 1,
                "subject": "Alice",
                "predicate": "likes",
                "object": "Python",
                "confidence": 0.95,
                "category": "preference",
                "source_note_id": "n1",
                "locked": False,
            }
        ]
        kg = KnowledgeGraph(db_path=str(self.db))
        results = kg.search_facts("Alice")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Fact)
        self.assertEqual(results[0].subject, "Alice")
        self.assertEqual(results[0].confidence, 0.95)

    def test_search_facts_empty_inputs(self):
        from agentic_memory.kg import _as_list

        self.assertEqual(_as_list(None), [])
        self.assertEqual(_as_list(""), [])
        self.assertEqual(_as_list("   "), [])
        self.assertEqual(_as_list({}), [])
        self.assertEqual(_as_list([]), [])


class TestKnowledgeGraphShortestPath(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("infra.db.open_db")
    @patch("kg.kg_traversal.find_shortest_path")
    def test_shortest_path(self, mock_find, mock_open):
        from agentic_memory import KnowledgeGraph, Relation

        mock_conn = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_conn
        mock_find.return_value = [
            {"name": "Python", "entity_type": "language"},
            {"id": 1, "relation": "influenced"},
            {"name": "JavaScript", "entity_type": "language"},
        ]
        kg = KnowledgeGraph(db_path=str(self.db))
        path = kg.shortest_path("Python", "JavaScript")
        self.assertEqual(len(path), 1)
        self.assertIsInstance(path[0], Relation)
        self.assertEqual(path[0].source, "Python")
        self.assertEqual(path[0].target, "JavaScript")
        self.assertEqual(path[0].relation_type, "influenced")

    @patch("infra.db.open_db")
    @patch("kg.kg_traversal.find_shortest_path")
    def test_shortest_path_no_path(self, mock_find, mock_open):
        from agentic_memory import KnowledgeGraph

        mock_conn = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_conn
        mock_find.return_value = []
        kg = KnowledgeGraph(db_path=str(self.db))
        path = kg.shortest_path("Python", "Rust")
        self.assertEqual(path, [])


class TestKnowledgeGraphTraverse(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("infra.db.open_db")
    @patch("kg.kg_traversal.find_neighbors")
    def test_traverse(self, mock_neighbors, mock_open):
        from agentic_memory import KnowledgeGraph, Entity, Relation

        mock_conn = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_conn
        mock_neighbors.return_value = [
            {
                "source": {"name": "Python", "entity_type": "language"},
                "target": {"name": "Django", "entity_type": "framework"},
                "relation": "used_by",
                "id": 1,
                "weight": 0.9,
            }
        ]
        kg = KnowledgeGraph(db_path=str(self.db))
        entities, relations = kg.traverse("Python")
        self.assertEqual(len(entities), 2)
        self.assertEqual(len(relations), 1)
        self.assertIsInstance(entities[0], Entity)
        self.assertIsInstance(relations[0], Relation)

    @patch("infra.db.open_db")
    @patch("kg.kg_traversal.find_neighbors")
    def test_traverse_empty(self, mock_neighbors, mock_open):
        from agentic_memory import KnowledgeGraph

        mock_conn = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_conn
        mock_neighbors.return_value = []
        kg = KnowledgeGraph(db_path=str(self.db))
        entities, relations = kg.traverse("Unknown")
        self.assertEqual(entities, [])
        self.assertEqual(relations, [])


class TestKnowledgeGraphListFacts(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("agentic_memory.kg.get_db_connection")
    @patch("agentic_memory.kg.safe_close_db")
    def test_list_facts_empty(self, mock_close, mock_conn):
        from agentic_memory import KnowledgeGraph

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.return_value.execute.return_value = mock_cursor

        kg = KnowledgeGraph(db_path=str(self.db))
        facts = kg.list_facts()
        self.assertEqual(facts, [])


class TestKnowledgeGraphStats(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("knowledge_graph.graph_stats_db")
    def test_stats(self, mock_stats):
        from agentic_memory import KnowledgeGraph

        mock_stats.return_value = {
            "enabled": True,
            "entity_count": 42,
            "edge_count": 100,
        }
        kg = KnowledgeGraph(db_path=str(self.db))
        stats = kg.stats()
        self.assertEqual(stats["entity_count"], 42)
        self.assertTrue(stats["enabled"])


# ── P2b: TemporalKG ───────────────────────────────────────────────


class TestTemporalKGInit(unittest.TestCase):
    def test_default_db_path(self):
        from agentic_memory import TemporalKG

        tk = TemporalKG()
        self.assertIsInstance(tk._db_path, Path)

    def test_explicit_db_path(self):
        from agentic_memory import TemporalKG

        tk = TemporalKG(db_path="/tmp/test_temporal.db")
        self.assertEqual(str(tk._db_path), "/tmp/test_temporal.db")


class TestTemporalKGSearch(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("mcp_surface.mcp_audit.memory_temporal_query")
    def test_search(self, mock_query):
        from agentic_memory import TemporalKG, Fact

        mock_query.return_value = {
            "rows": [
                {
                    "id": 1,
                    "subject": "Alice",
                    "predicate": "prefers",
                    "object": "dark mode",
                    "confidence": 0.9,
                    "locked": False,
                }
            ]
        }
        tk = TemporalKG(db_path=str(self.db))
        results = tk.search("dark mode")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Fact)
        self.assertEqual(results[0].subject, "Alice")

    @patch("mcp_surface.mcp_audit.memory_temporal_query")
    def test_search_empty(self, mock_query):
        from agentic_memory import TemporalKG

        mock_query.return_value = {}
        tk = TemporalKG(db_path=str(self.db))
        results = tk.search("nothing")
        self.assertEqual(results, [])


class TestTemporalKGContradictions(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("mcp_surface.mcp_audit.memory_temporal_contradictions")
    def test_contradictions(self, mock_contra):
        from agentic_memory import TemporalKG

        mock_contra.return_value = {
            "rows": [
                {
                    "old": "fact_1",
                    "new": "fact_2",
                    "reason": "contradicted",
                    "contradiction_score": 0.95,
                    "transaction_time": "2026-06-23T12:00:00",
                }
            ]
        }
        tk = TemporalKG(db_path=str(self.db))
        events = tk.contradictions()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "contradicted")

    @patch("mcp_surface.mcp_audit.memory_temporal_contradictions")
    def test_contradictions_empty(self, mock_contra):
        from agentic_memory import TemporalKG

        mock_contra.return_value = {}
        tk = TemporalKG(db_path=str(self.db))
        events = tk.contradictions()
        self.assertEqual(events, [])


class TestTemporalKGQueryAtTime(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("mcp_surface.mcp_audit.memory_temporal_query")
    def test_query_facts_at_time(self, mock_query):
        from agentic_memory import TemporalKG, Fact

        mock_query.return_value = {
            "rows": [
                {
                    "id": 1,
                    "subject": "User",
                    "predicate": "uses",
                    "object": "Python",
                    "valid_at": "2026-01-01",
                    "locked": False,
                }
            ]
        }
        tk = TemporalKG(db_path=str(self.db))
        facts = tk.query_facts_at_time(1760000000.0)
        self.assertEqual(len(facts), 1)
        self.assertIsInstance(facts[0], Fact)
        self.assertEqual(facts[0].predicate, "uses")


class TestTemporalKGChangedSince(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("mcp_surface.mcp_audit.memory_temporal_query")
    def test_changed_since(self, mock_query):
        from agentic_memory import TemporalKG, Fact

        mock_query.return_value = {
            "rows": [
                {
                    "id": 5,
                    "subject": "Config",
                    "predicate": "set_to",
                    "object": "true",
                    "locked": False,
                }
            ]
        }
        tk = TemporalKG(db_path=str(self.db))
        facts = tk.query_changed_since(1760000000.0)
        self.assertEqual(len(facts), 1)
        self.assertIsInstance(facts[0], Fact)


class TestTemporalKGSupersessionChain(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("mcp_surface.mcp_audit.memory_temporal_query")
    def test_supersession_chain(self, mock_query):
        from agentic_memory import TemporalKG

        mock_query.return_value = {
            "rows": [
                {
                    "id": 1,
                    "subject": "Theme",
                    "predicate": "is",
                    "object": "light",
                    "locked": False,
                },
                {
                    "id": 2,
                    "subject": "Theme",
                    "predicate": "is",
                    "object": "dark",
                    "supersedes": "1",
                    "locked": False,
                },
            ]
        }
        tk = TemporalKG(db_path=str(self.db))
        chain = tk.query_supersession_chain(1)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].obj, "light")
        self.assertEqual(chain[1].obj, "dark")


class TestTemporalKGInvalidate(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    @patch("fact_temporal.invalidate_fact")
    def test_invalidate_fact(self, mock_invalidate):
        from agentic_memory import TemporalKG

        mock_invalidate.return_value = True
        db_path = str(self.db)

        with patch("agentic_memory.temporal.sqlite_write_queue.start_session") as mock_conn:
            mock_conn.return_value.__enter__ = lambda s: mock_conn.return_value
            mock_conn.return_value.__exit__ = lambda *a: None
            mock_conn.return_value.execute.return_value.fetchone.return_value = None
            tk = TemporalKG(db_path=db_path)
            result = tk.invalidate_fact(1)
            self.assertTrue(result)


class TestTemporalKGHelpers(unittest.TestCase):
    def test_parse_fact_all_fields(self):
        from agentic_memory.temporal import TemporalKG
        from agentic_memory import Fact

        fact = TemporalKG._parse_fact(
            {
                "id": 10,
                "subject": "S",
                "predicate": "P",
                "object": "O",
                "confidence": 0.85,
                "category": "test",
                "source_note_id": "n1",
                "event_time": "2026-01-01",
                "event_time_granularity": "day",
                "valid_at": "2026-01-01",
                "invalid_at": None,
                "superseded_by": "",
                "supersedes": "",
                "contradiction_score": 0.75,
                "locked": True,
            }
        )
        self.assertIsInstance(fact, Fact)
        self.assertEqual(fact.id, "10")
        self.assertEqual(fact.confidence, 0.85)
        self.assertTrue(fact.locked)
        self.assertEqual(fact.contradiction_score, 0.75)

    def test_parse_json_various_inputs(self):
        from agentic_memory.temporal import TemporalKG

        self.assertEqual(TemporalKG._parse_json({"a": 1}), {"a": 1})
        self.assertEqual(TemporalKG._parse_json([1, 2]), [1, 2])
        self.assertEqual(TemporalKG._parse_json('{"x": 1}'), {"x": 1})
        self.assertEqual(TemporalKG._parse_json("invalid{json"), {})
        self.assertEqual(TemporalKG._parse_json(""), {})
        self.assertEqual(TemporalKG._parse_json(None), {})

    def test_extract_rows_various_shapes(self):
        from agentic_memory.temporal import TemporalKG

        self.assertEqual(TemporalKG._extract_rows({"rows": [1, 2]}), [1, 2])
        self.assertEqual(TemporalKG._extract_rows({"data": [3]}), [3])
        self.assertEqual(TemporalKG._extract_rows([4, 5]), [4, 5])
        self.assertEqual(TemporalKG._extract_rows({}), [])
        self.assertEqual(TemporalKG._extract_rows(""), [])
        self.assertEqual(TemporalKG._extract_rows(None), [])


if __name__ == "__main__":
    unittest.main()
