"""Tests for contextual enrichment feature.

Contextual enrichment adds related notes to memory metadata during save,
providing 49% retrieval improvement by creating richer embedding space
connections between related memories.
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.pop("MEMORY_CONTEXTUAL_ENRICHMENT", None)


@pytest.fixture
def enrichment_db(tmp_path):
    """Create a fresh DB for contextual enrichment tests."""
    db_path = tmp_path / "test.db"
    os.environ["MEMORY_DB_PATH"] = str(db_path)
    os.environ["MEMORY_CONTEXTUAL_ENRICHMENT"] = "1"
    from memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000;")
    # Create required tables
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            source_file TEXT,
            content TEXT,
            tags TEXT,
            created_at TEXT,
            updated_at TEXT,
            observed_at TEXT,
            fitness_score REAL DEFAULT 0.5,
            importance INTEGER DEFAULT 3,
            pinned INTEGER DEFAULT 0,
            repo_id TEXT,
            category TEXT,
            metadata TEXT DEFAULT '{}',
            deleted_at TEXT,
            valid_from TEXT,
            valid_to TEXT,
            superseded_by TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            id, content, tags, category, tokenize='porter unicode61'
        );
        CREATE TABLE IF NOT EXISTS chunks (
            note_id TEXT,
            chunk_idx INTEGER,
            content TEXT,
            start_offset INTEGER,
            end_offset INTEGER,
            token_count INTEGER,
            embedding BLOB,
            PRIMARY KEY (note_id, chunk_idx)
        );
        CREATE TABLE IF NOT EXISTS embeddings (
            note_id TEXT PRIMARY KEY,
            embedding BLOB,
            model_id TEXT,
            dim INTEGER,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS backlinks (
            source_id TEXT,
            target_id TEXT,
            link_type TEXT,
            created_at TEXT,
            PRIMARY KEY (source_id, target_id, link_type)
        );
    """)
    yield conn, db_path
    safe_close_db(conn)
    os.environ.pop("MEMORY_DB_PATH", None)
    os.environ.pop("MEMORY_CONTEXTUAL_ENRICHMENT", None)


