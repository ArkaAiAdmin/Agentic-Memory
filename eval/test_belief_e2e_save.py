"""E2E test: index_facts_for_memory creates belief_assertions (G1).

When feature_belief_layer is enabled, each fact upserted by
index_facts_for_memory should produce a corresponding row in
belief_assertions.  When the flag is disabled, no assertions should
be created.
"""
import sqlite3
import time
from unittest.mock import MagicMock, patch

from eval._fixtures import bootstrap_temp_db_clean
from fact.fact_extract import index_facts_for_memory


def _make_mock_config(belief_layer: bool = True):
    """Return a mock config with the attributes fact_extract.py reads."""
    cfg = MagicMock()
    cfg.knowledge_graph = True
    cfg.feature_belief_layer = belief_layer
    cfg.feature_temporal_kg = True
    cfg.llm = MagicMock()
    cfg.llm.extraction_hybrid_threshold = 0.5
    return cfg


class TestBeliefE2ESave:
    """belief_assertions should be populated by index_facts_for_memory."""

    def test_save_creates_belief_assertions(self, tmp_path):
        """With feature_belief_layer=True, facts produce belief_assertions."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO memories (id, content, category, created_at, updated_at, "
                "observed_at, source_file, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test-mem-1", "Alice is a corporate lawyer at Skadden", "lessons",
                 str(now), str(now), str(now), "", "{}"),
            )
            conn.commit()

            mock_cfg = _make_mock_config(belief_layer=True)
            with patch("fact.fact_extract.get_config", return_value=mock_cfg):
                result = index_facts_for_memory(
                    conn, "test-mem-1", "Alice is a corporate lawyer at Skadden"
                )

            assert result["facts"] > 0, f"Expected at least 1 fact, got {result}"

            # Check belief_assertions were created
            count = conn.execute("SELECT COUNT(*) FROM belief_assertions").fetchone()[0]
            assert count > 0, f"Expected belief_assertions, got {count}"

            # Verify the assertion has correct fields
            row = conn.execute(
                "SELECT fact_id, belief_status, epistemic_source, certainty_tier "
                "FROM belief_assertions LIMIT 1"
            ).fetchone()
            assert row is not None, "belief_assertions row should exist"
            assert row[1] == "active", f"Expected belief_status='active', got {row[1]}"
            assert row[2] == "agent", f"Expected epistemic_source='agent', got {row[2]}"
        finally:
            conn.close()

    def test_save_without_flag_creates_no_assertions(self, tmp_path):
        """With feature_belief_layer=False, no belief_assertions are created."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO memories (id, content, category, created_at, updated_at, "
                "observed_at, source_file, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test-mem-2", "Bob is a software engineer at Google", "lessons",
                 str(now), str(now), str(now), "", "{}"),
            )
            conn.commit()

            mock_cfg = _make_mock_config(belief_layer=False)
            with patch("fact.fact_extract.get_config", return_value=mock_cfg):
                result = index_facts_for_memory(
                    conn, "test-mem-2", "Bob is a software engineer at Google"
                )

            # Facts should still be extracted
            assert result["facts"] > 0, f"Expected at least 1 fact, got {result}"

            # But no belief_assertions
            count = conn.execute("SELECT COUNT(*) FROM belief_assertions").fetchone()[0]
            assert count == 0, f"Expected 0 belief_assertions, got {count}"
        finally:
            conn.close()

    def test_assertion_links_to_fact(self, tmp_path):
        """Each belief_assertion fact_id should reference a valid kg_facts row."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO memories (id, content, category, created_at, updated_at, "
                "observed_at, source_file, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test-mem-3", "Python is a programming language", "lessons",
                 str(now), str(now), str(now), "", "{}"),
            )
            conn.commit()

            mock_cfg = _make_mock_config(belief_layer=True)
            with patch("fact.fact_extract.get_config", return_value=mock_cfg):
                index_facts_for_memory(
                    conn, "test-mem-3", "Python is a programming language"
                )

            # Every belief_assertion should reference an existing kg_facts row
            orphans = conn.execute(
                "SELECT ba.fact_id FROM belief_assertions ba "
                "LEFT JOIN kg_facts kf ON ba.fact_id = kf.id "
                "WHERE kf.id IS NULL"
            ).fetchall()
            assert len(orphans) == 0, f"Orphaned belief_assertions: {orphans}"
        finally:
            conn.close()
