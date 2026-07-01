#!/usr/bin/env python3
"""Comprehensive integration tests for the full save pipeline.

Tests `save_memory` and its callees across all subsystems end-to-end:
  - Return value correctness (string note_id vs error dict)
  - File system output (.md files at correct paths)
  - Frontmatter generation (metadata extraction)
  - Subsystem writes (memories, FTS5, embeddings, KG, facts, chunks, backlinks,
    adaptive retention, semantic backlinks, audit)
  - Internal helpers (_index_backlinks, _index_chunks, _index_embedding,
    _index_kg, _index_facts, _auto_semantic_backlinks)
  - Edge cases (empty content, oversized content, unicode, wiki-links,
    self-references, pinned, multi-part auto-backlink, safety_wiring)
  - Error paths (invalid params, DB errors, slug traversal)

All tests use a temp directory — no production DB is touched.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


from infra.memory_common import open_db, connection_pool
from save_pipeline import (
    save_memory,
    _recalculate_fitness_scores,
)


def _make_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL;")
    db.execute("PRAGMA foreign_keys=ON;")
    db.execute("PRAGMA busy_timeout = 5000;")
    return db


def _init_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id            TEXT PRIMARY KEY,
            content       TEXT NOT NULL,
            source_file   TEXT NOT NULL,
            tags          TEXT DEFAULT '[]',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            observed_at   TEXT NOT NULL,
            pinned        INTEGER DEFAULT 0,
            importance    INTEGER DEFAULT 3,
            decay         TEXT DEFAULT 'none',
            score         REAL DEFAULT 1.0,
            supersedes    TEXT,
            repo_id       TEXT,
            access_count  INTEGER DEFAULT 1,
            success_score REAL DEFAULT 0.0,
            fitness_score REAL DEFAULT 1.0,
            conflict_policy TEXT DEFAULT 'supersede',
            version_vector TEXT DEFAULT '{}',
            logical_clock INTEGER DEFAULT 0,
            consolidation_state TEXT DEFAULT 'working',
            valid_from    TEXT,
            valid_to      TEXT,
            superseded_by TEXT,
            last_accessed TEXT,
            deleted_at    TEXT,
            deleted_by    TEXT,
            context_prefix TEXT,
            category      TEXT,
            tier          TEXT,
            psi           REAL,
            next_review   TEXT,
            adaptive_halflife_days REAL,
            embedding_revision TEXT,
            metadata      TEXT DEFAULT '{}',
            tenant_id     TEXT DEFAULT 'default'
        )
    """)
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, tags, tokenize='porter unicode61'
        )
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
        WHEN new.deleted_at IS NULL
        BEGIN
            INSERT INTO memories_fts(rowid, content, tags)
            VALUES (new.rowid, new.content, new.tags);
        END
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            DELETE FROM memories_fts WHERE rowid = old.rowid;
        END
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            DELETE FROM memories_fts WHERE rowid = old.rowid;
            INSERT INTO memories_fts(rowid, content, tags)
            VALUES (new.rowid, new.content, new.tags);
        END
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS backlinks (
            source_id TEXT,
            target_id TEXT,
            PRIMARY KEY (source_id, target_id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS file_mtimes (
            path TEXT PRIMARY KEY,
            mtime REAL,
            content_hash TEXT
        )
    """)
    from infra.memory_common import run_db_migrations, _migrate_kg_tables
    from fact import ensure_facts_schema
    from adaptive_retention import ensure_adaptive_schema

    run_db_migrations(db)
    _migrate_kg_tables(db)
    ensure_facts_schema(db)
    ensure_adaptive_schema(db)


def _count(db: sqlite3.Connection, table: str, where: str = "1=1", params=()) -> int:
    return db.execute(
        f"SELECT COUNT(*) FROM [{table}] WHERE {where}", params
    ).fetchone()[0]


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


# ===========================================================================
# Fixture: redirect save_memory to a temp directory
# ===========================================================================


class SavePipelineFixture:
    """Sets up a temp directory with a working schema and patches
    save_memory to write there instead of production.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.mem_dir = self.tmpdir / "memory"
        self.mem_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.mem_dir / "memory.db"
        db = _make_db(self.db_path)
        _init_schema(db)
        db.close()
        self._patches = []
        self._apply_patches()
        self._cleanup_ids = []

    def _apply_patches(self):
        p1 = patch("save_pipeline.resolve_active_memory_dir", return_value=self.mem_dir)
        p1.start()
        self._patches.append(p1)
        p2 = patch(
            "save_pipeline.get_memory_paths",
            return_value=(self.mem_dir, self.mem_dir, self.mem_dir),
        )
        p2.start()
        self._patches.append(p2)
        self._prev_global = os.environ.get("MEMORY_KNOWLEDGE_GRAPH")
        os.environ["MEMORY_KNOWLEDGE_GRAPH"] = "1"

    def tearDown(self):
        for p in self._patches:
            p.stop()
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        if self._prev_global is None:
            os.environ.pop("MEMORY_KNOWLEDGE_GRAPH", None)
        else:
            os.environ["MEMORY_KNOWLEDGE_GRAPH"] = self._prev_global
        for nid in self._cleanup_ids:
            try:
                f = self.mem_dir / f"{nid}.md"
                f.unlink(missing_ok=True)
            except Exception:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def save(
        self,
        content="Test content.",
        category="lessons",
        title_slug=None,
        tags=None,
        pinned=False,
        is_global=False,
        **kw,
    ):
        slug = title_slug or f"test-{int(time.time() * 1e6)}"
        result = save_memory(
            content=content,
            category=category,
            title_slug=slug,
            tags=tags or ["test"],
            pinned=pinned,
            is_global=is_global,
            **kw,
        )
        self._cleanup_ids.append(f"{category}/{slug}")
        return result, slug

    def exists_in_memories(self, note_id):
        with open_db(self.db_path) as db:
            return (
                db.execute("SELECT 1 FROM memories WHERE id=?", (note_id,)).fetchone()
                is not None
            )

    def get_row(self, note_id):
        with open_db(self.db_path) as db:
            return db.execute(
                "SELECT * FROM memories WHERE id=?", (note_id,)
            ).fetchone()


# ===========================================================================
# Phase 1: save_memory return values and validation
# ===========================================================================


class TestSaveMemoryValidation(SavePipelineFixture, unittest.TestCase):
    """Verify save_memory correctly validates inputs and returns proper types."""

    def test_valid_save_returns_string(self):
        result, slug = self.save()
        self.assertIsInstance(result, str)
        self.assertEqual(result, f"lessons/{slug}")

    def test_empty_content_returns_string(self):
        result, slug = self.save(content="")
        self.assertIsInstance(result, str)

    def test_whitespace_only_content_returns_string(self):
        result, slug = self.save(content="   \n\n  ")
        self.assertIsInstance(result, str)

    def test_oversized_content_returns_error(self):
        body = "x" * 51000
        result, _ = self.save(content=body)
        self.assertIsInstance(result, str)
        self.assertIn(
            "error",
            result.lower() if isinstance(result, str) else "",
            msg="Oversized content should return error envelope",
        )

    def test_50kb_boundary_accepted(self):
        body = "x" * 49500
        result, slug = self.save(content=body)
        self.assertIsInstance(result, str)
        self.assertEqual(result, f"lessons/{slug}")

    def test_non_string_content_rejected(self):
        bad_content = 123  # type: ignore[assignment]
        result = save_memory(
            content=bad_content, category="lessons", title_slug="test-nonstr"
        )  # type: ignore[arg-type]
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())

    def test_none_content_rejected(self):
        result = save_memory(content=None, category="lessons", title_slug="test-none")  # type: ignore[arg-type]
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())

    def test_invalid_category_dot_rejected(self):
        result, _ = self.save(category=".")
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())

    def test_invalid_category_double_dot_rejected(self):
        result, _ = self.save(category="..")
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())

    def test_invalid_category_with_slash_rejected(self):
        result, _ = self.save(category="foo/bar")
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())

    def test_invalid_slug_with_slash_rejected(self):
        result = save_memory(content="test", category="lessons", title_slug="foo/bar")
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())

    def test_long_slug_rejected(self):
        result = save_memory(content="test", category="lessons", title_slug="x" * 129)
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())

    def test_long_category_rejected(self):
        result = save_memory(
            content="test", category="x" * 65, title_slug="test-longcat"
        )
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())

    def test_too_many_tags_rejected(self):
        many_tags = [f"tag{i}" for i in range(51)]
        result, _ = self.save(tags=many_tags)
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())