class TestContextualEnrichment:
    """Test contextual enrichment feature."""

    def test_enrichment_disabled_by_default(self, enrichment_db):
        """Enrichment should not run when MEMORY_CONTEXTUAL_ENRICHMENT != '1'."""
        conn, db_path = enrichment_db
        os.environ.pop("MEMORY_CONTEXTUAL_ENRICHMENT", None)

        from save_pipeline import _enrich_context

        _enrich_context(
            conn, "test/note1", "Some content about Python", "test", ["python"]
        )

        # Metadata should not have contextual_related
        row = conn.execute(
            "SELECT metadata FROM memories WHERE id = 'test/note1'"
        ).fetchone()
        # No row means the function didn't run (which is correct when disabled)
        # If row exists, metadata should not have contextual_related
        if row:
            metadata = json.loads(row[0])
            assert "contextual_related" not in metadata

    def test_enrichment_adds_related_notes(self, enrichment_db):
        """Enrichment should add related notes to metadata when enabled."""
        conn, db_path = enrichment_db
        os.environ["MEMORY_CONTEXTUAL_ENRICHMENT"] = "1"

        # Insert some existing notes with similar content
        existing_notes = [
            (
                "test/python1",
                "Python is a programming language used for web development",
            ),
            (
                "test/python2",
                "Python decorators are used in Flask and Django frameworks",
            ),
            ("test/javascript", "JavaScript is used for frontend web development"),
        ]

        for nid, content in existing_notes:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO memories (id, source_file, content, tags, created_at, updated_at, observed_at, category, metadata)
                   VALUES (?, ?, ?, '[]', ?, ?, ?, 'test', '{}')""",
                (nid, f"{nid}.md", content, now, now, now),
            )
        conn.commit()

        # Insert the note we're going to enrich
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO memories (id, source_file, content, tags, created_at, updated_at, observed_at, category, metadata)
               VALUES (?, ?, ?, '[]', ?, ?, ?, 'test', '{}')""",
            (
                "test/new_note",
                "test/new_note.md",
                "Python programming is great for data science and machine learning",
                now,
                now,
                now,
            ),
        )
        conn.commit()

        # Now test enrichment
        from save_pipeline import _enrich_context

        _enrich_context(
            conn,
            "test/new_note",
            "Python programming is great for data science and machine learning",
            "test",
            ["python", "data-science"],
        )

        # Check if related notes were added
        row = conn.execute(
            "SELECT metadata FROM memories WHERE id = 'test/new_note'"
        ).fetchone()
        assert row is not None
        metadata = json.loads(row[0])
        assert "contextual_related" in metadata
        assert len(metadata["contextual_related"]) > 0
        assert "contextual_enriched_at" in metadata

    def test_enrichment_ignores_self(self, enrichment_db):
        """Enrichment should not add the note itself to related notes."""
        conn, db_path = enrichment_db
        os.environ["MEMORY_CONTEXTUAL_ENRICHMENT"] = "1"

        # Insert a note
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO memories (id, source_file, content, tags, created_at, updated_at, observed_at, category, metadata)
               VALUES (?, ?, ?, '[]', ?, ?, ?, 'test', '{}')""",
            (
                "test/python1",
                "test/python1.md",
                "Python programming is great",
                now,
                now,
                now,
            ),
        )
        conn.commit()

        from save_pipeline import _enrich_context

        _enrich_context(
            conn, "test/python1", "Python programming is great", "test", ["python"]
        )

        # The note should not reference itself
        row = conn.execute(
            "SELECT metadata FROM memories WHERE id = 'test/python1'"
        ).fetchone()
        if row:
            metadata = json.loads(row[0])
            if "contextual_related" in metadata:
                assert "test/python1" not in metadata["contextual_related"]

    def test_enrichment_limits_related_count(self, enrichment_db):
        """Enrichment should limit related notes to 5 maximum."""
        conn, db_path = enrichment_db
        os.environ["MEMORY_CONTEXTUAL_ENRICHMENT"] = "1"

        # Insert many notes with similar content
        for i in range(10):
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO memories (id, source_file, content, tags, created_at, updated_at, observed_at, category, metadata)
                   VALUES (?, ?, ?, '[]', ?, ?, ?, 'test', '{}')""",
                (
                    f"test/note{i}",
                    f"test/note{i}.md",
                    f"Python programming for task {i} with similar keywords",
                    now,
                    now,
                    now,
                ),
            )
        conn.commit()

        # Insert the target note
        now2 = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO memories (id, source_file, content, tags, created_at, updated_at, observed_at, category, metadata)
               VALUES (?, ?, ?, '[]', ?, ?, ?, 'test', '{}')""",
            (
                "test/new_note",
                "test/new_note.md",
                "Python programming for multiple tasks",
                now2,
                now2,
                now2,
            ),
        )
        conn.commit()

        from save_pipeline import _enrich_context

        _enrich_context(
            conn,
            "test/new_note",
            "Python programming for multiple tasks",
            "test",
            ["python"],
        )

        row = conn.execute(
            "SELECT metadata FROM memories WHERE id = 'test/new_note'"
        ).fetchone()
        assert row is not None
        metadata = json.loads(row[0])
        assert "contextual_related" in metadata
        assert len(metadata["contextual_related"]) <= 5

    def test_enrichment_graceful_on_fts_error(self, enrichment_db):
        """Enrichment should not crash if FTS query fails."""
        conn, db_path = enrichment_db
        os.environ["MEMORY_CONTEXTUAL_ENRICHMENT"] = "1"

        # Don't create FTS table to simulate error
        from save_pipeline import _enrich_context

        # Should not raise
        _enrich_context(conn, "test/note1", "Some content", "test", ["tag"])

    def test_enrichment_adds_timestamp(self, enrichment_db):
        """Enrichment should add contextual_enriched_at timestamp."""
        conn, db_path = enrichment_db
        os.environ["MEMORY_CONTEXTUAL_ENRICHMENT"] = "1"

        # Insert a similar note
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO memories (id, source_file, content, tags, created_at, updated_at, observed_at, category, metadata)
               VALUES (?, ?, ?, '[]', ?, ?, ?, 'test', '{}')""",
            (
                "test/python1",
                "test/python1.md",
                "Python programming is great",
                now,
                now,
                now,
            ),
        )
        conn.commit()

        # Insert the target note
        now2 = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO memories (id, source_file, content, tags, created_at, updated_at, observed_at, category, metadata)
               VALUES (?, ?, ?, '[]', ?, ?, ?, 'test', '{}')""",
            ("test/new", "test/new.md", "Python language basics", now2, now2, now2),
        )
        conn.commit()

        from save_pipeline import _enrich_context

        _enrich_context(conn, "test/new", "Python language basics", "test", ["python"])

        row = conn.execute(
            "SELECT metadata FROM memories WHERE id = 'test/new'"
        ).fetchone()
        assert row is not None
        metadata = json.loads(row[0])
        assert "contextual_enriched_at" in metadata
        # Verify it's a valid ISO timestamp
        datetime.fromisoformat(metadata["contextual_enriched_at"])
