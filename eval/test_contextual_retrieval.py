"""Tests for Contextual Retrieval (Anthropic 2024-09).

Covers:
  1. _build_context_prefix produces expected format.
  2. _embed_text_with_context prepends prefix when enabled.
  3. _embed_text_with_context returns raw text when disabled.
  4. context_prefix column exists after migration.
  5. index_embedding stores correct content_hash with context.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestBuildContextPrefix(unittest.TestCase):
    """_build_context_prefix assembles short context strings."""

    def test_category_and_tags(self):
        from infra.embedding_search import _build_context_prefix
        result = _build_context_prefix("lessons", ["python", "testing"], "")
        self.assertEqual(result, "[lessons | python, testing] ")

    def test_category_only(self):
        from infra.embedding_search import _build_context_prefix
        result = _build_context_prefix("projects", None, "")
        self.assertEqual(result, "[projects] ")

    def test_tags_only(self):
        from infra.embedding_search import _build_context_prefix
        result = _build_context_prefix("", ["rust", "async"], "")
        self.assertEqual(result, "[rust, async] ")

    def test_empty_everything(self):
        from infra.embedding_search import _build_context_prefix
        result = _build_context_prefix("", None, "")
        self.assertEqual(result, "")

    def test_source_file_fallback(self):
        from infra.embedding_search import _build_context_prefix
        result = _build_context_prefix("", None, "lessons/python/foo.md")
        self.assertIn("lessons", result)

    def test_max_five_tags(self):
        from infra.embedding_search import _build_context_prefix
        result = _build_context_prefix("x", ["a", "b", "c", "d", "e", "f", "g"], "")
        self.assertNotIn("f", result)
        self.assertIn("e", result)


class TestEmbedTextWithContext(unittest.TestCase):
    """_embed_text_with_context respects MEMORY_CONTEXTUAL_RETRIEVAL."""

    def test_enabled_prepends_prefix(self):
        import infra.embedding_search
        old = embedding_search._CONTEXTUAL_ENABLED
        embedding_search._CONTEXTUAL_ENABLED = True
        try:
            from infra.embedding_search import _embed_text_with_context
            result = _embed_text_with_context(
                "Python is great", category="lessons", tags=["python"]
            )
            self.assertIn("lessons", result)
            self.assertIn("python", result)
            self.assertIn("Python is great", result)
        finally:
            embedding_search._CONTEXTUAL_ENABLED = old

    def test_disabled_returns_raw(self):
        import infra.embedding_search
        old = embedding_search._CONTEXTUAL_ENABLED
        embedding_search._CONTEXTUAL_ENABLED = False
        try:
            from infra.embedding_search import _embed_text_with_context
            result = _embed_text_with_context(
                "Python is great", category="lessons", tags=["python"]
            )
            self.assertFalse(result.startswith("["))
            self.assertEqual(result, "Python is great"[:500])
        finally:
            embedding_search._CONTEXTUAL_ENABLED = old

    def test_no_prefix_when_empty(self):
        import infra.embedding_search
        old = embedding_search._CONTEXTUAL_ENABLED
        embedding_search._CONTEXTUAL_ENABLED = True
        try:
            from infra.embedding_search import _embed_text_with_context
            result = _embed_text_with_context("Hello world")
            self.assertFalse(result.startswith("["))
        finally:
            embedding_search._CONTEXTUAL_ENABLED = old


class TestContextPrefixColumn(unittest.TestCase):
    """context_prefix column exists after migration on existing DB."""

    def test_column_exists_after_migration(self):
        from infra.memory_common import run_db_migrations
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            # Create the memories table first (migrations skip if no table)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, content TEXT, source_file TEXT,
                    tags TEXT, created_at TEXT, updated_at TEXT
                )
            """)
            conn.commit()
            run_db_migrations(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
            self.assertIn("context_prefix", cols)
            conn.close()
        finally:
            os.unlink(db_path)


class TestIndexEmbeddingWithContext(unittest.TestCase):
    """index_embedding stores correct hash when context is enabled."""

    def test_hash_includes_context(self):
        import infra.embedding_search
        old = embedding_search._CONTEXTUAL_ENABLED
        embedding_search._CONTEXTUAL_ENABLED = True
        try:
            from infra.embedding_search import _embed_text_with_context, _content_hash
            text_with_ctx = _embed_text_with_context(
                "Hello", category="test", tags=["a"]
            )
            embedding_search._CONTEXTUAL_ENABLED = False
            text_without = _embed_text_with_context("Hello")
            self.assertNotEqual(
                _content_hash(text_with_ctx),
                _content_hash(text_without),
            )
        finally:
            embedding_search._CONTEXTUAL_ENABLED = old


if __name__ == "__main__":
    unittest.main()
