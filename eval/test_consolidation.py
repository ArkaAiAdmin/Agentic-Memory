"""Tests for consolidation.py — duplicate detection, clustering, merge suggestions."""

import os, sys, sqlite3, json, time

sys.path.insert(
    0,
    str(
        os.environ.get("MEMORY_INSTALL_ROOT")
        or os.path.expanduser("~/.config/agentic-memory")
    ),
)

from memory_config import install_root

sys.path.insert(0, str(install_root()))

import consolidation as co

# Production-faithful schema — keep in sync with db.py memories table definition.
_PRODUCTION_SCHEMA = """
    CREATE TABLE memories (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        source_file TEXT NOT NULL,
        tags TEXT DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        pinned INTEGER DEFAULT 0,
        importance INTEGER DEFAULT 3,
        decay TEXT DEFAULT 'none',
        score REAL DEFAULT 1.0,
        supersedes TEXT,
        repo_id TEXT,
        access_count INTEGER DEFAULT 1,
        success_score REAL DEFAULT 0.0,
        fitness_score REAL DEFAULT 1.0,
        conflict_policy TEXT DEFAULT 'supersede',
        version_vector TEXT DEFAULT '{}',
        logical_clock INTEGER DEFAULT 0,
        consolidation_state TEXT DEFAULT 'working',
        category TEXT,
        tier TEXT DEFAULT 'warm',
        importance_score REAL DEFAULT 0.5,
        deleted_at REAL,
        model_revision TEXT
    )
"""


class TestSimilarity:
    def test_jaccard_identical(self):
        assert co._jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0

    def test_jaccard_disjoint(self):
        assert co._jaccard_similarity({"a"}, {"b"}) == 0.0

    def test_jaccard_partial(self):
        assert 0.0 < co._jaccard_similarity({"a", "b"}, {"b", "c"}) < 1.0

    def test_jaccard_empty(self):
        assert co._jaccard_similarity(set(), set()) == 1.0

    def test_content_hash(self):
        h1 = co._content_hash("Hello World")
        h2 = co._content_hash("hello world")
        assert h1 == h2  # normalized

    def test_content_hash_different(self):
        h1 = co._content_hash("Hello World")
        h2 = co._content_hash("Goodbye World")
        assert h1 != h2


class TestDuplicateDetection:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(_PRODUCTION_SCHEMA)

    def teardown_method(self):
        self.conn.close()

    def test_exact_duplicates(self):
        now = time.time()
        self.conn.execute(
            "INSERT INTO memories (id, content, category, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "m1",
                "This is a test note about Python.",
                "lessons",
                "test",
                now,
                now,
                now,
            ),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, category, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "m2",
                "This is a test note about Python.",
                "lessons",
                "test",
                now,
                now,
                now,
            ),
        )
        self.conn.commit()
        dupes = co.detect_duplicates(self.conn)
        assert len(dupes) >= 1
        assert any(d["type"] == "exact" for d in dupes)

    def test_near_duplicates(self):
        now = time.time()
        self.conn.execute(
            "INSERT INTO memories (id, content, category, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "m1",
                "Python is a great language for machine learning and AI.",
                "lessons",
                "test",
                now,
                now,
                now,
            ),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, category, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "m2",
                "Python is a great language for machine learning and deep learning.",
                "lessons",
                "test",
                now,
                now,
                now,
            ),
        )
        self.conn.commit()
        dupes = co.detect_duplicates(self.conn, threshold=0.6)
        assert len(dupes) >= 1

    def test_no_duplicates(self):
        now = time.time()
        self.conn.execute(
            "INSERT INTO memories (id, content, category, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "m1",
                "Completely different topic about cooking.",
                "lessons",
                "test",
                now,
                now,
                now,
            ),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, category, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "m2",
                "Another unrelated note about sports.",
                "lessons",
                "test",
                now,
                now,
                now,
            ),
        )
        self.conn.commit()
        dupes = co.detect_duplicates(self.conn, threshold=0.9)
        assert len(dupes) == 0

    def test_single_note(self):
        now = time.time()
        self.conn.execute(
            "INSERT INTO memories (id, content, category, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("m1", "Just one note.", "lessons", "test", now, now, now),
        )
        self.conn.commit()
        dupes = co.detect_duplicates(self.conn)
        assert len(dupes) == 0


class TestClustering:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(_PRODUCTION_SCHEMA)

    def teardown_method(self):
        self.conn.close()

    def test_cluster_by_tags(self):
        now = time.time()
        tags1 = json.dumps(["python", "ml"])
        tags2 = json.dumps(["python", "ai"])
        tags3 = json.dumps(["cooking", "recipe"])
        self.conn.execute(
            "INSERT INTO memories (id, content, category, tags, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("m1", "Python ML note", "lessons", tags1, "test", now, now, now),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, category, tags, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("m2", "Python AI note", "lessons", tags2, "test", now, now, now),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, category, tags, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("m3", "Cooking note", "lessons", tags3, "test", now, now, now),
        )
        self.conn.commit()
        clusters = co.cluster_related(self.conn)
        assert len(clusters) >= 1
        # m1 and m2 should cluster together (share "python")
        assert any(len(c["members"]) >= 2 for c in clusters)


class TestMergeSuggestions:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(_PRODUCTION_SCHEMA)

    def teardown_method(self):
        self.conn.close()

    def test_merge_suggestions(self):
        now = time.time()
        self.conn.execute(
            "INSERT INTO memories (id, content, category, access_count, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "m1",
                "This is a test note about Python and machine learning.",
                "lessons",
                5,
                "test",
                now,
                now,
                now,
            ),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, category, access_count, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "m2",
                "This is a test note about Python and machine learning.",
                "lessons",
                2,
                "test",
                now,
                now,
                now,
            ),
        )
        self.conn.commit()
        suggestions = co.merge_suggestions(self.conn, duplicate_threshold=0.90)
        assert len(suggestions) >= 1
        assert suggestions[0]["keep"] == "m1"  # higher access count


class TestConsolidationStats:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(_PRODUCTION_SCHEMA)

    def teardown_method(self):
        self.conn.close()

    def test_stats(self):
        now = time.time()
        self.conn.execute(
            "INSERT INTO memories (id, content, category, tags, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("m1", "Note 1", "lessons", json.dumps(["python"]), "test", now, now, now),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, category, tags, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "m2",
                "Note 2",
                "lessons",
                json.dumps(["python", "ml"]),
                "test",
                now,
                now,
                now,
            ),
        )
        self.conn.commit()
        stats = co.consolidation_stats(self.conn)
        assert stats["enabled"] is True
        assert stats["total_notes"] == 2
        assert stats["unique_tags"] >= 2
