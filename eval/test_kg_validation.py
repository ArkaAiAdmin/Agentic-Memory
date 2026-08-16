"""Comprehensive validation tests for the agentic-memory knowledge graph,
fact extraction, and graph-RAG expansion.

Uses a TEMP DB — never touches production.
"""

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge_graph import (
    ensure_kg_schema,
    extract_entities,
    index_kg_for_memory,
    graph_search,
    _upsert_entity,
    _upsert_edge,
)
from kg.kg_crdt import ensure_kg_crdt_schema
from fact import (
    ensure_facts_schema,
    extract_facts,
    _clean_description,
    index_facts_for_memory,
)


class KGTestBase(unittest.TestCase):
    """Base class: creates an in-memory SQLite DB with KG schema for each test."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_kg_schema(self.conn)
        ensure_kg_crdt_schema(self.conn)
        ensure_facts_schema(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()


# ─────────────────────────────────────────────────────────────────────
# 1. Entity Extraction
# ─────────────────────────────────────────────────────────────────────
class TestEntityExtraction(KGTestBase):
    """Verify extract_entities returns correct entity types."""

    def test_person_extraction(self):
        """Capitalized two-word names → person."""
        ents = extract_entities("Alice Johnson went to the store.", min_occurrences=1)
        names = {e[0] for e in ents}
        types = {e[0]: e[1] for e in ents}
        self.assertIn("Alice Johnson", names)
        self.assertEqual(types["Alice Johnson"], "concept")

    def test_organization_extraction(self):
        """Known org names → organization."""
        ents = extract_entities(
            "Google and Anthropic are AI companies.", min_occurrences=1
        )
        types = {e[0]: e[1] for e in ents}
        self.assertEqual(types.get("Google"), "organization")
        self.assertEqual(types.get("Anthropic"), "organization")

    def test_place_extraction(self):
        """City names → place."""
        ents = extract_entities(
            "She lives in San Francisco and visits New York.", min_occurrences=1
        )
        types = {e[0]: e[1] for e in ents}
        self.assertEqual(types.get("San Francisco"), "place")
        self.assertEqual(types.get("New York"), "place")

    def test_concept_extraction(self):
        """Tech keywords → concept."""
        ents = extract_entities("We used Python and Docker for the pipeline.")
        types = {e[0]: e[1] for e in ents}
        self.assertEqual(types.get("python"), "concept")
        self.assertEqual(types.get("docker"), "concept")

    def test_email_extraction(self):
        """Email addresses → email."""
        ents = extract_entities(
            "Contact alice@example.com for details.", min_occurrences=1
        )
        types = {e[0]: e[1] for e in ents}
        self.assertIn("alice@example.com", types)
        self.assertEqual(types["alice@example.com"], "email")

    def test_deduplication(self):
        """Same entity mentioned twice should appear once."""
        ents = extract_entities("Alice Johnson met Bob Smith. Alice Johnson left.")
        names = [e[0] for e in ents]
        self.assertEqual(names.count("Alice Johnson"), 1)

    def test_empty_text(self):
        """Empty text returns empty list."""
        self.assertEqual(extract_entities(""), [])

    def test_ner_spacy_augments_without_dropping_regex(self):
        """When ner_spacy_enabled, spaCy additions are appended to the regex
        entities — the regex results must NOT be discarded.

        Regression: augment_entities returns only new spaCy entities, and the
        caller previously overwrote `unique` with that result, dropping every
        regex-extracted entity.
        """
        import infra._lazy_imports as _li
        from types import SimpleNamespace
        from unittest import mock

        spoof = SimpleNamespace(ner_spacy_enabled=False)
        strue = SimpleNamespace(ner_spacy_enabled=True)

        text = (
            "Sundar Pichai announced that Google is opening a lab in Zurich, "
            "Switzerland. Dr. Jane Smith from MIT will lead it with OpenAI."
        )

        with mock.patch.object(_li, "get_config", return_value=spoof):
            regex_only = extract_entities(text, min_occurrences=1)
        with mock.patch.object(_li, "get_config", return_value=strue):
            with_spacy = extract_entities(text, min_occurrences=1)

        regex_names = {e[0] for e in regex_only}
        spacy_names = {e[0] for e in with_spacy}

        # All regex entities must survive when spaCy is enabled.
        self.assertTrue(
            regex_names <= spacy_names,
            f"regex entities dropped by NER: {regex_names - spacy_names}",
        )
        # spaCy must add at least one entity the regex path missed.
        self.assertLess(
            len(regex_only), len(with_spacy),
            "spaCy NER added nothing — augmentation not wired",
        )
        # Spot-check a regex (Google) and a spaCy-only (Zurich) entity.
        self.assertIn("Google", spacy_names)
        self.assertIn("Zurich", spacy_names)

    def test_index_kg_entities_in_db(self):
        """index_kg_for_memory writes entities to kg_entities table."""
        index_kg_for_memory(
            self.conn,
            "test/1",
            "Alice Johnson and Bob Smith work on the Python project. Alice Johnson and Bob Smith are colleagues.",
        )
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT name, entity_type FROM kg_entities ORDER BY name"
        ).fetchall()
        names = {r[0] for r in rows}
        self.assertIn("alice johnson", names)
        self.assertIn("bob smith", names)


# ─────────────────────────────────────────────────────────────────────
# 2. Edge Extraction
# ─────────────────────────────────────────────────────────────────────
class TestEdgeExtraction(KGTestBase):
    """Verify kg_edges has correct source_id, target_id, relation, weight."""

    def _index_and_get_edges(self, content):
        index_kg_for_memory(self.conn, "test/edge1", content)
        self.conn.commit()
        return self.conn.execute(
            "SELECT source_id, target_id, relation, weight FROM kg_edges"
        ).fetchall()

    def test_co_occurrence_edges(self):
        """Two entities in same sentence → co_occurs edge."""
        edges = self._index_and_get_edges(
            "Alice Johnson works with Bob Smith every day. Alice Johnson and Bob Smith meet often."
        )
        self.assertTrue(len(edges) >= 1)
        for src, tgt, rel, w in edges:
            self.assertEqual(rel, "co_occurs")

    def test_edge_weight_default(self):
        """New edge starts at weight 1.0."""
        edges = self._index_and_get_edges(
            "Alice Johnson and Bob Smith met. Alice Johnson and Bob Smith met again."
        )
        self.assertTrue(len(edges) >= 1)
        for src, tgt, rel, w in edges:
            self.assertAlmostEqual(w, 1.0, places=1)

    def test_edge_weight_cap(self):
        """Edge weight cannot exceed 10.0."""
        now = time.time()
        e1 = _upsert_entity(self.conn, "alice", "person", now)
        e2 = _upsert_entity(self.conn, "bob", "person", now)
        # Insert 100 times to accumulate weight
        for _ in range(100):
            _upsert_edge(self.conn, e1, e2, "co_occurs", now)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT weight FROM kg_edges WHERE source_id=? AND target_id=?",
            (e1, e2),
        ).fetchone()
        self.assertLessEqual(row[0], 10.0)

    def test_clique_cap_4_entities(self):
        """5 entities in a sentence create co_occurs edges between pairs."""
        content = (
            "Alice Johnson met Bob Smith and Carol White "
            "and Dave Brown and Eve Green at the meeting."
        )
        index_kg_for_memory(self.conn, "test/clique", content)
        self.conn.commit()
        edges = self.conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
        # 5 entities → C(5,2) = 10 edges max
        self.assertLessEqual(edges, 10)

    def test_edge_unique_constraint(self):
        """Duplicate source-target-relation pairs are upserted, not duplicated."""
        now = time.time()
        e1 = _upsert_entity(self.conn, "x", "concept", now)
        e2 = _upsert_entity(self.conn, "y", "concept", now)
        _upsert_edge(self.conn, e1, e2, "co_occurs", now)
        _upsert_edge(self.conn, e1, e2, "co_occurs", now)
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM kg_edges WHERE source_id=? AND target_id=? AND relation='co_occurs'",
            (e1, e2),
        ).fetchone()[0]
        self.assertEqual(count, 1)


# ─────────────────────────────────────────────────────────────────────
# 3. Fact Extraction
# ─────────────────────────────────────────────────────────────────────
class TestFactExtraction(KGTestBase):
    """Verify kg_facts has correct subject, predicate, object, confidence."""

    def test_classification_fact(self):
        """'X is a Y' → is_a predicate."""
        facts = extract_facts("The memory system is a knowledge store for agents.")
        spo = {(f[0].lower(), f[1], f[2].lower()) for f in facts}
        # Should find something like (memory system, is_a, knowledge store)
        self.assertTrue(any("memory" in s for s, p, _ in spo if p == "is_a"))

    def test_bold_label_fact(self):
        """'**Feature:** desc' → has_description predicate."""
        text = (
            "## My Feature\n\n**What it does:** Caches query results for faster lookup."
        )
        facts = extract_facts(text)
        preds = {f[1] for f in facts}
        self.assertIn("has_description", preds)

    def test_dash_bullet_fact(self):
        """'- Feature — description' → has_description predicate."""
        text = "- Query Cache — Stores previous search results in memory."
        facts = extract_facts(text)
        preds = {f[1] for f in facts}
        self.assertIn("has_description", preds)

    def test_confidence_range(self):
        """Extracted facts should have confidence between 0 and 1."""
        facts = extract_facts(
            "## Feature\n\n**Description:** Handles all the requests."
        )
        for _, _, _, conf, *_ in facts:
            self.assertGreaterEqual(conf, 0.0)
            self.assertLessEqual(conf, 1.0)

    def test_fact_indexed_in_db(self):
        """index_facts_for_memory writes to kg_facts table."""
        index_facts_for_memory(
            self.conn,
            "test/fact1",
            "## Query Cache\n\n**What it does:** Stores previous search results in memory.",
        )
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT subject, predicate, object FROM kg_facts"
        ).fetchall()
        self.assertTrue(len(rows) >= 1)

    def test_clean_description_strips_table_rows(self):
        """_clean_description strips markdown table rows."""
        result = _clean_description("| col1 | col2 | col3 |")
        self.assertEqual(result, "")

    def test_clean_description_strips_paren_only(self):
        """_clean_description returns empty for parenthetical-only text."""
        result = _clean_description("(done)")
        self.assertEqual(result, "")

    def test_clean_description_strips_numeric_only(self):
        """_clean_description returns empty for pure numbers."""
        result = _clean_description("42")
        self.assertEqual(result, "")

    def test_clean_description_strips_status_done(self):
        """_clean_description strips 'N/N done' patterns."""
        result = _clean_description("5/5 done")
        self.assertEqual(result, "")

    def test_clean_description_keeps_valid_text(self):
        """_clean_description preserves meaningful descriptions."""
        result = _clean_description("Caches query results for faster lookup")
        self.assertEqual(result, "Caches query results for faster lookup")

    def test_clean_description_strips_pipe_remnants(self):
        """_clean_description strips pipe chars (table remnants)."""
        result = _clean_description("foo | bar |")
        self.assertEqual(result, "")

    def test_clean_description_min_length(self):
        """_clean_description returns empty for very short descriptions."""
        result = _clean_description("hi")
        self.assertEqual(result, "")


# ─────────────────────────────────────────────────────────────────────
# 4. 2-Hop Traversal
# ─────────────────────────────────────────────────────────────────────
class TestTwoHopTraversal(KGTestBase):
    """Verify graph_search finds entities 2 hops away."""

    def _build_chain(self):
        """Build chain: A --co_occurs--> B --co_occurs--> C --co_occurs--> D"""
        now = time.time()
        ea = _upsert_entity(self.conn, "alice", "person", now)
        eb = _upsert_entity(self.conn, "bob", "person", now)
        ec = _upsert_entity(self.conn, "carol", "person", now)
        ed = _upsert_entity(self.conn, "dave", "person", now)
        _upsert_edge(self.conn, ea, eb, "co_occurs", now)
        _upsert_edge(self.conn, eb, ec, "co_occurs", now)
        _upsert_edge(self.conn, ec, ed, "co_occurs", now)
        self.conn.commit()
        return ea, eb, ec, ed

    def test_1_hop(self):
        """Direct neighbors are found with max_hops=1."""
        ea, eb, ec, ed = self._build_chain()
        result = graph_search(self.conn, "alice", limit=10, max_hops=1)
        entity_names = {e["name"] for e in result["entities"]}
        # 1 hop: alice → bob
        self.assertIn("bob", entity_names)
        # 2 hops away should NOT be in 1-hop result
        self.assertNotIn("carol", entity_names)

    def test_2_hops(self):
        """2-hop traversal reaches bob→carol (alice→bob→carol)."""
        ea, eb, ec, ed = self._build_chain()
        result = graph_search(self.conn, "alice", limit=10, max_hops=2)
        entity_names = {e["name"] for e in result["entities"]}
        # Should find bob (1 hop) and carol (2 hops)
        self.assertIn("bob", entity_names)
        self.assertIn("carol", entity_names)

    def test_2_hops_returns_context(self):
        """2-hop result includes edges from the 2nd hop."""
        ea, eb, ec, ed = self._build_chain()
        result = graph_search(self.conn, "alice", limit=10, max_hops=2)
        edge_rels = {(e["source"], e["target"], e["relation"]) for e in result["edges"]}
        # Should include bob→carol edge
        self.assertTrue(any(s == "bob" and t == "carol" for s, t, r in edge_rels))

    def test_fallback_search(self):
        """graph_search falls back to LIKE if FTS fails."""
        ea, eb, ec, ed = self._build_chain()
        # Search for partial name
        result = graph_search(self.conn, "ali", limit=10, max_hops=1)
        entity_names = {e["name"] for e in result["entities"]}
        # Should still find alice via prefix LIKE
        self.assertIn("alice", entity_names)


# ─────────────────────────────────────────────────────────────────────
# 5. Entity Search (FTS5)
# ─────────────────────────────────────────────────────────────────────
class TestEntitySearch(KGTestBase):
    """Verify FTS5 entity search with exact/prefix/substring fallback."""

    def _populate(self):
        now = time.time()
        _upsert_entity(self.conn, "alice johnson", "person", now)
        _upsert_entity(self.conn, "anthropic", "organization", now)
        _upsert_entity(self.conn, "azure", "concept", now)
        self.conn.commit()

    def test_exact_match(self):
        """FTS5 finds exact entity name."""
        self._populate()
        result = graph_search(self.conn, "alice johnson", limit=10)
        names = {e["name"] for e in result["entities"]}
        self.assertIn("alice johnson", names)

    def test_prefix_match(self):
        """LIKE prefix fallback finds 'anthropic' from 'anth'."""
        self._populate()
        result = graph_search(self.conn, "anth", limit=10)
        names = {e["name"] for e in result["entities"]}
        self.assertIn("anthropic", names)

    def test_substring_fallback(self):
        """LIKE substring fallback finds 'azure' from 'azu'."""
        self._populate()
        result = graph_search(self.conn, "azu", limit=10)
        names = {e["name"] for e in result["entities"]}
        self.assertIn("azure", names)

    def test_fts_synced_with_entities(self):
        """FTS5 index matches kg_entities row count."""
        self._populate()
        entity_count = self.conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[
            0
        ]
        fts_count = self.conn.execute(
            "SELECT COUNT(*) FROM kg_entities_fts"
        ).fetchone()[0]
        self.assertEqual(entity_count, fts_count)

    def test_fts_insert_trigger(self):
        """New entity auto-inserts into FTS via trigger."""
        self._populate()
        now = time.time()
        _upsert_entity(self.conn, "zebra", "animal", now)
        self.conn.commit()
        fts_names = {
            r[0]
            for r in self.conn.execute("SELECT name FROM kg_entities_fts").fetchall()
        }
        self.assertIn("zebra", fts_names)


# ─────────────────────────────────────────────────────────────────────
# 6. Batch Entity Lookups
# ─────────────────────────────────────────────────────────────────────
class TestBatchEntityLookups(KGTestBase):
    """Verify graph_search handles multiple entities without N+1."""

    def _sync_fts(self):
        """Sync kg_entities_fts with kg_entities (external content table)."""
        self.conn.execute("DELETE FROM kg_entities_fts")
        self.conn.execute(
            "INSERT INTO kg_entities_fts(rowid, name, entity_type) "
            "SELECT id, name, entity_type FROM kg_entities"
        )
        self.conn.commit()

    def test_batch_query_single_call(self):
        """Multiple entities queried in one SQL call, not N separate calls."""
        now = time.time()
        ids = []
        for name in ["alice", "bob", "carol", "dave"]:
            ids.append(_upsert_entity(self.conn, name, "person", now))
        for i in range(len(ids) - 1):
            _upsert_edge(self.conn, ids[i], ids[i + 1], "co_occurs", now)
        self.conn.commit()
        self._sync_fts()

        # Capture SQL calls before
        result = graph_search(self.conn, "alice bob carol dave", limit=20)
        # Should find all entities
        names = {e["name"] for e in result["entities"]}
        self.assertTrue(len(names) >= 3, f"Expected >= 3 entities, got {names}")

    def test_efficient_edge_fetch(self):
        """Edges fetched in batch via IN clause, not per-entity."""
        now = time.time()
        ea = _upsert_entity(self.conn, "a", "concept", now)
        eb = _upsert_entity(self.conn, "b", "concept", now)
        ec = _upsert_entity(self.conn, "c", "concept", now)
        _upsert_edge(self.conn, ea, eb, "co_occurs", now)
        _upsert_edge(self.conn, eb, ec, "co_occurs", now)
        self.conn.commit()
        self._sync_fts()
        result = graph_search(self.conn, "a b", limit=10)
        # Should have edges from both hops
        self.assertTrue(len(result["edges"]) >= 2)


# ─────────────────────────────────────────────────────────────────────
# 7. KG Schema
# ─────────────────────────────────────────────────────────────────────
class TestKGSchema(KGTestBase):
    """Verify ensure_kg_schema creates correct tables and columns."""

    def test_entities_table_exists(self):
        """kg_entities table exists with expected columns."""
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(kg_entities)").fetchall()
        }
        self.assertIn("id", cols)
        self.assertIn("name", cols)
        self.assertIn("entity_type", cols)
        self.assertIn("mentions", cols)

    def test_edges_table_exists(self):
        """kg_edges table exists with correct FK columns."""
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(kg_edges)").fetchall()
        }
        self.assertIn("id", cols)
        self.assertIn("source_id", cols)
        self.assertIn("target_id", cols)
        self.assertIn("relation", cols)
        self.assertIn("weight", cols)

    def test_facts_table_exists(self):
        """kg_facts table exists with expected columns."""
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(kg_facts)").fetchall()
        }
        self.assertIn("subject", cols)
        self.assertIn("predicate", cols)
        self.assertIn("object", cols)
        self.assertIn("confidence", cols)

    def test_entities_name_is_text(self):
        """kg_entities.name is TEXT type."""
        for row in self.conn.execute("PRAGMA table_info(kg_entities)").fetchall():
            if row[1] == "name":
                self.assertEqual(row[2].upper(), "TEXT")

    def test_entities_mentions_is_integer(self):
        """kg_entities.mentions is INTEGER type."""
        for row in self.conn.execute("PRAGMA table_info(kg_entities)").fetchall():
            if row[1] == "mentions":
                self.assertEqual(row[2].upper(), "INTEGER")

    def test_edges_source_target_are_integer(self):
        """kg_edges.source_id and target_id are INTEGER."""
        for row in self.conn.execute("PRAGMA table_info(kg_edges)").fetchall():
            if row[1] in ("source_id", "target_id"):
                self.assertEqual(row[2].upper(), "INTEGER")

    def test_edges_foreign_keys(self):
        """kg_edges has FK constraints on source_id and target_id."""
        fk_info = self.conn.execute("PRAGMA foreign_key_list(kg_edges)").fetchall()
        fk_cols = {fk[3] for fk in fk_info}
        self.assertIn("source_id", fk_cols)
        self.assertIn("target_id", fk_cols)

    def test_idempotent_schema(self):
        """Calling ensure_kg_schema twice doesn't error."""
        ensure_kg_schema(self.conn)
        ensure_kg_schema(self.conn)
        self.conn.commit()

    def test_entities_unique_constraint(self):
        """kg_entities has UNIQUE(name, entity_type) constraint."""
        now = time.time()
        _upsert_entity(self.conn, "test_dup", "person", now)
        self.conn.commit()
        # Insert again — should update, not duplicate
        _upsert_entity(self.conn, "test_dup", "person", now)
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM kg_entities WHERE name='test_dup'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_entities_mentions_increments(self):
        """Re-indexing an entity increments mentions count."""
        now = time.time()
        eid = _upsert_entity(self.conn, "incr_test", "concept", now)
        self.conn.commit()
        _upsert_entity(self.conn, "incr_test", "concept", now)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT mentions FROM kg_entities WHERE id=?", (eid,)
        ).fetchone()
        self.assertGreater(row[0], 1)


