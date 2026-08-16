"""Tests for knowledge_graph.py — temporal knowledge graph.

Covers: entity extraction, relation extraction, indexing, graph search,
graph stats, schema creation.
"""

import os
import sys
import importlib
import sqlite3

import pytest

sys.path.insert(0, os.path.expanduser("~/.config/agentic-memory"))

import knowledge_graph as kg
from knowledge_graph.kg_extract import extract_relations

# Force re-read of the env var after import
setattr(kg, "KG_ENABLED", True)

@pytest.fixture(autouse=True)
def reset_kg_module():
    setattr(kg, "KG_ENABLED", True)
    yield
    kg._clear_kg_enabled_cache()



class TestEntityExtraction:
    def test_extract_persons(self):
        text = "John Smith works with Alice Johnson on the transformer project."
        text = text + " John Smith and Alice Johnson are colleagues."
        entities = kg.extract_entities(text)
        names = [e[0] for e in entities]
        [e[1] for e in entities]
        assert any("John Smith" in n for n in names), f"No John Smith found in {names}"
        assert any("Alice Johnson" in n for n in names), (
            f"No Alice Johnson found in {names}"
        )

    def test_extract_organizations(self):
        text = "We use OpenAI for the API and Supabase for the database."
        entities = kg.extract_entities(text)
        names = [e[0] for e in entities]
        assert "OpenAI" in names, f"OpenAI not found in {names}"
        assert "Supabase" in names, f"Supabase not found in {names}"

    def test_extract_concepts(self):
        text = "The embedding model uses Python and the transformer architecture."
        entities = kg.extract_entities(text)
        names = [e[0] for e in entities]
        assert "python" in names, f"python not found in {names}"
        assert "transformer" in names, f"transformer not found in {names}"

    def test_extract_dates(self):
        text = "The meeting is on 2024-01-15 and the release is on January 20, 2024. The January 20, 2024 release is critical."
        entities = kg.extract_entities(text)
        names = [e[0] for e in entities]
        # ISO dates are filtered as garbage entities
        assert "2024-01-15" not in names, "ISO dates should be filtered"
        assert "January 20, 2024" in names, f"Natural date not found in {names}"

    def test_extract_emails(self):
        text = "Contact me at alice@example.com for more info. My email is alice@example.com."
        entities = kg.extract_entities(text)
        names = [e[0] for e in entities]
        assert "alice@example.com" in names, f"Email not found in {names}"

    def test_extract_empty(self):
        assert kg.extract_entities("") == []

    def test_extract_deduplication(self):
        text = "John Smith likes Python. John Smith uses Python."
        entities = kg.extract_entities(text)
        # Should not have duplicate entries
        seen = set()
        for name, etype in entities:
            key = (name.lower(), etype)
            assert key not in seen, f"Duplicate: {name} {etype}"
            seen.add(key)


class TestRelationExtraction:
    @pytest.mark.filterwarnings(
        "ignore:extract_relations is deprecated:DeprecationWarning"
    )
    def test_pattern_relations(self):
        text = "Alice works at Google. Bob created the API."
        relations = extract_relations(text)
        assert len(relations) > 0, "No relations extracted"

    @pytest.mark.filterwarnings(
        "ignore:extract_relations is deprecated:DeprecationWarning"
    )
    def test_co_occurrence(self):
        text = "Alice Johnson and Bob Smith discussed the database schema."
        relations = extract_relations(text)
        # Should extract co-occurrence relation
        co_occurs = [r for r in relations if r[1] == "co_occurs"]
        assert len(co_occurs) > 0, f"No co_occurs found in {relations}"

    @pytest.mark.filterwarnings(
        "ignore:extract_relations is deprecated:DeprecationWarning"
    )
    def test_extract_empty(self):
        assert extract_relations("") == []


class TestKGSchema:
    def test_ensure_kg_schema(self):
        conn = sqlite3.connect(":memory:")
        kg.ensure_kg_schema(conn)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "kg_entities" in tables
        assert "kg_edges" in tables
        conn.close()

    def test_ensure_kg_schema_idempotent(self):
        conn = sqlite3.connect(":memory:")
        kg.ensure_kg_schema(conn)
        kg.ensure_kg_schema(conn)  # second call should not fail
        conn.close()


class TestKGIndexing:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        kg.ensure_kg_schema(self.conn)

    def teardown_method(self):
        self.conn.close()

    def test_index_kg_for_memory(self):
        content = "John Smith works at OpenAI on the GPT model. Alice Johnson created the API."
        result = kg.index_kg_for_memory(self.conn, "test/memory1", content)
        assert result["entities"] > 0
        # Relations may be 0 if patterns don't match, but entities should exist

    def test_index_kg_disabled(self):
        old_val = getattr(kg, "KG_ENABLED", True)
        setattr(kg, "KG_ENABLED", False)
        result = kg.index_kg_for_memory(self.conn, "test/memory1", "some content")
        assert result == {"entities": 0, "relations": 0}
        setattr(kg, "KG_ENABLED", old_val)

    def test_entity_mentions_increment(self):
        content1 = "John Smith likes Python. John Smith loves Python."
        content2 = "John Smith uses Python every day. John Smith and Python are great."
        kg.index_kg_for_memory(self.conn, "test/m1", content1)
        kg.index_kg_for_memory(self.conn, "test/m2", content2)
        # John Smith should have mentions >= 2
        row = self.conn.execute(
            "SELECT mentions FROM kg_entities WHERE name = 'john smith'"
        ).fetchone()
        assert row is not None, "John Smith not found"
        assert row[0] >= 2, f"Expected mentions >= 2, got {row[0]}"


class TestGraphSearch:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        kg.ensure_kg_schema(self.conn)
        content = "John Smith works at OpenAI on the GPT model. John Smith works at OpenAI on the GPT model. Alice Johnson created the API. Alice Johnson created the API."
        kg.index_kg_for_memory(self.conn, "test/m1", content)

    def teardown_method(self):
        self.conn.close()

    def test_graph_search_found(self):
        result = kg.graph_search(self.conn, "John", limit=5)
        assert len(result["entities"]) > 0
        assert any("john" in e["name"].lower() for e in result["entities"])

    def test_graph_search_not_found(self):
        result = kg.graph_search(self.conn, "zzzznonexistent", limit=5)
        assert len(result["entities"]) == 0

    def test_graph_search_disabled(self):
        old_val = getattr(kg, "KG_ENABLED", True)
        setattr(kg, "KG_ENABLED", False)
        result = kg.graph_search(self.conn, "John")
        assert result == {"entities": [], "edges": []}
        setattr(kg, "KG_ENABLED", old_val)


class TestGraphStats:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        kg.ensure_kg_schema(self.conn)

    def teardown_method(self):
        self.conn.close()

    def test_graph_stats_empty(self):
        result = kg.graph_stats(self.conn)
        assert result["enabled"] is True
        assert result["entity_count"] == 0
        assert result["edge_count"] == 0

    def test_graph_stats_with_data(self):
        content = "John Smith works at OpenAI. Alice Johnson created the API. Bob Smith manages the team."
        kg.index_kg_for_memory(self.conn, "test/m1", content)
        result = kg.graph_stats(self.conn)
        assert result["entity_count"] > 0
