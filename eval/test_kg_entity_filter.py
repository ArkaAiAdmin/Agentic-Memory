"""Tests for KG entity quality filters (P3.2, 2026-06-19).

Covers:
- _is_stopword() correctly identifies stopwords
- _is_valid_entity() rejects too-short, punctuation, stopwords, non-alnum edge
- _is_valid_entity() accepts meaningful entities
- _backfill_kg_graph() drops noise entities when env vars are set
- _backfill_kg_graph() computes real mention counts (was hardcoded to 1)
- min_mentions env var filters out singletons
- min_length env var filters out short tokens
"""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load_backfill():
    """Load backfill_all module fresh."""
    import importlib.util as _importlib_util

    spec = _importlib_util.spec_from_file_location(
        "backfill_all", str(REPO / "backfill_all.py")
    )
    if spec is None:
        raise RuntimeError(
            f"Could not load backfill_all.py from {REPO / 'backfill_all.py'}"
        )
    mod = _importlib_util.module_from_spec(spec)
    sys.modules["backfill_all_test"] = mod
    loader = spec.loader
    if loader is None:
        raise RuntimeError("spec.loader is None — cannot exec_module")
    loader.exec_module(mod)
    return mod


class TestIsStopword(unittest.TestCase):
    def test_common_articles_are_stopwords(self):
        bf = _load_backfill()
        for w in ("a", "an", "the", "this", "that", "is", "are", "was"):
            self.assertTrue(bf._is_stopword(w), f"expected '{w}' to be a stopword")

    def test_common_verbs_are_stopwords(self):
        bf = _load_backfill()
        for w in ("use", "make", "create", "add", "delete", "update", "check"):
            self.assertTrue(bf._is_stopword(w), f"expected '{w}' to be a stopword")

    def test_markdown_tokens_are_stopwords(self):
        bf = _load_backfill()
        for w in ("section", "header", "code", "table", "list", "item", "note"):
            self.assertTrue(bf._is_stopword(w), f"expected '{w}' to be a stopword")

    def test_meaningful_words_are_not_stopwords(self):
        bf = _load_backfill()
        for w in (
            "python",
            "database",
            "memory",
            "config",
            "fastapi",
            "react",
            "agent",
            "schema",
            "index",
            "vector",
            "embedding",
        ):
            self.assertFalse(bf._is_stopword(w), f"'{w}' should NOT be a stopword")

    def test_case_insensitive(self):
        bf = _load_backfill()
        self.assertTrue(bf._is_stopword("The"))
        self.assertTrue(bf._is_stopword("USE"))


class TestIsValidEntity(unittest.TestCase):
    def test_rejects_too_short(self):
        bf = _load_backfill()
        self.assertFalse(bf._is_valid_entity("a", min_len=3))
        self.assertFalse(bf._is_valid_entity("ab", min_len=3))
        self.assertFalse(bf._is_valid_entity("", min_len=3))

    def test_rejects_punctuation_only(self):
        bf = _load_backfill()
        self.assertFalse(bf._is_valid_entity("---", min_len=3))
        self.assertFalse(bf._is_valid_entity("===", min_len=3))
        self.assertFalse(bf._is_valid_entity("...", min_len=3))
        self.assertFalse(bf._is_valid_entity(":::", min_len=3))

    def test_rejects_pure_numeric(self):
        bf = _load_backfill()
        self.assertFalse(bf._is_valid_entity("123", min_len=3))
        self.assertFalse(bf._is_valid_entity("2026", min_len=3))

    def test_rejects_non_alnum_edges(self):
        bf = _load_backfill()
        self.assertFalse(bf._is_valid_entity("(test)", min_len=3))
        self.assertFalse(bf._is_valid_entity("'foo'", min_len=3))
        self.assertFalse(bf._is_valid_entity("test,", min_len=3))

    def test_rejects_stopwords(self):
        bf = _load_backfill()
        self.assertFalse(bf._is_valid_entity("the", min_len=3))
        self.assertFalse(bf._is_valid_entity("use", min_len=3))
        self.assertFalse(bf._is_valid_entity("section", min_len=3))

    def test_accepts_meaningful_words(self):
        bf = _load_backfill()
        for w in (
            "python",
            "database",
            "config",
            "schema",
            "memory",
            "vector",
            "embedding",
            "fastapi",
            "react",
            "opencode",
        ):
            self.assertTrue(bf._is_valid_entity(w, min_len=3), f"'{w}' should be valid")

    def test_custom_min_len(self):
        bf = _load_backfill()
        # "ab" has len 2, fails with min_len=4
        self.assertFalse(bf._is_valid_entity("ab", min_len=4))
        # "abc" has len 3, still fails with min_len=4
        self.assertFalse(bf._is_valid_entity("abc", min_len=4))
        # "abcd" has len 4, passes with min_len=4
        self.assertTrue(bf._is_valid_entity("abcd", min_len=4))
        # "python" has len 6, passes with min_len=4
        self.assertTrue(bf._is_valid_entity("python", min_len=4))

    def test_handles_whitespace(self):
        bf = _load_backfill()
        self.assertTrue(bf._is_valid_entity("  python  ", min_len=3))
        self.assertFalse(bf._is_valid_entity("   ", min_len=3))