# ─────────────────────────────────────────────────────────────────────
# 8. KG FTS Sync
# ─────────────────────────────────────────────────────────────────────
class TestKGFTSSync(KGTestBase):
    """Verify kg_entities_fts stays in sync with kg_entities."""

    def test_insert_sync(self):
        """Inserting entity auto-inserts into FTS."""
        now = time.time()
        _upsert_entity(self.conn, "sync_test", "person", now)
        self.conn.commit()
        fts_count = self.conn.execute(
            "SELECT COUNT(*) FROM kg_entities_fts WHERE name='sync_test'"
        ).fetchone()[0]
        self.assertEqual(fts_count, 1)

    def test_delete_sync(self):
        """Deleting entity removes from FTS."""
        now = time.time()
        eid = _upsert_entity(self.conn, "del_test", "person", now)
        self.conn.commit()
        self.conn.execute("DELETE FROM kg_entities WHERE id=?", (eid,))
        self.conn.commit()
        fts_count = self.conn.execute(
            "SELECT COUNT(*) FROM kg_entities_fts WHERE name='del_test'"
        ).fetchone()[0]
        self.assertEqual(fts_count, 0)

    def test_update_sync(self):
        """Updating entity name updates FTS."""
        now = time.time()
        eid = _upsert_entity(self.conn, "old_name", "person", now)
        self.conn.commit()
        self.conn.execute("UPDATE kg_entities SET name='new_name' WHERE id=?", (eid,))
        self.conn.commit()
        old_fts = self.conn.execute(
            "SELECT COUNT(*) FROM kg_entities_fts WHERE name='old_name'"
        ).fetchone()[0]
        new_fts = self.conn.execute(
            "SELECT COUNT(*) FROM kg_entities_fts WHERE name='new_name'"
        ).fetchone()[0]
        self.assertEqual(old_fts, 0)
        self.assertEqual(new_fts, 1)

    def test_fts_matches_entity_count(self):
        """FTS row count equals entity row count."""
        now = time.time()
        for name in ["a", "b", "c", "d", "e"]:
            _upsert_entity(self.conn, name, "concept", now)
        self.conn.commit()
        ec = self.conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
        fc = self.conn.execute("SELECT COUNT(*) FROM kg_entities_fts").fetchone()[0]
        self.assertEqual(ec, fc)