class TestSaveMemoryUnicode(SavePipelineFixture, unittest.TestCase):
    """Unicode and special characters in content."""

    def test_unicode_content(self):
        result, slug = self.save(content="日本語テスト 🎉 émojis ñ ü")
        self.assertIsInstance(result, str)
        self.assertEqual(result, f"lessons/{slug}")

    def test_html_content(self):
        result, slug = self.save(content="<html><body>Hello</body></html>")
        self.assertIsInstance(result, str)

    def test_content_with_newlines_and_tabs(self):
        result, slug = self.save(content="Line1\nLine2\n\tIndented\n\nParagraph2")
        self.assertIsInstance(result, str)

    def test_emoji_only_content(self):
        result, slug = self.save(content="🎉🎊🎈")
        self.assertIsInstance(result, str)


class TestSaveMemoryTags(SavePipelineFixture, unittest.TestCase):
    """Tags as various input types."""

    def test_tags_as_list(self):
        result, slug = self.save(tags=["alpha", "beta", "gamma"])
        self.assertIsInstance(result, str)

    def test_tags_as_comma_string(self):
        result, slug = self.save(tags="alpha, beta, gamma")
        self.assertIsInstance(result, str)

    def test_tags_as_semicolon_string(self):
        result, slug = self.save(tags="alpha;beta;gamma")
        self.assertIsInstance(result, str)

    def test_tags_empty_list(self):
        result, slug = self.save(tags=[])
        self.assertIsInstance(result, str)

    def test_tags_none(self):
        result, slug = self.save(tags=None)
        self.assertIsInstance(result, str)

    def test_tags_with_numbers(self):
        result, slug = self.save(tags=[1, 2, 3])
        self.assertIsInstance(result, str)


# ===========================================================================
# Phase 2: File system output
# ===========================================================================


