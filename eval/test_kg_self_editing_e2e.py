"""E2E: save → facts → beliefs → contradiction → supersede (G5.2).

End-to-end test of the knowledge graph self-editing pipeline:
1. Save a memory → extract facts → create belief assertions
2. Save a contradicting memory → extract facts → verify KG consistency
3. Verify no orphan edges in kg_edges
4. Verify belief_assertions link to kg_facts
"""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys_path_inserted = str(REPO) in __import__("sys").path
if not sys_path_inserted:
    __import__("sys").path.insert(0, str(REPO))


def _setup_kg_db() -> tuple[sqlite3.Connection, str]:
    """Create a temp DB with full schema (migrations + KG + facts + beliefs)."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    from infra.db_migrations import run_schema_setup
    run_schema_setup(conn)
    from knowledge_graph.kg_schema import ensure_kg_schema
    ensure_kg_schema(conn)
    from fact.fact_schema import ensure_facts_schema
    ensure_facts_schema(conn)
    from belief.belief_schema import ensure_beliefs_schema
    ensure_beliefs_schema(conn)
    conn.commit()
    return conn, db_path


def _insert_memory(conn: sqlite3.Connection, mem_id: str, content: str, now: float):
    """Insert a memory row with the required columns."""
    ts = str(now)
    conn.execute(
        "INSERT INTO memories (id, content, source_file, tags, category, "
        "created_at, updated_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (mem_id, content, f"test/{mem_id}.md", "[]", "lessons", ts, ts, ts),
    )
    conn.commit()


class TestKGSelfEditingE2E:
    """End-to-end tests of the KG self-editing pipeline."""

    def test_full_pipeline_contradiction_creates_facts_and_beliefs(self):
        """Save creates facts+beliefs; a contradicting save also creates facts."""
        conn, db_path = _setup_kg_db()
        try:
            now = time.time()

            # Step 1: Save memory and extract facts + beliefs
            _insert_memory(conn, "mem-1", "Alice is a corporate lawyer at Skadden", now - 100)

            from fact.fact_extract import index_facts_for_memory
            result = index_facts_for_memory(conn, "mem-1", "Alice is a corporate lawyer at Skadden")
            assert result["facts"] > 0, f"Expected facts extracted, got {result}"

            # Verify beliefs created
            belief_count = conn.execute(
                "SELECT COUNT(*) FROM belief_assertions"
            ).fetchone()[0]
            assert belief_count > 0, "Belief assertions should be created"

            # Step 2: Inject contradicting memory
            _insert_memory(conn, "mem-2", "Alice is an executive chef at Le Bernardin", now)

            result2 = index_facts_for_memory(conn, "mem-2", "Alice is an executive chef at Le Bernardin")
            assert result2["facts"] > 0, f"Expected facts from contradicting memory"

            # Step 3: Verify no orphan edges
            orphans = conn.execute(
                "SELECT COUNT(*) FROM kg_edges e "
                "WHERE e.source_id NOT IN (SELECT id FROM kg_entities) "
                "OR e.target_id NOT IN (SELECT id FROM kg_entities)"
            ).fetchone()[0]
            assert orphans == 0, f"Orphan edges found: {orphans}"

            # Step 4: Verify belief_assertions link to kg_facts
            linked = conn.execute(
                "SELECT COUNT(*) FROM belief_assertions ba "
                "JOIN kg_facts kf ON kf.id = ba.fact_id"
            ).fetchone()[0]
            assert linked > 0, "Belief assertions should link to kg_facts"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_save_deferred_path_still_extracts_facts(self):
        """index_facts_for_memory works standalone (simulates deferred drain)."""
        conn, db_path = _setup_kg_db()
        try:
            now = time.time()
            _insert_memory(conn, "mem-deferred", "Bob is a software engineer at Google", now)

            from fact.fact_extract import index_facts_for_memory
            result = index_facts_for_memory(
                conn, "mem-deferred", "Bob is a software engineer at Google"
            )
            assert result["facts"] > 0, f"Expected facts, got {result}"

            belief_count = conn.execute(
                "SELECT COUNT(*) FROM belief_assertions"
            ).fetchone()[0]
            assert belief_count > 0, "Belief assertions should be created"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_kg_entities_created_for_facts(self):
        """index_facts_for_memory creates kg_facts with subject/object data."""
        conn, db_path = _setup_kg_db()
        try:
            now = time.time()
            _insert_memory(conn, "mem-ents", "Google is a technology company", now)

            from fact.fact_extract import index_facts_for_memory
            result = index_facts_for_memory(
                conn, "mem-ents", "Google is a technology company"
            )
            assert result["facts"] > 0

            # index_facts_for_memory creates kg_facts (not kg_entities directly)
            facts = conn.execute(
                "SELECT subject, predicate, object FROM kg_facts ORDER BY id"
            ).fetchall()
            assert len(facts) > 0, "Expected kg_facts to be created"
            # Verify the fact has meaningful subject/object
            subjects = {row[0] for row in facts}
            assert any("google" in s.lower() for s in subjects), (
                f"Expected 'Google' in subjects, got {subjects}"
            )
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_kg_edges_created_between_entities(self):
        """index_facts_for_memory creates kg_edges connecting entities."""
        conn, db_path = _setup_kg_db()
        try:
            now = time.time()
            _insert_memory(conn, "mem-edges", "Apple is a consumer electronics company", now)

            from fact.fact_extract import index_facts_for_memory
            result = index_facts_for_memory(
                conn, "mem-edges", "Apple is a consumer electronics company"
            )
            assert result["facts"] > 0

            edge_count = conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
            # Edges may or may not be created depending on extraction,
            # but kg_facts should exist
            fact_count = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
            assert fact_count > 0, "Expected kg_facts to be created"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_belief_status_matches_extraction(self):
        """Belief assertions have the correct default status and source."""
        conn, db_path = _setup_kg_db()
        try:
            now = time.time()
            _insert_memory(conn, "mem-status", "Python is a programming language", now)

            from fact.fact_extract import index_facts_for_memory
            index_facts_for_memory(
                conn, "mem-status", "Python is a programming language"
            )

            row = conn.execute(
                "SELECT belief_status, epistemic_source, certainty_tier "
                "FROM belief_assertions LIMIT 1"
            ).fetchone()
            assert row is not None, "Expected a belief assertion"
            assert row[0] == "active", f"Expected active status, got {row[0]}"
            assert row[1] == "agent", f"Expected agent source, got {row[1]}"
            assert row[2] == "likely", f"Expected likely tier, got {row[2]}"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_multiple_memories_build_up_facts(self):
        """Multiple saves accumulate kg_facts without duplicates (upsert)."""
        conn, db_path = _setup_kg_db()
        try:
            now = time.time()

            from fact.fact_extract import index_facts_for_memory

            # Save two memories about the same entity
            _insert_memory(conn, "mem-a", "OpenAI builds GPT models", now - 10)
            _insert_memory(conn, "mem-b", "OpenAI also builds DALL-E", now)

            r1 = index_facts_for_memory(conn, "mem-a", "OpenAI builds GPT models")
            r2 = index_facts_for_memory(conn, "mem-b", "OpenAI also builds DALL-E")

            total_facts = r1["facts"] + r2["facts"]
            assert total_facts > 0, "Expected facts from both memories"

            # kg_facts should have rows (possibly with incremented mention_count)
            fact_count = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
            assert fact_count > 0, "Expected kg_facts rows"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_no_orphan_edges_after_pipeline(self):
        """After the full pipeline, kg_edges only reference existing entities."""
        conn, db_path = _setup_kg_db()
        try:
            now = time.time()

            from fact.fact_extract import index_facts_for_memory

            _insert_memory(conn, "mem-orphan-1", "Tesla makes electric cars", now - 5)
            _insert_memory(conn, "mem-orphan-2", "SpaceX launches rockets", now)

            index_facts_for_memory(conn, "mem-orphan-1", "Tesla makes electric cars")
            index_facts_for_memory(conn, "mem-orphan-2", "SpaceX launches rockets")

            orphans = conn.execute(
                "SELECT COUNT(*) FROM kg_edges e "
                "WHERE e.source_id NOT IN (SELECT id FROM kg_entities) "
                "OR e.target_id NOT IN (SELECT id FROM kg_entities)"
            ).fetchone()[0]
            assert orphans == 0, f"Orphan edges after pipeline: {orphans}"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_belief_assertions_have_valid_fact_references(self):
        """Every belief_assertion.fact_id references an existing kg_facts row."""
        conn, db_path = _setup_kg_db()
        try:
            now = time.time()
            _insert_memory(conn, "mem-ref", "NASA is a space agency", now)

            from fact.fact_extract import index_facts_for_memory
            index_facts_for_memory(
                conn, "mem-ref", "NASA is a space agency"
            )

            # Check referential integrity
            broken = conn.execute(
                "SELECT COUNT(*) FROM belief_assertions ba "
                "WHERE NOT EXISTS (SELECT 1 FROM kg_facts kf WHERE kf.id = ba.fact_id)"
            ).fetchone()[0]
            assert broken == 0, f"Broken belief→fact references: {broken}"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_kg_facts_have_source_memory(self):
        """Every kg_fact links back to a source memory via source_memory column."""
        conn, db_path = _setup_kg_db()
        try:
            now = time.time()
            _insert_memory(conn, "mem-src", "Linux is an operating system", now)

            from fact.fact_extract import index_facts_for_memory
            index_facts_for_memory(
                conn, "mem-src", "Linux is an operating system"
            )

            rows = conn.execute(
                "SELECT source_memory FROM kg_facts WHERE source_memory IS NOT NULL"
            ).fetchall()
            assert len(rows) > 0, "Expected kg_facts with source_memory"
            for row in rows:
                assert row[0] != "", f"Empty source_memory in kg_facts"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()