class TestBackfillKgGraphFiltering(unittest.TestCase):
    """Integration: _backfill_kg_graph actually filters with env vars."""

    def setUp(self):
        import sqlite3

        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                deleted_at TEXT
            );
            CREATE TABLE kg_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                predicate TEXT,
                object TEXT,
                confidence REAL,
                source_memory TEXT,
                mention_count INTEGER DEFAULT 1
            );
            CREATE TABLE kg_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                entity_type TEXT,
                mentions INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(name, entity_type)
            );
            CREATE TABLE kg_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES kg_entities(id),
                target_id INTEGER NOT NULL REFERENCES kg_entities(id),
                relation TEXT NOT NULL DEFAULT 'related_to',
                weight REAL DEFAULT 1.0,
                created_at TEXT,
                valid_at TEXT,
                invalid_at TEXT,
                UNIQUE(source_id, target_id, relation)
            );
            """
        )

    def tearDown(self):
        import shutil

        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Clear env vars
        for k in (
            "MEMORY_KG_MIN_ENTITY_LENGTH",
            "MEMORY_KG_MIN_ENTITY_MENTIONS",
        ):
            os.environ.pop(k, None)

    def test_filters_stopwords_and_short_tokens(self):
        """Insert facts with noise + signal entities, verify noise is dropped."""
        # Insert memories to satisfy any FK constraints
        self.conn.execute(
            "INSERT INTO memories (id, content) VALUES (?, ?)",
            ("m1", "test content"),
        )

        # Insert facts: each row is a subject-predicate-object triple
        # At min_mentions=1 (default), singletons are kept if they're meaningful
        facts = [
            # Noise entities (should be filtered even at min_mentions=1)
            ("the", "is_a", "test"),  # 'the' is stopword
            ("a", "is_a", "thing"),  # 'a' is stopword
            ("to", "is_a", "code"),  # 'to' is stopword
            ("ab", "is_a", "foo"),  # too short
            ("---", "is_a", "bar"),  # punctuation
            # Signal entities — meaningful
            ("python", "is_a", "language"),
            ("python", "uses", "interpreter"),
            ("python", "creates", "bytecode"),
            ("database", "stores", "data"),
            ("database", "is_a", "system"),
            ("database", "uses", "sql"),
        ]
        for s, p, o in facts:
            self.conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, confidence, source_memory) "
                "VALUES (?, ?, ?, ?, ?)",
                (s, p, o, 0.9, "m1"),
            )
        self.conn.commit()

        bf = _load_backfill()
        bf._backfill_kg_graph(self.conn)

        # Read back entities
        rows = self.conn.execute("SELECT name, mentions FROM kg_entities").fetchall()
        names = {r[0] for r in rows}

        # Noise should be dropped (regardless of min_mentions)
        self.assertNotIn("the", names)
        self.assertNotIn("a", names)
        self.assertNotIn("to", names)
        self.assertNotIn("ab", names)
        self.assertNotIn("---", names)
        # Signal entities (long, not stopwords) are kept at min_mentions=1
        self.assertIn("python", names)
        self.assertIn("database", names)
        self.assertIn("language", names)
        self.assertIn("interpreter", names)
        self.assertIn("bytecode", names)
        self.assertIn("data", names)
        self.assertIn("system", names)
        self.assertIn("sql", names)
        # Note: 'foo', 'bar', 'thing' are 3-char non-stopword singletons —
        # they pass _is_valid_entity so they are KEPT at min_mentions=1.
        # To filter them out, set MEMORY_KG_MIN_ENTITY_MENTIONS=2 (test below).

    def test_mention_counts_are_real_not_hardcoded(self):
        """The mentions field must reflect actual fact counts, not 1."""
        # 'alpha' appears 4 times, 'beta' appears 2 times, 'gamma' once
        facts = [
            ("alpha", "is_a", "x"),
            ("alpha", "uses", "y"),
            ("alpha", "calls", "z"),
            ("alpha", "stores", "w"),
            ("beta", "is_a", "q"),
            ("beta", "uses", "r"),
            ("gamma", "is_a", "s"),  # singleton — kept at default min_mentions=1
        ]
        for s, p, o in facts:
            self.conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, confidence, source_memory) "
                "VALUES (?, ?, ?, ?, ?)",
                (s, p, o, 0.9, "m1"),
            )
        self.conn.execute(
            "INSERT INTO memories (id, content) VALUES (?, ?)", ("m1", "x")
        )
        self.conn.commit()

        bf = _load_backfill()
        bf._backfill_kg_graph(self.conn)

        rows = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT name, mentions FROM kg_entities"
            ).fetchall()
        }
        # alpha has 4 subject appearances + 0 object = 4 mentions
        self.assertEqual(rows.get("alpha"), 4)
        # beta has 2 subject appearances
        self.assertEqual(rows.get("beta"), 2)
        # gamma has 1 mention — kept at min_mentions=1 (default)
        self.assertEqual(rows.get("gamma"), 1)

    def test_mention_counts_at_min_mentions_2(self):
        """At min_mentions=2, gamma (1 mention) is filtered out."""
        os.environ["MEMORY_KG_MIN_ENTITY_MENTIONS"] = "2"
        facts = [
            ("alpha", "is_a", "x"),
            ("alpha", "uses", "y"),
            ("alpha", "calls", "z"),
            ("alpha", "stores", "w"),
            ("beta", "is_a", "q"),
            ("beta", "uses", "r"),
            ("gamma", "is_a", "s"),  # singleton — filtered at min_mentions=2
        ]
        for s, p, o in facts:
            self.conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, confidence, source_memory) "
                "VALUES (?, ?, ?, ?, ?)",
                (s, p, o, 0.9, "m1"),
            )
        self.conn.execute(
            "INSERT INTO memories (id, content) VALUES (?, ?)", ("m1", "x")
        )
        self.conn.commit()

        bf = _load_backfill()
        bf._backfill_kg_graph(self.conn)

        rows = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT name, mentions FROM kg_entities"
            ).fetchall()
        }
        self.assertEqual(rows.get("alpha"), 4)
        self.assertEqual(rows.get("beta"), 2)
        self.assertNotIn("gamma", rows)

    def test_min_mentions_env_var_lowers_threshold(self):
        """Setting min_mentions=1 keeps singletons."""
        os.environ["MEMORY_KG_MIN_ENTITY_MENTIONS"] = "1"
        # Insert a memory first (any FK constraint)
        self.conn.execute(
            "INSERT INTO memories (id, content) VALUES (?, ?)", ("m1", "x")
        )
        # Insert one fact (5 columns: subject, predicate, object, confidence, source_memory)
        self.conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, source_memory) "
            "VALUES (?, ?, ?, ?, ?)",
            ("lonely", "is_a", "thing", 0.9, "m1"),
        )
        self.conn.commit()

        bf = _load_backfill()
        bf._backfill_kg_graph(self.conn)

        rows = {
            r[0] for r in self.conn.execute("SELECT name FROM kg_entities").fetchall()
        }
        self.assertIn("lonely", rows)
        self.assertIn("thing", rows)

    def test_min_length_env_var_raises_threshold(self):
        """Setting min_length=5 drops 3-4 char entities."""
        os.environ["MEMORY_KG_MIN_ENTITY_LENGTH"] = "5"
        os.environ["MEMORY_KG_MIN_ENTITY_MENTIONS"] = "2"
        facts = [
            ("foo", "is_a", "bar"),  # both 3 chars — would be dropped
            ("foo", "uses", "baz"),  # 3 chars
            ("python", "is_a", "language"),  # 6+8 — kept
            ("python", "uses", "interpreter"),  # 6+11
            ("python", "creates", "bytecode"),  # 6+8
            ("language", "has_property", "syntax"),  # make language ≥2
        ]
        for s, p, o in facts:
            self.conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, confidence, source_memory) "
                "VALUES (?, ?, ?, ?, ?)",
                (s, p, o, 0.9, "m1"),
            )
        self.conn.execute(
            "INSERT INTO memories (id, content) VALUES (?, ?)", ("m1", "x")
        )
        self.conn.commit()

        bf = _load_backfill()
        bf._backfill_kg_graph(self.conn)

        rows = {
            r[0] for r in self.conn.execute("SELECT name FROM kg_entities").fetchall()
        }
        self.assertNotIn("foo", rows)
        self.assertNotIn("bar", rows)
        self.assertIn("python", rows)
        self.assertIn("language", rows)
        # 'interpreter' has 1 mention — would be kept at min_mentions=1
        # but at min_mentions=2 (default) it is filtered
        self.assertNotIn("interpreter", rows)


if __name__ == "__main__":
    unittest.main()