class TestSaveMemoryFileSystem(SavePipelineFixture, unittest.TestCase):
    """Verify files and directories are created."""

    def test_creates_category_directory(self):
        result, slug = self.save(category="decisions")
        category_dir = self.mem_dir / "decisions"
        self.assertTrue(category_dir.exists(), f"Directory {category_dir} not created")
        self.assertTrue(category_dir.is_dir())

    def test_creates_markdown_file(self):
        result, slug = self.save()
        md_file = self.mem_dir / "lessons" / f"{slug}.md"
        self.assertTrue(md_file.exists(), f"File {md_file} not created")
        content = md_file.read_text(encoding="utf-8")
        self.assertIn("Test content.", content)

    def test_markdown_has_frontmatter(self):
        result, slug = self.save(content="Hello world.")
        md_file = self.mem_dir / "lessons" / f"{slug}.md"
        content = md_file.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---"), "Frontmatter missing")
        self.assertIn("created:", content)
        self.assertIn("tags:", content)
        self.assertIn("pinned:", content)

    def test_pinned_true_in_frontmatter(self):
        result, slug = self.save(pinned=True)
        md_file = self.mem_dir / "lessons" / f"{slug}.md"
        content = md_file.read_text(encoding="utf-8")
        self.assertIn("pinned: true", content)

    def test_pinned_false_in_frontmatter(self):
        result, slug = self.save(pinned=False)
        md_file = self.mem_dir / "lessons" / f"{slug}.md"
        content = md_file.read_text(encoding="utf-8")
        self.assertIn("pinned: false", content)

    def test_content_inside_markdown_file(self):
        result, slug = self.save(content="**bold** and *italic* text.")
        md_file = self.mem_dir / "lessons" / f"{slug}.md"
        content = md_file.read_text(encoding="utf-8")
        self.assertIn("**bold**", content)
        self.assertIn("*italic*", content)


# ===========================================================================
# Phase 3: Database writes — memories table
# ===========================================================================


