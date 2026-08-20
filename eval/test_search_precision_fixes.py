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

        # Mock before importing to prevent ML model loading under suite load
        with mock.patch("infra._lazy_imports.get_embedding_search", return_value=mock_es), \
             mock.patch("infra._lazy_imports.get_config", return_value=mock_cfg), \
             mock.patch("search.phases.fusion._search_chunks_enhanced", side_effect=fake_search_chunks), \
             mock.patch("search.phases.fusion._merge_chunk_hits", side_effect=fake_merge), \
             mock.patch("search.phases.fusion._fetch_rows_by_ids", side_effect=fake_fetch_by_ids):
            from search.phases.fusion import _hybrid_fusion

            fts_results = [("doc/fts-hit", None, None, None, None, 0.0, None, None, None, None)]
            merged = _hybrid_fusion(
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
        conn.execute("PRAGMA journal_mode=WAL")
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
            extracted_saves = []
            def mock_save_memory_auto(content, category, title_slug, tags, pinned, importance, safety_wiring):
                extracted_saves.append({
                    "content": content,
                    "category": category,
                    "title_slug": title_slug,
                    "importance": importance,
                })
                return f"lessons/{title_slug}"

            with mock.patch("save.pipeline.save_memory_auto", side_effect=mock_save_memory_auto):
                marker = {"first_tool_at": 1783936800}
                res = extract_session_findings(marker)
                self.assertEqual(res.get("extracted"), 1)
                
                self.assertEqual(len(extracted_saves), 1)
                self.assertIn("Fixed: resolved the double sigmoid bug", extracted_saves[0]["content"])
                self.assertEqual(extracted_saves[0]["category"], "lessons")
                self.assertEqual(extracted_saves[0]["importance"], 3)

    def test_extraction_skips_duplicate_findings(self):
        """Already-extracted findings are not duplicated."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
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
                conn_test = sqlite3.connect(str(db_path), check_same_thread=False)
                conn_test.execute("PRAGMA journal_mode=WAL")
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

    # ─── Query cleaning, punctuation stripping, and entailment precision ───

    def test_token_boundary_punctuation_stripped(self):
        """Tokens with trailing periods or commas must be stripped of boundary punctuation."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        norm, fts, bare, kg = _parse_search_query("On the item list. Select a range, from dropdown:", db_path)
        self.assertIn('"list"', fts)
        self.assertIn('"range"', fts)
        self.assertIn('"dropdown"', fts)
        self.assertNotIn('"list."', fts)
        self.assertNotIn('"range,"', fts)
        self.assertNotIn('"dropdown:"', fts)
        self.assertIn('"item list"', fts)

    def test_hyphenated_and_symbol_words_preserved(self):
        """Internal hyphens and symbols in technical words must be preserved."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        norm, fts, bare, kg = _parse_search_query("using local-first and @component-name", db_path)
        self.assertIn("local-first", fts)
        self.assertIn("@component-name", fts)

    def test_instruction_boilerplate_stripped(self):
        """Prompt instruction suffixes like 'Mark your final answer...' must not pollute FTS clauses."""
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        raw_query = (
            "What field is marked customer visible on the task page?\n\n"
            "Mark your final answer (should be one or more short phrases) in \\boxed{}."
        )
        norm, fts, bare, kg = _parse_search_query(raw_query, db_path)
        self.assertIn('"customer visible"', fts)
        self.assertIn('"task page"', fts)
        self.assertNotIn("boxed", fts)
        self.assertNotIn('"Mark final"', fts)
        self.assertNotIn('"final answer"', fts)
        self.assertNotIn('"short phrases"', fts)

    def test_reasoning_expand_no_trigger_on_bare_is(self):
        """Bare linking verb 'is' must NOT trigger reasoning expansion on normal English sentences."""
        from search.phases.retrieve import _reasoning_expand
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        conn = sqlite3.connect(str(db_path))
        query = "Which menu item is displayed at the top?"
        expansions = _reasoning_expand(db_path, query, conn=conn)
        self.assertEqual(expansions, [])
        conn.close()

    def test_kg_search_token_sanitization_no_fts_syntax_error(self):
        """Quoted tokens in _match_query_entities must not generate invalid FTS5 triple quotes."""
        from knowledge_graph.kg_search import _match_query_entities
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE kg_entities (id TEXT PRIMARY KEY, name TEXT, entity_type TEXT, mentions INTEGER DEFAULT 1)")
        conn.execute("CREATE VIRTUAL TABLE kg_entities_fts USING fts5(name, entity_type, content='kg_entities', content_rowid='rowid')")
        conn.execute("INSERT INTO kg_entities (rowid, id, name, entity_type) VALUES (1, 'e1', 'ServiceNow', 'system')")
        conn.execute("INSERT INTO kg_entities_fts (rowid, name, entity_type) VALUES (1, 'ServiceNow', 'system')")
        conn.commit()

        results = _match_query_entities(conn, 'on "servicenow" page', limit=5)
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0][1], "ServiceNow")
        conn.close()

if __name__ == "__main__":
    unittest.main()