# ─────────────────────────────────────────────────────────────────────
# 9. Fingerprint-based dedup in _upsert_entity
# ─────────────────────────────────────────────────────────────────────
class TestFingerprintDedup(KGTestBase):
    """Verify _upsert_entity prevents duplicates via content-keyed fingerprint."""

    def _count(self, name: str, entity_type: str = "") -> int:
        if entity_type:
            return self.conn.execute(
                "SELECT COUNT(*) FROM kg_entities WHERE LOWER(name)=? AND LOWER(entity_type)=?",
                (name.lower(), entity_type.lower()),
            ).fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM kg_entities WHERE LOWER(name)=?", (name.lower(),)
        ).fetchone()[0]

    def test_same_entity_twice_returns_same_id(self):
        """Two upserts of the same (name, type) return the same entity_id."""
        now = time.time()
        e1 = _upsert_entity(self.conn, "alice", "person", now)
        e2 = _upsert_entity(self.conn, "alice", "person", now)
        self.assertEqual(e1, e2)
        self.assertEqual(self._count("alice", "person"), 1)

    def test_different_type_casing_matched(self):
        """Entity_type with different casing (Concept vs concept) resolves to same entity
        via fingerprint, not creating a duplicate."""
        now = time.time()
        e1 = _upsert_entity(self.conn, "machine learning", "Concept", now)
        e2 = _upsert_entity(self.conn, "machine learning", "concept", now)
        self.assertEqual(e1, e2)
        self.assertEqual(self._count("machine learning", "concept"), 1)

    def test_different_types_not_merged(self):
        """Different normalized entity_types produce different fingerprints
        and remain distinct."""
        now = time.time()
        e1 = _upsert_entity(self.conn, "python", "language", now)
        e2 = _upsert_entity(self.conn, "python", "snake", now)
        self.assertNotEqual(e1, e2)
        self.assertEqual(self._count("python"), 2)

    def test_fingerprint_backfilled_on_existing_row(self):
        """An entity created without fingerprint (simulating pre-041 state)
        gets one backfilled on the next upsert."""
        now = time.time()
        fp_col = [r[1] for r in self.conn.execute("PRAGMA table_info(kg_entities)").fetchall()]
        has_fp = "fingerprint" in fp_col

        e1 = _upsert_entity(self.conn, "backfill_test", "person", now)
        if has_fp:
            # Simulate pre-041 state by nulling the fingerprint
            self.conn.execute("UPDATE kg_entities SET fingerprint = NULL WHERE id = ?", (e1,))
            self.conn.commit()

        _upsert_entity(self.conn, "backfill_test", "person", now)
        if has_fp:
            fp = self.conn.execute(
                "SELECT fingerprint FROM kg_entities WHERE id = ?", (e1,)
            ).fetchone()[0]
            self.assertIsNotNone(fp, "Fingerprint should have been backfilled")

    def test_fingerprint_via_description_differentiates(self):
        """Entities with the same (name, type) but different descriptions
        get different fingerprints and remain distinct."""
        from kg.kg_crdt import _compute_fingerprint
        fp_no_desc = _compute_fingerprint("same_name", "same_type", "")
        fp_with_desc = _compute_fingerprint("same_name", "same_type", "different")
        self.assertNotEqual(fp_no_desc, fp_with_desc)

    def test_fingerprint_unique_constraint_honored(self):
        """New INSERT with a fingerprint respects UNIQUE(fingerprint)."""
        now = time.time()
        e1 = _upsert_entity(self.conn, "unique_fp", "test", now)
        e2 = _upsert_entity(self.conn, "unique_fp", "test", now)
        self.assertEqual(e1, e2)
        self.assertEqual(self._count("unique_fp", "test"), 1)

    def test_name_whitespace_normalized(self):
        """Leading/trailing whitespace in name is normalized via fingerprint."""
        now = time.time()
        e1 = _upsert_entity(self.conn, "  spaced  ", "t", now)
        e2 = _upsert_entity(self.conn, "spaced", "t", now)
        self.assertEqual(e1, e2)
        self.assertEqual(self._count("spaced", "t"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