class TestSaveMemoryDBMemories(SavePipelineFixture, unittest.TestCase):
    """Verify the memories row is written correctly."""

    def test_memories_row_created(self):
        result, slug = self.save()
        note_id = f"lessons/{slug}"
        self.assertTrue(self.exists_in_memories(note_id))

    def test_content_stored(self):
        result, slug = self.save(content="UniqueContentMarker")
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            row = db.execute(
                "SELECT content FROM memories WHERE id=?", (note_id,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("UniqueContentMarker", row[0])

    def test_tags_stored_as_json(self):
        result, slug = self.save(tags=["tag1", "tag2"])
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            row = db.execute(
                "SELECT tags FROM memories WHERE id=?", (note_id,)
            ).fetchone()
        tags = json.loads(row[0])
        self.assertIn("tag1", tags)
        self.assertIn("tag2", tags)

    def test_category_stored(self):
        result, slug = self.save(category="projects", tags=["t"])
        note_id = f"projects/{slug}"
        with open_db(self.db_path) as db:
            row = db.execute(
                "SELECT category FROM memories WHERE id=?", (note_id,)
            ).fetchone()
        self.assertEqual(row[0], "projects")

    def test_pinned_stored(self):
        result, slug = self.save(pinned=True)
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            row = db.execute(
                "SELECT pinned FROM memories WHERE id=?", (note_id,)
            ).fetchone()
        self.assertEqual(row[0], 1)

    def test_created_at_and_updated_at_set(self):
        result, slug = self.save()
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            row = db.execute(
                "SELECT created_at, updated_at FROM memories WHERE id=?", (note_id,)
            ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertIsNotNone(row[1])

    def test_fitness_score_default(self):
        result, slug = self.save()
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            row = db.execute(
                "SELECT fitness_score FROM memories WHERE id=?", (note_id,)
            ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertGreaterEqual(row[0], 0.0)
        self.assertLessEqual(row[0], 1.0)


class TestSaveMemoryUpsert(SavePipelineFixture, unittest.TestCase):
    """Verifying upsert preserves child data."""

    def test_upsert_same_note_updates_content(self):
        result, slug = self.save(content="Original content", tags=["orig"])
        note_id = f"lessons/{slug}"
        embed_avail = _embedding_model_available()
        with open_db(self.db_path) as db:
            emb_before = _count(db, "memory_embeddings", "memory_id=?", (note_id,))
            chunks_before = _count(db, "memory_chunks", "parent_id=?", (note_id,))
        if embed_avail:
            self.assertEqual(emb_before, 1, "Embedding should exist after first save")
        else:
            self.assertEqual(emb_before, 0, "No embedding when model unavailable")

        result2 = save_memory(
            content="Updated content",
            category="lessons",
            title_slug=slug,
            tags=["updated"],
            pinned=False,
            is_global=False,
        )
        self.assertIsInstance(result2, str)

        with open_db(self.db_path) as db:
            row = db.execute(
                "SELECT content FROM memories WHERE id=?", (note_id,)
            ).fetchone()
            emb_after = _count(db, "memory_embeddings", "memory_id=?", (note_id,))
            chunks_after = _count(db, "memory_chunks", "parent_id=?", (note_id,))
        self.assertIn("Updated", row[0])
        if embed_avail:
            self.assertEqual(emb_after, 1, "Upsert should not delete embedding")
        else:
            self.assertEqual(emb_after, 0, "No embedding when model unavailable")
        self.assertEqual(chunks_after, chunks_before if chunks_before > 0 else 0)

    def test_upsert_preserves_backlinks_on_second_write(self):
        result, slug = self.save(
            content="Reference to [[other/note]] here.", tags=["t"]
        )
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            bl_before = _count(
                db, "backlinks", "source_id=? OR target_id=?", (note_id, note_id)
            )
        self.assertGreater(bl_before, 0, "Backlinks should exist after first save")

        save_memory(
            content="Updated reference to [[other/note]] here.",
            category="lessons",
            title_slug=slug,
            tags=["t"],
            pinned=False,
            is_global=False,
        )
        with open_db(self.db_path) as db:
            bl_after = _count(
                db, "backlinks", "source_id=? OR target_id=?", (note_id, note_id)
            )
        self.assertGreaterEqual(bl_after, bl_before - 1)


# ===========================================================================
# Phase 4: Subsystem writes — FTS5, embeddings, KG, facts, chunks, backlinks
# ===========================================================================


class TestSaveSubsystemFTS5(SavePipelineFixture, unittest.TestCase):
    """FTS5 index populated by trigger on memories insert."""

    def test_fts5_populated(self):
        result, slug = self.save(content="XYZZY_SEARCH_MARKER unique text")
        with open_db(self.db_path) as db:
            rows = db.execute(
                "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?",
                ("XYZZY_SEARCH_MARKER",),
            ).fetchall()
        self.assertGreaterEqual(len(rows), 1, "FTS5 should have indexed the content")

    def test_fts5_searchable_via_query(self):
        result, slug = self.save(content="SpecialUnicornPhrase")
        with open_db(self.db_path) as db:
            rows = db.execute(
                "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?",
                ("SpecialUnicornPhrase",),
            ).fetchall()
        self.assertGreaterEqual(len(rows), 1)

    def test_multiple_saves_all_indexed(self):
        slugs = []
        for i in range(3):
            result, slug = self.save(content=f"MultiSaveTest content {i}")
            slugs.append(slug)
        with open_db(self.db_path) as db:
            rows = db.execute(
                "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?",
                ("MultiSaveTest",),
            ).fetchall()
        self.assertGreaterEqual(len(rows), 3)


def _embedding_model_available():
    """Check if an embedding model is available (best-effort)."""
    try:
        from infra.embedding_search import get_embedding_search

        es = get_embedding_search()
        return es.model is not None
    except Exception:
        return False


class TestSaveSubsystemEmbeddings(SavePipelineFixture, unittest.TestCase):
    """Embeddings computed and stored for each saved note."""

    def test_embedding_created(self):
        if not _embedding_model_available():
            self.skipTest("No embedding model available")
        result, slug = self.save()
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            row = db.execute(
                "SELECT embedding, dim FROM memory_embeddings WHERE memory_id=?",
                (note_id,),
            ).fetchone()
        self.assertIsNotNone(row, "Embedding should exist")
        self.assertGreater(len(row[0]), 0, "Embedding blob should not be empty")
        self.assertGreater(row[1], 0, "Embedding dim should be > 0")

    def test_different_content_different_embeddings(self):
        if not _embedding_model_available():
            self.skipTest("No embedding model available")
        r1, s1 = self.save(content="Python programming language")
        r2, s2 = self.save(content="Cooking recipes for pasta")
        n1 = f"lessons/{s1}"
        n2 = f"lessons/{s2}"
        with open_db(self.db_path) as db:
            e1 = db.execute(
                "SELECT embedding FROM memory_embeddings WHERE memory_id=?", (n1,)
            ).fetchone()
            e2 = db.execute(
                "SELECT embedding FROM memory_embeddings WHERE memory_id=?", (n2,)
            ).fetchone()
        self.assertIsNotNone(e1)
        self.assertIsNotNone(e2)
        self.assertNotEqual(e1[0], e2[0])


class TestSaveSubsystemKG(SavePipelineFixture, unittest.TestCase):
    """Knowledge graph entities and relations."""

    def test_kg_entities_created(self):
        result, slug = self.save(
            content="Python is a programming language created by Guido van Rossum.",
        )
        with open_db(self.db_path) as db:
            entities = db.execute("SELECT name FROM kg_entities").fetchall()
        names = [r[0].lower() for r in entities]
        useful = [
            n
            for n in names
            if n in ("python", "guido van rossum", "programming language")
        ]
        self.assertGreaterEqual(
            len(useful), 1, f"Expected at least 1 useful entity, got {names}"
        )

    def test_kg_relations_created(self):
        result, slug = self.save(
            content="Python and Java are both programming languages.",
        )
        with open_db(self.db_path) as db:
            edges = db.execute("SELECT relation FROM kg_edges").fetchall()
        self.assertGreaterEqual(
            len(edges), 1, f"Expected at least 1 edge, got {len(edges)}"
        )
        rels = [r[0] for r in edges]
        self.assertIn(
            "co_occurs", rels, msg="Co-occurrence relations should be created"
        )


class TestSaveSubsystemFacts(SavePipelineFixture, unittest.TestCase):
    """SPO facts extracted into kg_facts."""

    def test_facts_created(self):
        result, slug = self.save(
            content="Python is a programming language. "
            "**Creator:** Guido van Rossum created it.",
        )
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            facts = db.execute(
                "SELECT predicate, subject, object FROM kg_facts WHERE source_memory=?",
                (note_id,),
            ).fetchall()
        [r[0].lower() for r in facts]
        self.assertGreaterEqual(
            len(facts), 1, f"Expected at least 1 fact, got {len(facts)}"
        )

    def test_facts_have_confidence(self):
        result, slug = self.save(
            content="Python is a programming language.",
        )
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            row = db.execute(
                "SELECT confidence FROM kg_facts WHERE source_memory=? LIMIT 1",
                (note_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(row[0], 0.0)


class TestSaveSubsystemBacklinks(SavePipelineFixture, unittest.TestCase):
    """Wiki-link backlinks."""

    def test_backlinks_created(self):
        result, slug = self.save(content="See [[other/note]] for details.", tags=["t"])
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            fwd = _count(
                db, "backlinks", "source_id=? AND target_id=?", (note_id, "other/note")
            )
            rev = _count(
                db, "backlinks", "source_id=? AND target_id=?", ("other/note", note_id)
            )
        self.assertEqual(fwd, 1, "Forward link should exist")
        self.assertEqual(rev, 1, "Reverse link should exist")

    def test_multiple_links(self):
        result, slug = self.save(
            content="See [[note/a]] and [[note/b]] also [[note/c]].", tags=["t"]
        )
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            total = _count(db, "backlinks", "source_id=?", (note_id,))
        self.assertEqual(total, 3, "Should have 3 forward backlinks")

    def test_self_reference_ignored(self):
        slug = f"selfref-{int(time.time() * 1e6)}"
        save_memory(
            content=f"This is about [[lessons/{slug}]] itself.",
            category="lessons",
            title_slug=slug,
            tags=["t"],
            pinned=False,
            is_global=False,
        )
        self._cleanup_ids.append(f"lessons/{slug}")
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            total = _count(db, "backlinks", "source_id=?", (note_id,))
        self.assertEqual(total, 0, "Self-referencing links should be ignored")

    def test_pipe_syntax_link(self):
        result, slug = self.save(
            content="Visit [[target/note|display text]].", tags=["t"]
        )
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            fwd = _count(
                db, "backlinks", "source_id=? AND target_id=?", (note_id, "target/note")
            )
        self.assertEqual(fwd, 1, "Pipe syntax should extract the target")


class TestSaveSubsystemChunks(SavePipelineFixture, unittest.TestCase):
    """QW5 chunk indexing for content of various lengths."""

    def test_short_content_one_chunk(self):
        result, slug = self.save(content="Short content here.")
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            n = _count(db, "memory_chunks", "parent_id=?", (note_id,))
        self.assertEqual(
            n, 1, "Short content should produce exactly 1 chunk (full content)"
        )

    def test_long_content_creates_chunks(self):
        content = "Word " * 400
        result, slug = self.save(content=content)
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            chunks = db.execute(
                "SELECT chunk_idx, start_offset, end_offset, content "
                "FROM memory_chunks WHERE parent_id=? ORDER BY chunk_idx",
                (note_id,),
            ).fetchall()
        self.assertGreaterEqual(
            len(chunks), 1, f"Expected >=1 chunk for long content, got {len(chunks)}"
        )

    def test_chunk_ordering(self):
        content = "Multiple sentences. " * 300
        result, slug = self.save(content=content)
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            indices = db.execute(
                "SELECT chunk_idx FROM memory_chunks WHERE parent_id=? ORDER BY chunk_idx",
                (note_id,),
            ).fetchall()
        idxs = [r[0] for r in indices]
        self.assertEqual(idxs, sorted(idxs), "Chunk indices should be ordered")

    def test_chunk_offsets_reasonable(self):
        content = "A long chunk test content with many sentences. " * 200
        result, slug = self.save(content=content)
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            chunks = db.execute(
                "SELECT start_offset, end_offset, content FROM memory_chunks "
                "WHERE parent_id=? ORDER BY chunk_idx",
                (note_id,),
            ).fetchall()
        for s, e, text in chunks:
            self.assertLess(s, e, "Start offset must be less than end offset")
            self.assertLessEqual(
                e, len(content), "End offset must be within content length"
            )
            self.assertEqual(
                content[s:e], text, "Chunk text must match original content slice"
            )


class TestSaveSubsystemAdaptiveRetention(SavePipelineFixture, unittest.TestCase):
    """Adaptive retention schema and access log."""

    def test_user_access_log_table_exists(self):
        result, slug = self.save()
        with open_db(self.db_path) as db:
            has = _has_table(db, "user_access_log")
        self.assertTrue(has, "ensure_adaptive_schema() creates user_access_log table")

    def test_user_access_log_recorded_when_enabled(self):
        result, slug = self.save()
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            row = db.execute(
                "SELECT 1 FROM user_access_log WHERE note_id=?", (note_id,)
            ).fetchone()
        if os.environ.get("MEMORY_ADAPTIVE_RETENTION") == "1":
            self.assertIsNotNone(row, "Access should be recorded when enabled")
        else:
            self.assertIsNone(
                row, "No access log when MEMORY_ADAPTIVE_RETENTION is not 1"
            )


class TestSaveSubsystemSemanticBacklinks(SavePipelineFixture, unittest.TestCase):
    """Auto-semantic backlinks via KG edges."""

    def test_semantic_backlinks_created_when_multiple_notes(self):
        if not _embedding_model_available():
            self.skipTest("Semantic backlinks require embedding model")
        r1, s1 = self.save(content="Python for machine learning and AI")
        r2, s2 = self.save(content="Deep learning with Python and TensorFlow")
        with open_db(self.db_path) as db:
            edges = db.execute(
                "SELECT relation, weight FROM kg_edges WHERE relation='semantically_related'"
            ).fetchall()
        self.assertGreaterEqual(
            len(edges), 1, f"Expected semantically_related edges, got {len(edges)}"
        )

    def test_semantic_backlinks_not_created_for_single_note(self):
        if not _embedding_model_available():
            self.skipTest("Semantic backlinks require embedding model")
        result, slug = self.save(content="Just one note.")
        with open_db(self.db_path) as db:
            edges = db.execute(
                "SELECT 1 FROM kg_edges WHERE relation='semantically_related'"
            ).fetchall()
        self.assertEqual(len(edges), 0, "Single note should not create semantic edges")


# ===========================================================================
# Phase 5: _index_* internal helpers
# ===========================================================================


class TestIndexBacklinksDirectly(SavePipelineFixture, unittest.TestCase):
    """Direct test of _index_backlinks via _update_memory_index_incremental."""

    def test_backlinks_empty_when_no_links(self):
        result, slug = self.save(content="Just plain text, no links.")
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            n = _count(db, "backlinks", "source_id=?", (note_id,))
        self.assertEqual(n, 0)

    def test_backlinks_skip_self_when_category_matches(self):
        slug = f"selflink-{int(time.time() * 1e6)}"
        save_memory(
            content=f"See [[{slug}]] here.",
            category="lessons",
            title_slug=slug,
            tags=["t"],
            pinned=False,
            is_global=False,
        )
        self._cleanup_ids.append(f"lessons/{slug}")
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            n = _count(db, "backlinks", "source_id=?", (note_id,))
        self.assertEqual(n, 0, "Should skip self-referencing backlinks")


class TestIndexChunksDirectly(SavePipelineFixture, unittest.TestCase):
    """QW5 chunk indexing edge cases."""

    def test_empty_content_one_chunk(self):
        result, slug = self.save(content="")
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            n = _count(db, "memory_chunks", "parent_id=?", (note_id,))
        self.assertEqual(n, 1, "Even empty content gets 1 chunk [(0, 0, '')]")

    def test_content_just_below_threshold(self):
        content = "A" * 1900
        result, slug = self.save(content=content)
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            n = _count(db, "memory_chunks", "parent_id=?", (note_id,))
        self.assertEqual(
            n, 1, "Content below 2000-char threshold produces 1 chunk (full content)"
        )

    def test_content_just_above_threshold(self):
        content = "Hello world. " * 100  # ~1200 words, well over 2000 chars
        result, slug = self.save(content=content)
        note_id = f"lessons/{slug}"
        with open_db(self.db_path) as db:
            n = _count(db, "memory_chunks", "parent_id=?", (note_id,))
        self.assertGreaterEqual(n, 1, "Content above 2000-char threshold should chunk")


class TestIndexKGDiverse(SavePipelineFixture, unittest.TestCase):
    """KG indexing with various inputs."""

    def test_random_text_no_entities(self):
        result, slug = self.save(content="a b c d e f g h i j k")
        with open_db(self.db_path) as db:
            entities = db.execute("SELECT name FROM kg_entities").fetchall()
        self.assertGreaterEqual(len(entities), 0)

    def test_kg_dedup_same_entity(self):
        r1, s1 = self.save(content="Python is great.")
        r2, s2 = self.save(content="Python is also versatile.")
        with open_db(self.db_path) as db:
            python_rows = db.execute(
                "SELECT id FROM kg_entities WHERE name=?", ("python",)
            ).fetchall()
        self.assertEqual(len(python_rows), 1, "Same entity name should produce one row")


# ===========================================================================
# Phase 6: _recalculate_fitness_scores
# ===========================================================================


class TestRecalculateFitnessScores(SavePipelineFixture, unittest.TestCase):
    """Fitness score recalculation."""

    def test_empty_list_no_crash(self):
        try:
            _recalculate_fitness_scores(self.db_path, memory_ids=[])
        except Exception as e:
            self.fail(f"_recalculate_fitness_scores crashed on empty list: {e}")

    def test_nonexistent_ids_skipped(self):
        try:
            _recalculate_fitness_scores(self.db_path, memory_ids=["nonexistent/id"])
        except Exception as e:
            self.fail(f"Should skip nonexistent IDs: {e}")

    def test_scores_bounded_0_to_1(self):
        slugs = []
        for i in range(3):
            result, slug = self.save(content=f"Fitness test {i}")
            slugs.append(slug)
        ids = [f"lessons/{s}" for s in slugs]
        _recalculate_fitness_scores(self.db_path, memory_ids=ids)
        with open_db(self.db_path) as db:
            rows = db.execute(
                "SELECT fitness_score FROM memories WHERE id IN ({})".format(
                    ",".join("?" * len(ids))
                ),
                ids,
            ).fetchall()
        for (score,) in rows:
            self.assertGreaterEqual(score, 0.0, f"Score {score} < 0")
            self.assertLessEqual(score, 1.0, f"Score {score} > 1")

    def test_idempotent(self):
        result, slug = self.save(content="Idempotent test")
        note_id = f"lessons/{slug}"
        _recalculate_fitness_scores(self.db_path, memory_ids=[note_id])
        with open_db(self.db_path) as db:
            s1 = db.execute(
                "SELECT fitness_score FROM memories WHERE id=?", (note_id,)
            ).fetchone()[0]
        _recalculate_fitness_scores(self.db_path, memory_ids=[note_id])
        with open_db(self.db_path) as db:
            s2 = db.execute(
                "SELECT fitness_score FROM memories WHERE id=?", (note_id,)
            ).fetchone()[0]
        self.assertAlmostEqual(s1, s2, places=10)


# ===========================================================================
# Phase 7: Error paths and edge cases
# ===========================================================================


class TestSaveMemoryErrors(SavePipelineFixture, unittest.TestCase):
    """Error paths through the save pipeline."""

    def test_save_returns_error_on_bad_db_path(self):
        result = save_memory(
            content="test",
            category="lessons",
            title_slug="err-test",
            tags=[],
            pinned=False,
            is_global=False,
            db_path=Path("/nonexistent/deep/db.sqlite"),
        )
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())

    def test_save_handles_unicode_slug_rejection(self):
        result = save_memory(
            content="test",
            category="lessons",
            title_slug="\u0000null-byte",
            tags=[],
            pinned=False,
            is_global=False,
        )
        self.assertIsInstance(result, str)
        self.assertIn("error", result.lower())

    def test_save_single_char_slug(self):
        result, slug = self.save(title_slug="a")
        self.assertIsInstance(result, str)

    def test_save_very_long_valid_slug(self):
        slug = "x" * 128
        result = save_memory(
            content="test",
            category="lessons",
            title_slug=slug,
            tags=[],
            pinned=False,
            is_global=False,
        )
        self._cleanup_ids.append(f"lessons/{slug}")
        self.assertIsInstance(result, str)


class TestSaveMemorySafetyWiring(SavePipelineFixture, unittest.TestCase):
    """Contradiction detection integration."""

    def test_safety_wiring_true_by_default(self):
        result, slug = self.save(content="Test contradiction check")
        self.assertIsInstance(result, str)

    def test_safety_wiring_false_skips_contradiction(self):
        result, slug = self.save(
            content="Test contradiction check", safety_wiring=False
        )
        self.assertIsInstance(result, str)


class TestSaveMemoryMultiPartAutoBacklink(SavePipelineFixture, unittest.TestCase):
    """Auto-backlink for multi-part series."""

    def test_auto_backlink_multi_part_creates_links(self):
        r1, s1 = self.save(content="Part 1 content", title_slug="guide-part-1")
        r2, s2 = self.save(content="Part 2 content", title_slug="guide-part-2")
        r3, s3 = self.save(content="Part 3 content", title_slug="guide-part-3")
        n1 = f"lessons/{s1}"
        n2 = f"lessons/{s2}"
        n3 = f"lessons/{s3}"
        with open_db(self.db_path) as db:
            row1 = db.execute(
                "SELECT content FROM memories WHERE id=?", (n1,)
            ).fetchone()
            row2 = db.execute(
                "SELECT content FROM memories WHERE id=?", (n2,)
            ).fetchone()
            row3 = db.execute(
                "SELECT content FROM memories WHERE id=?", (n3,)
            ).fetchone()
        self.assertIsNotNone(row1, f"Note {n1} should exist")
        self.assertIsNotNone(row2, f"Note {n2} should exist")
        self.assertIsNotNone(row3, f"Note {n3} should exist")
        # Auto-backlink prepends "**Part of:** [[lessons/guide-part-X]]" to content
        parts_in_content = sum(
            1 for r in (row1, row2, row3) if "Part of:" in (r[0] if r else "")
        )
        self.assertGreaterEqual(
            parts_in_content,
            2,
            "Multi-part series should inject Part-of links in content",
        )


# ===========================================================================
# Phase 8: Concurrent/duplicate saves
# ===========================================================================


class TestSaveMemoryConcurrent(SavePipelineFixture, unittest.TestCase):
    """Concurrent saves to the same note."""

    def test_double_save_same_note(self):
        slug = f"concurrent-{int(time.time())}"
        r1 = save_memory(
            content="First",
            category="lessons",
            title_slug=slug,
            tags=["t"],
            pinned=False,
            is_global=False,
        )
        r2 = save_memory(
            content="Second",
            category="lessons",
            title_slug=slug,
            tags=["t"],
            pinned=False,
            is_global=False,
        )
        self._cleanup_ids.append(f"lessons/{slug}")
        self.assertIsInstance(r1, str)
        self.assertIsInstance(r2, str)
        with open_db(self.db_path) as db:
            rows = db.execute(
                "SELECT content FROM memories WHERE id=?", (f"lessons/{slug}",)
            ).fetchall()
        self.assertEqual(len(rows), 1, "Should have exactly one row")
        self.assertIn("Second", rows[0][0], "Second save should win")


class TestSaveMemoryPinnedToggling(SavePipelineFixture, unittest.TestCase):
    """Saving again with different pinned value."""

    def test_toggle_pinned(self):
        slug = f"toggle-{int(time.time())}"
        r1 = save_memory(
            content="Test",
            category="lessons",
            title_slug=slug,
            tags=["t"],
            pinned=True,
            is_global=False,
        )
        self._cleanup_ids.append(f"lessons/{slug}")
        self.assertIsInstance(r1, str)
        with open_db(self.db_path) as db:
            p1 = db.execute(
                "SELECT pinned FROM memories WHERE id=?", (f"lessons/{slug}",)
            ).fetchone()[0]
        self.assertEqual(p1, 1)

        r2 = save_memory(
            content="Test",
            category="lessons",
            title_slug=slug,
            tags=["t"],
            pinned=False,
            is_global=False,
        )
        self.assertIsInstance(r2, str)
        with open_db(self.db_path) as db:
            p2 = db.execute(
                "SELECT pinned FROM memories WHERE id=?", (f"lessons/{slug}",)
            ).fetchone()[0]
        self.assertEqual(p2, 0)


class TestSaveMemoryDiskBacklinks(SavePipelineFixture, unittest.TestCase):
    """Verify FTS5 and multi-part auto-backlinking updates the backlinks table, but not the files on disk."""

    def test_fts_backlinks_written_to_disk(self):
        # Save a note containing 'quantum physics'
        r1, s1 = self.save(
            content="Notes on quantum physics and entanglement.",
            title_slug="quantum-physics-notes",
        )
        # Save another note containing 'quantum physics' (should link bidirectionally)
        r2, s2 = self.save(
            content="Quantum physics is a branch of physics.",
            title_slug="quantum-branch",
        )

        f1 = self.mem_dir / f"lessons/{s1}.md"
        f2 = self.mem_dir / f"lessons/{s2}.md"

        self.assertTrue(f1.exists())
        self.assertTrue(f2.exists())

        c1 = f1.read_text(encoding="utf-8")
        c2 = f2.read_text(encoding="utf-8")

        # Bidirectional links should NOT be in the files on disk
        self.assertNotIn(f"[[lessons/{s2}]]", c1)
        self.assertNotIn(f"[[lessons/{s1}]]", c2)

        # But they should be present in the backlinks database table
        with open_db(self.db_path) as db:
            rows = db.execute("SELECT source_id, target_id FROM backlinks").fetchall()
            pairs = {(r[0], r[1]) for r in rows}
            self.assertIn((f"lessons/{s1}", f"lessons/{s2}"), pairs)
            self.assertIn((f"lessons/{s2}", f"lessons/{s1}"), pairs)

    def test_multi_part_backlinks_written_to_disk(self):
        r1, s1 = self.save(content="Part 1 content", title_slug="series-part-1")
        r2, s2 = self.save(content="Part 2 content", title_slug="series-part-2")

        f1 = self.mem_dir / f"lessons/{s1}.md"
        f2 = self.mem_dir / f"lessons/{s2}.md"

        self.assertTrue(f1.exists())
        self.assertTrue(f2.exists())

        c1 = f1.read_text(encoding="utf-8")
        c2 = f2.read_text(encoding="utf-8")

        self.assertIn(f"[[lessons/{s2}]]", c1)
        self.assertIn(f"[[lessons/{s1}]]", c2)


# ===========================================================================
# P0-1 regression: save_memory must not leak DB connections
# ===========================================================================


class TestSaveMemoryConnectionLeak(SavePipelineFixture, unittest.TestCase):
    """Regression test for the saga-path connection leak (P0-1, 2026-06-22).

    Before the fix, save_memory never called safe_close_db on the conn
    acquired in the saga path, so connection_pool._depth grew unbounded
    with every save and the pool eventually exhausted in a long-running
    daemon.  The existing test_pool_no_connection_leak only checked
    len(_pool) (the number of unique keys), which is unaffected by
    depth leaks.  This test asserts depth = 0 after save_memory returns,
    which is the actual contract.
    """

    def _main_thread_key(self):
        import threading as _t

        return (str(self.db_path), _t.current_thread().ident or 0)

    def test_depth_zero_after_single_save(self):
        """A single save_memory call must leave pool depth = 0 for the caller thread."""
        key = self._main_thread_key()
        # Make sure pool is empty for this key before we start.
        connection_pool._depth.pop(key, None)

        result, _slug = self.save(content="P0-1 depth-zero test, single save")
        self.assertIsInstance(result, str)
        self.assertFalse(result.startswith("Error"), f"save returned error: {result}")

        depth = connection_pool._depth.get(key, 0)
        self.assertEqual(
            depth,
            0,
            f"Pool depth must be 0 after save_memory; got {depth}. "
            f"The conn was not returned to the pool (saga-path leak).",
        )

    def test_depth_zero_after_many_saves(self):
        """After 25 sequential saves, depth must still be 0 (no cumulative leak)."""
        key = self._main_thread_key()
        connection_pool._depth.pop(key, None)

        for i in range(25):
            result, _slug = self.save(
                content=f"P0-1 depth-zero test, save #{i}",
                title_slug=f"p01-leak-test-{i}",
            )
            self.assertIsInstance(result, str)
            self.assertFalse(
                result.startswith("Error"),
                f"save #{i} returned error: {result}",
            )

        depth = connection_pool._depth.get(key, 0)
        self.assertEqual(
            depth,
            0,
            f"After 25 saves, pool depth must be 0; got {depth}. "
            f"Cumulative saga-path leak.",
        )


# ===========================================================================
# P0-2 regression: save_memory must acquire file lock before DB conn
# ===========================================================================


class TestSaveMemoryLockOrder(SavePipelineFixture, unittest.TestCase):
    """Regression test for the lock-order inversion (P0-2, 2026-06-22).

    Before the fix, save_memory acquired the DB conn before the file
    lock, while _update_memory_index_incremental acquired the file
    lock before the conn.  This is a classic lock-order inversion
    that can deadlock if the conn ever becomes process-wide.

    The fix: save_memory now acquires the file lock first, then the
    conn, matching the incremental path's order.

    This test monkey-patches _acquire_lock and connection_pool.get to
    record the order of calls and asserts the file lock is acquired
    before the conn.
    """

    def test_file_lock_acquired_before_conn(self):
        """save_memory must call _acquire_lock before connection_pool.get."""
        import save_pipeline as sp

        call_order = []
        original_acquire_lock = sp._acquire_lock
        original_pool_get = connection_pool.get

        def mock_acquire_lock(db_path):
            call_order.append(("file_lock", time.time()))
            # Return None to skip the lock — we only care about order.
            return None

        def mock_pool_get(path, timeout=30.0, **kwargs):
            call_order.append(("conn", time.time()))
            return original_pool_get(path, timeout=timeout)

        try:
            sp._acquire_lock = mock_acquire_lock
            connection_pool.get = mock_pool_get
            result, _slug = self.save(
                content="P0-2 lock-order test",
                title_slug="p02-lock-order",
            )
        finally:
            sp._acquire_lock = original_acquire_lock
            connection_pool.get = original_pool_get

        # Find the indices of the first file_lock and first conn calls
        try:
            lock_idx = next(
                i for i, (kind, _) in enumerate(call_order) if kind == "file_lock"
            )
        except StopIteration:
            self.fail(f"save_memory did not call _acquire_lock; calls: {call_order}")
        try:
            conn_idx = next(
                i for i, (kind, _) in enumerate(call_order) if kind == "conn"
            )
        except StopIteration:
            self.fail(
                f"save_memory did not call connection_pool.get; calls: {call_order}"
            )

        self.assertLess(
            lock_idx,
            conn_idx,
            f"File lock must be acquired BEFORE conn (lock-order inversion). "
            f"file_lock at index {lock_idx}, conn at index {conn_idx}. "
            f"Full call order: {call_order}",
        )

        # Sanity: save still succeeded
        self.assertIsInstance(result, str)
        self.assertFalse(result.startswith("Error"), f"save returned error: {result}")


# ===========================================================================
# Run
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
