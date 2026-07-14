import os
import sys
import unittest
import tempfile
import sqlite3
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from search.query_parser import _parse_search_query
from save.session_end_extractor import extract_session_findings


class TestSearchPrecisionFixes(unittest.TestCase):
    """Tests for search precision improvements: bigram phrases, chunk RRF, session-end extraction."""

    # ─── Query parser: bigram generation ─────────────────────────────

    def test_adjacent_bigrams_generation(self):
        """Verify that adjacent bigrams are generated for bare multi-word queries."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        norm, fts, bare, kg = _parse_search_query("double sigmoid bug", db_path)
        self.assertIn('"double sigmoid"', fts)
        self.assertIn('"sigmoid bug"', fts)
        self.assertIn('"double"', fts)
        self.assertIn('"sigmoid"', fts)
        self.assertIn('"bug"', fts)

    def test_bigrams_single_word(self):
        """Single-word queries produce no bigrams, fall back to expansion or bare word."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        norm, fts, bare, kg = _parse_search_query("sigmoid", db_path)
        self.assertIn("sigmoid", fts)

    def test_bigrams_with_stopwords(self):
        """Stop words are excluded from content_words and don't generate bigrams."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        norm, fts, bare, kg = _parse_search_query("the double sigmoid and bug", db_path)
        # "the", "and" are stop words — should not appear in bigrams
        self.assertNotIn("the", fts)
        self.assertNotIn("and", fts)
        # Core words still produce bigrams
        self.assertIn('"double sigmoid"', fts)
        self.assertIn('"sigmoid bug"', fts)

    # ─── Query parser: quoted-phrase regression fix ──────────────────

    def test_quoted_phrase_preserved(self):
        """User-quoted phrases are preserved even when no expansions or bigrams exist."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        norm, fts, bare, kg = _parse_search_query('"sigmoid bug"', db_path)
        self.assertIn('"sigmoid bug"', fts)

    def test_quoted_phrase_with_bare_word(self):
        """Quoted phrase + bare word: both preserved in FTS query."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        norm, fts, bare, kg = _parse_search_query('"double sigmoid" bug', db_path)
        self.assertIn('"double sigmoid"', fts)
        self.assertIn('"bug"', fts)

    def test_quoted_phrase_with_bigrams(self):
        """Mixed quoted phrases and bare words produce quoted terms + bigrams + unigrams."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        norm, fts, bare, kg = _parse_search_query('"critical bug" double sigmoid', db_path)
        # User-quoted phrase preserved
        self.assertIn('"critical bug"', fts)
        # Bigrams from bare words
        self.assertIn('"double sigmoid"', fts)
        # Unigrams present
        self.assertIn('"double"', fts)
        self.assertIn('"sigmoid"', fts)

    # ─── Chunk FTS RRF channel ───────────────────────────────────────

    def test_chunk_fts_rrf_merges_chunk_only_hits(self):
        """_hybrid_fusion incorporates chunk-level FTS hits via RRF alongside doc FTS and semantic."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        conn = sqlite3.connect(str(db_path))

        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, source_file TEXT, "
                     "tags TEXT, category TEXT, created_at TEXT, updated_at TEXT, "
                     "observed_at TEXT, pinned INTEGER DEFAULT 0, importance INTEGER, "
                     "tenant_id TEXT, fitness REAL DEFAULT 0.5, access_count INTEGER DEFAULT 0, "
                     "metadata TEXT DEFAULT '{}', last_accessed TEXT)")
        conn.execute("INSERT OR IGNORE INTO memories VALUES "
                     "('doc/fts-hit', 'document about sigmoid activation', '', '[]', 'test', "
                     "'2026-07-13', '2026-07-13', '2026-07-13', 0, 3, 'default', 0.5, 0, '{}', NULL)")
        conn.execute("INSERT OR IGNORE INTO memories VALUES "
                     "('doc/chunk-only', 'another document', '', '[]', 'test', "
                     "'2026-07-13', '2026-07-13', '2026-07-13', 0, 3, 'default', 0.5, 0, '{}', NULL)")
        conn.commit()
        conn.close()

        def fake_search_chunks(db, fts_query, limit):
            return [("doc/chunk-only", 0, "buried sigmoid bug detail", 0, 100, 1.0)]

        def fake_merge(chunks):
            return [(p_id, chunk_idx, c, s, e) for p_id, chunk_idx, c, s, e, r in chunks]

        def fake_fetch_by_ids(db, ids, table="tenant_memories", columns=None, extra_filter="", extra_params=()):
            result = {}
            for mid in ids:
                row = db.execute("SELECT id, content, source_file, tags, created_at, fitness, "
                                 "importance, pinned, last_accessed, metadata, access_count "
                                 "FROM memories WHERE id=?", (mid,)).fetchone()
                if row:
                    result[mid] = row
            return result

        mock_es = mock.MagicMock()
        mock_es.search.return_value = []

        mock_cfg = mock.MagicMock()
        mock_cfg.hybrid_semantic_overfetch = 3
        mock_cfg.hybrid_rrf_k = 60
        mock_cfg.hybrid_rank_proxy_scale = 30.0
        mock_cfg.hybrid_fts_weight = 1.0
        mock_cfg.hybrid_semantic_weight = 1.0
        mock_cfg.hybrid_chunk_fts_weight = 0.8

        # Mock before importing orchestrator to prevent ML model loading under suite load
        with mock.patch("infra._lazy_imports.get_embedding_search", return_value=mock_es), \
             mock.patch("infra._lazy_imports.get_config", return_value=mock_cfg):
            from search import orchestrator as orch

            with mock.patch.object(orch, "_search_chunks_enhanced", side_effect=fake_search_chunks), \
                 mock.patch.object(orch, "_merge_chunk_hits", side_effect=fake_merge), \
                 mock.patch.object(orch, "_fetch_rows_by_ids", side_effect=fake_fetch_by_ids):

                fts_results = [("doc/fts-hit", None, None, None, None, 0.0, None, None, None, None)]
                merged = orch._hybrid_fusion(
                    db=sqlite3.connect(str(db_path)),
                    results=fts_results,
                    normalized_query="sigmoid bug",
                    fts_query='"sigmoid" OR "bug"',
                    db_path=db_path,
                    limit=10,
                    repo_filter="",
                )

        merged_ids = {r[0] for r in merged}
        self.assertIn("doc/fts-hit", merged_ids)
        self.assertIn("doc/chunk-only", merged_ids)

    # ─── Session-end findings extraction ─────────────────────────────

    def test_inline_findings_extraction(self):
        """Session notes with 'Fixed:' patterns are extracted as lesson notes."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                category TEXT,
                tags TEXT DEFAULT '[]',
                source_file TEXT DEFAULT '',
                pinned INTEGER DEFAULT 0,
                is_global INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                accessed_at TEXT DEFAULT (datetime('now')),
                importance INTEGER DEFAULT 2,
                deleted_at TEXT
            )
        """)
        
        now_iso = "2026-07-13T12:00:00Z"
        conn.execute(
            "INSERT INTO memories (id, content, category, importance, created_at, updated_at) "
            "VALUES ('sessions/end-test', 'We investigated the issue. Fixed: resolved the double sigmoid bug by patching query_parser.', 'sessions', 2, ?, ?)",
            (now_iso, now_iso)
        )
        conn.commit()
        conn.close()
        
        with mock.patch("infra.infrastructure.resolve_active_memory_dir", return_value=Path(tmpdir)):
            def mock_save_memory_auto(content, category, title_slug, tags, pinned, importance, safety_wiring):
                conn_test = sqlite3.connect(str(db_path))
                conn_test.execute(
                    "INSERT INTO memories (id, content, category, importance, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
                    (f"lessons/{title_slug}", content, category, importance)
                )
                conn_test.commit()
                conn_test.close()
                return f"lessons/{title_slug}"

            with mock.patch("save.pipeline.save_memory_auto", side_effect=mock_save_memory_auto):
                marker = {"first_tool_at": 1783936800}
                res = extract_session_findings(marker)
                self.assertEqual(res.get("extracted"), 1)
                
                conn = sqlite3.connect(str(db_path))
                rows = conn.execute(
                    "SELECT id, content, category, importance FROM memories WHERE category='lessons'"
                ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][3], 3)
                self.assertIn("Fixed: resolved the double sigmoid bug", rows[0][1])
                conn.close()

    def test_extraction_skips_duplicate_findings(self):
        """Already-extracted findings are not duplicated."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                category TEXT,
                tags TEXT DEFAULT '[]',
                source_file TEXT DEFAULT '',
                pinned INTEGER DEFAULT 0,
                is_global INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                accessed_at TEXT DEFAULT (datetime('now')),
                importance INTEGER DEFAULT 2,
                deleted_at TEXT
            )
        """)
        # Pre-seed an existing lesson with similar content
        now_iso = "2026-07-13T12:00:00Z"
        conn.execute(
            "INSERT INTO memories (id, content, category, importance, created_at, updated_at) "
            "VALUES ('lessons/existing-fix', '# Lesson: Fixed: resolved the double sigmoid bug by patching query_parser.', 'lessons', 3, ?, ?)",
            (now_iso, now_iso)
        )
        conn.execute(
            "INSERT INTO memories (id, content, category, importance, created_at, updated_at) "
            "VALUES ('sessions/end-test', 'We fixed: resolved the double sigmoid bug by patching query_parser.', 'sessions', 2, ?, ?)",
            (now_iso, now_iso)
        )
        conn.commit()
        conn.close()
        
        with mock.patch("infra.infrastructure.resolve_active_memory_dir", return_value=Path(tmpdir)):
            def mock_save_memory_auto(content, category, title_slug, tags, pinned, importance, safety_wiring):
                conn_test = sqlite3.connect(str(db_path))
                conn_test.execute(
                    "INSERT INTO memories (id, content, category, importance, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
                    (f"lessons/{title_slug}", content, category, importance)
                )
                conn_test.commit()
                conn_test.close()
                return f"lessons/{title_slug}"

            with mock.patch("save.pipeline.save_memory_auto", side_effect=mock_save_memory_auto):
                marker = {"first_tool_at": 1783936800}
                res = extract_session_findings(marker)
                self.assertEqual(res.get("extracted"), 0)

if __name__ == "__main__":
    unittest.main()
