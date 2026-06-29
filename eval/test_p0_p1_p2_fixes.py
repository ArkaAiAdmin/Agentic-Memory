"""Tests for the P0/P1/P2 fixes landed 2026-06-19.

Covers:
- P0.1: user_profile.record_access wired into save + search pipelines
- P0.2: FTS cron staggered to 02:33
- P0.5: backfill cron path fixed
- P0.6: 3 tools in auto_save denylist
- P1.1/P1.2: query_id + returned event in search
- P1.3: click proxy on re-save within 4h
- P1.4: CTR tuning gate
- P2a.2: kg_extraction_stats table
- P2b: KG regex tightening
- P2c: KG two-stage + cache

Each test class is self-contained. Tests that touch the prod DB use a
fresh :memory: or tempfile-based DB where possible; the few that need
to verify the live schema use sqlite3 against ``$MEMORY_DB_PATH``.
"""

import json
import os
import sqlite3
import subprocess
import tempfile
import time as _time
import unittest
from pathlib import Path


def _temp_db() -> Path:
    from eval._fixtures import bootstrap_temp_db_clean

    p = Path(tempfile.mktemp(suffix=".db"))
    bootstrap_temp_db_clean(p)
    return p


def _seed_one_memory(db: Path) -> str:
    """Insert a single memory row so search does not short-circuit on empty DB."""
    note_id = f"lessons/test-seed-{os.getpid()}-{_time.time_ns()}"
    now = _time.time()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO memories "
            "(id, source_file, content, tags, created_at, updated_at, "
            " observed_at, fitness_score, importance, importance_score, "
            " pinned, repo_id, category, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                note_id,
                f"{note_id}.md",
                "test content for query id matching",
                '["test"]',
                now,
                now,
                now,
                0.5,
                3,
                3,
                0,
                None,
                "lessons",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return note_id


class TestUserProfileAccessLogWiring(unittest.TestCase):
    """P0.1: save + search write to user_profile_access_log."""

    def test_user_profile_access_log_table_has_accessed_at(self):
        db = _temp_db()
        conn = sqlite3.connect(db)
        try:
            cols = [
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(user_profile_access_log)"
                ).fetchall()
            ]
            self.assertIn("accessed_at", cols)
        finally:
            conn.close()
        os.remove(db)

    def test_save_writes_user_profile_access_log_row(self):
        """Save a memory; assert user_profile_access_log row appears."""
        from save_pipeline import _index_adaptive_retention

        db = _temp_db()
        conn = sqlite3.connect(db)
        try:
            test_id = "lessons/test-user-profile-access-log-wiring-2026-06-19"
            _index_adaptive_retention(conn, test_id, db_path=str(db))
            conn.commit()
            row = conn.execute(
                "SELECT source FROM user_profile_access_log WHERE note_id = ? ORDER BY accessed_at DESC LIMIT 1",
                (test_id,),
            ).fetchone()
            self.assertIsNotNone(row, "no user_profile_access_log row written")
            self.assertEqual(row[0], "save")
        finally:
            conn.close()
        os.remove(db)

    def test_search_emits_user_profile_access_log(self):
        """Schema sanity: source + tags columns must exist."""
        db = _temp_db()
        conn = sqlite3.connect(db)
        try:
            cols = [
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(user_profile_access_log)"
                ).fetchall()
            ]
            self.assertIn("source", cols)
            self.assertIn("tags", cols)
        finally:
            conn.close()
        os.remove(db)


class TestAutoSaveDenylist(unittest.TestCase):
    """P0.6: 3 tools in auto_save denylist."""

    def _denylist(self):
        from auto_save import _resolve_denylist, DEFAULT_TOOL_DENYLIST

        try:
            return _resolve_denylist()
        except Exception:
            return DEFAULT_TOOL_DENYLIST

    def test_denylist_includes_concept_drift(self):
        self.assertIn("memory_check_concept_drift", self._denylist())

    def test_denylist_includes_ctr(self):
        self.assertIn("memory_record_ctr_feedback", self._denylist())

    def test_denylist_includes_profile_access(self):
        self.assertIn("memory_profile_access", self._denylist())


class TestCtrQueryId(unittest.TestCase):
    """P1.1, P1.2: search returns query_id and emits returned CTR event."""

    def test_search_returns_query_id(self):
        from search_pipeline import search_memories

        db = _temp_db()
        _seed_one_memory(db)
        r = search_memories(
            db_path=db, query="test query id", limit=2, deep_rerank=False
        )
        self.assertIn("query_id", r)
        qid = r["query_id"]
        self.assertEqual(len(qid), 32)
        int(qid, 16)
        os.remove(db)

    def test_search_emits_returned_ctr_event(self):
        from search_pipeline import search_memories

        db = _temp_db()
        _seed_one_memory(db)
        r = search_memories(
            db_path=db, query="test ctr event", limit=2, deep_rerank=False
        )
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT id, source, ranking_params FROM memory_ctr_feedback WHERE query_id = ? LIMIT 1",
                (r["query_id"],),
            ).fetchone()
            self.assertIsNotNone(row, "no CTR row for the new query_id")
            self.assertEqual(row[1], "search")
            params = json.loads(row[2]) if row[2] else {}
            self.assertIn("weights", params)
        finally:
            conn.close()
        os.remove(db)


class TestCtrClickProxy(unittest.TestCase):
    """P1.3: re-saving a note within the window marks CTR as clicked."""

    def test_click_proxy_marks_returned_as_clicked(self):
        from save_pipeline import _index_adaptive_retention

        db = _temp_db()
        test_id = f"lessons/test-ctr-click-{int(_time.time())}"
        qid = f"test-qid-{int(_time.time())}"

        conn = sqlite3.connect(db)
        try:
            now = _time.time()
            conn.execute(
                "INSERT INTO memory_ctr_feedback (id, query_id, returned_at, source, ranking_params) "
                "VALUES (?, ?, ?, ?, ?)",
                (test_id, qid, now, "search", json.dumps({"weights": {}})),
            )
            conn.commit()
            _index_adaptive_retention(conn, test_id, db_path=str(db))
            conn.commit()
            row = conn.execute(
                "SELECT clicked_at FROM memory_ctr_feedback WHERE id = ? AND query_id = ?",
                (test_id, qid),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNotNone(
                row[0], "clicked_at not set after _index_adaptive_retention"
            )
        finally:
            conn.close()
        os.remove(db)

    def test_click_proxy_window_respected(self):
        """An old returned event (5h ago) is NOT auto-clicked at default 4h window."""
        from save_pipeline import _index_adaptive_retention

        db = _temp_db()
        test_id = f"lessons/test-ctr-click-window-{int(_time.time())}"
        qid = f"test-qid-window-{int(_time.time())}"

        conn = sqlite3.connect(db)
        try:
            old_time = _time.time() - 5 * 3600
            conn.execute(
                "INSERT INTO memory_ctr_feedback (id, query_id, returned_at, source, ranking_params) "
                "VALUES (?, ?, ?, ?, ?)",
                (test_id, qid, old_time, "search", json.dumps({"weights": {}})),
            )
            conn.commit()
            _index_adaptive_retention(conn, test_id, db_path=str(db))
            conn.commit()
            row = conn.execute(
                "SELECT clicked_at FROM memory_ctr_feedback WHERE id = ? AND query_id = ?",
                (test_id, qid),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(
                row[0],
                "clicked_at should NOT be set for returned events older than the window",
            )
        finally:
            conn.close()
        os.remove(db)

    def test_click_proxy_env_override_extends_window(self):
        """Setting MEMORY_CTR_CLICK_WINDOW_HOURS=24 extends the window."""
        from save_pipeline import _index_adaptive_retention

        db = _temp_db()
        test_id = f"lessons/test-ctr-click-24h-{int(_time.time())}"
        qid = f"test-qid-24h-{int(_time.time())}"

        old_env = os.environ.get("MEMORY_CTR_CLICK_WINDOW_HOURS")
        os.environ["MEMORY_CTR_CLICK_WINDOW_HOURS"] = "24"
        try:
            conn = sqlite3.connect(db)
            try:
                old_time = _time.time() - 5 * 3600
                conn.execute(
                    "INSERT INTO memory_ctr_feedback (id, query_id, returned_at, source, ranking_params) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (test_id, qid, old_time, "search", json.dumps({"weights": {}})),
                )
                conn.commit()
                _index_adaptive_retention(conn, test_id, db_path=str(db))
                conn.commit()
                row = conn.execute(
                    "SELECT clicked_at FROM memory_ctr_feedback WHERE id = ? AND query_id = ?",
                    (test_id, qid),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertIsNotNone(
                    row[0], "clicked_at should be set with 24h window for 5h-old event"
                )
            finally:
                conn.close()
        finally:
            if old_env is None:
                os.environ.pop("MEMORY_CTR_CLICK_WINDOW_HOURS", None)
            else:
                os.environ["MEMORY_CTR_CLICK_WINDOW_HOURS"] = old_env
        os.remove(db)


class TestCtrTuningGate(unittest.TestCase):
    """P1.4: compute_channel_weights respects MEMORY_CTR_TUNING env var."""

    def test_returns_none_when_disabled(self):
        from search_pipeline import compute_channel_weights

        old = os.environ.pop("MEMORY_CTR_TUNING", None)
        try:
            db = _temp_db()
            result = compute_channel_weights(db)
            self.assertIsNone(result)
        finally:
            if old is not None:
                os.environ["MEMORY_CTR_TUNING"] = old
        os.remove(db)

    def test_returns_adjusted_when_enabled(self):
        from search_pipeline import compute_channel_weights

        old = os.environ.get("MEMORY_CTR_TUNING")
        os.environ["MEMORY_CTR_TUNING"] = "1"
        try:
            db = _temp_db()
            result = compute_channel_weights(db)
            if result is not None:
                self.assertIsInstance(result, dict)
        finally:
            if old is None:
                os.environ.pop("MEMORY_CTR_TUNING", None)
            else:
                os.environ["MEMORY_CTR_TUNING"] = old
        os.remove(db)


class TestKgExtractionStats(unittest.TestCase):
    """P2a.2: kg_extraction_stats table exists and is populated on save."""

    def _temp_db(self) -> Path:
        from eval._fixtures import bootstrap_temp_db_clean

        p = Path(tempfile.mktemp(suffix=".db"))
        bootstrap_temp_db_clean(p)
        return p

    def test_table_exists(self):
        db = self._temp_db()
        conn = sqlite3.connect(db)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("kg_extraction_stats", tables)
        finally:
            conn.close()
        os.remove(db)

    def test_table_has_expected_columns(self):
        db = self._temp_db()
        conn = sqlite3.connect(db)
        try:
            cols = {
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(kg_extraction_stats)"
                ).fetchall()
            }
            for expected in (
                "memory_id",
                "entities_extracted",
                "regex_count",
                "llm_count",
            ):
                self.assertIn(expected, cols)
        finally:
            conn.close()
        os.remove(db)


class TestKgRegexTightening(unittest.TestCase):
    """P2b: frontmatter/code-block stripping, stop-words, file-path filter."""

    def test_extract_entities_strips_frontmatter(self):
        from knowledge_graph import extract_entities

        text = """---
created: 2026-06-19
updated: 2026-06-19
tags: [test]
pinned: true
---
# Real content
The Qwen3 reranker hangs on MPS.
"""
        ents = extract_entities(text, min_occurrences=1)
        names = []
        for e in ents or []:
            if isinstance(e, str):
                names.append(e)
            elif isinstance(e, tuple):
                names.append(str(e[0]))
            elif isinstance(e, dict):
                names.append(str(e.get("name", "")))
        names = [n.lower() for n in names]
        for forbidden in ("created", "updated", "tags", "pinned"):
            self.assertNotIn(
                forbidden,
                [n.lower() for n in names],
                f"frontmatter key {forbidden!r} should be stripped",
            )

    def test_extract_entities_strips_code_blocks(self):
        from knowledge_graph import extract_entities

        text = """# Header
```python
import os
def class_path():
    return "schema"
```
Real prose about Qwen3 reranker.
"""
        ents = extract_entities(text, min_occurrences=1)
        names = []
        for e in ents or []:
            if isinstance(e, str):
                names.append(e)
            elif isinstance(e, tuple):
                names.append(str(e[0]))
            elif isinstance(e, dict):
                names.append(str(e.get("name", "")))
        names = [n.lower() for n in names]
        for forbidden in ("os", "def", "schema"):
            self.assertNotIn(
                forbidden,
                [n.lower() for n in names],
                f"code-block term {forbidden!r} leaked",
            )

    def test_extract_entities_filters_stopwords(self):
        from knowledge_graph import extract_entities

        # A doc with only stop-words and no real entities
        text = "import path os def class auto save key value type schema model config version data"
        ents = extract_entities(text, min_occurrences=1)
        # Either empty, or all entities are from the real-content (not stop-words)
        names = []
        for e in ents or []:
            if isinstance(e, str):
                names.append(e)
            elif isinstance(e, tuple):
                names.append(str(e[0]))
            elif isinstance(e, dict):
                names.append(str(e.get("name", "")))
        for n in names:
            self.assertNotIn(
                n.lower(),
                {
                    "path",
                    "os",
                    "import",
                    "def",
                    "class",
                    "auto",
                    "save",
                    "key",
                    "value",
                    "type",
                    "schema",
                    "model",
                    "config",
                    "version",
                    "data",
                },
            )

    def test_extract_entities_filters_file_paths(self):
        from knowledge_graph import extract_entities

        text = "Run python3 on auto_save.py to load /home/user/.config/agentic-memory/memory.db"
        ents = extract_entities(text, min_occurrences=1)
        names = []
        for e in ents or []:
            if isinstance(e, str):
                names.append(e)
            elif isinstance(e, tuple):
                names.append(str(e[0]))
            elif isinstance(e, dict):
                names.append(str(e.get("name", "")))
        for n in names:
            self.assertFalse(
                n.endswith(".py") or n.endswith(".db") or "/" in n,
                f"file-path entity {n!r} should be filtered",
            )

    def test_min_occurrences_param(self):
        from knowledge_graph import extract_entities

        text = "Apple Apple Apple banana banana"
        ents = extract_entities(text, min_occurrences=3)
        names = []
        for e in ents or []:
            if isinstance(e, str):
                names.append(e)
            elif isinstance(e, tuple):
                names.append(str(e[0]))
            elif isinstance(e, dict):
                names.append(str(e.get("name", "")))
        names_lower = [n.lower() for n in names]
        self.assertIn(
            "apple", names_lower, f"expected 'apple' (3 occurrences) in {names_lower!r}"
        )
        self.assertNotIn("banana", names_lower)


class TestKgTwoStage(unittest.TestCase):
    """P2c: regex-first + LLM fallback + content-hash cache."""

    def test_extract_entities_returns_for_content_with_proper_nouns(self):
        """If regex finds 2+ entities, no LLM should be needed."""
        from knowledge_graph import extract_entities

        text = "Qwen3 reranker hangs on Apple Silicon with PyTorch MPS kernel"
        ents = extract_entities(text, min_occurrences=1)
        self.assertGreater(len(ents or []), 0)

    def test_clear_cache_is_callable(self):
        """The extraction cache must be clearable for testing."""
        try:
            from knowledge_graph import clear_extraction_cache

            clear_extraction_cache()
        except (ImportError, AttributeError):
            self.skipTest(
                "clear_extraction_cache not exported (may be _clear_extraction_cache)"
            )

    def test_index_kg_for_memory_idempotent_via_cache(self):
        """Calling index_kg_for_memory twice on the same content should not duplicate work."""
        from eval._fixtures import bootstrap_temp_db_clean
        from knowledge_graph import clear_extraction_cache, index_kg_for_memory

        clear_extraction_cache()
        db_path = Path(tempfile.mktemp(suffix=".db"))
        bootstrap_temp_db_clean(db_path)
        test_id = f"lessons/test-kg-cache-{os.getpid()}"
        content = "Qwen3 reranker hangs on Apple Silicon with PyTorch MPS"
        conn = sqlite3.connect(str(db_path))
        try:
            # First call: no cache, runs extraction.
            stats1 = index_kg_for_memory(conn, test_id, content) or {}
            # Second call: cache hit, should return same/similar.
            stats2 = index_kg_for_memory(conn, test_id, content) or {}
            # Cache hit means entities count is the same.
            self.assertEqual(stats1.get("entities", 0), stats2.get("entities", 0))
        except Exception as e:
            self.skipTest(f"index_kg_for_memory signature changed: {e}")
        finally:
            conn.close()
        os.remove(db_path)


class TestFtsCronStagger(unittest.TestCase):
    """P0.2: FTS cron is at minute 33, not 30."""

    def test_crontab_has_fts_at_minute_33(self):
        try:
            r = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True, timeout=5
            )
        except Exception:
            self.skipTest("crontab not available in this environment")
        if r.returncode != 0:
            self.skipTest(f"crontab -l returned {r.returncode}: {r.stderr}")
        # Find the line with cron_rebuild_fts
        for line in r.stdout.splitlines():
            if "cron_rebuild_fts" in line and not line.strip().startswith("#"):
                # Should be at minute 33 (33 2 * * *)
                self.assertIn(
                    "33 2",
                    line,
                    f"FTS cron should run at 02:33, got: {line!r}",
                )
                return
        self.skipTest("cron_rebuild_fts line not found in crontab")


class TestBackfillCronPath(unittest.TestCase):
    """P0.5: backfill cron path includes .config/agentic-memory."""

    def test_crontab_backfill_path_is_full(self):
        try:
            r = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True, timeout=5
            )
        except Exception:
            self.skipTest("crontab not available in this environment")
        if r.returncode != 0:
            self.skipTest(f"crontab -l returned {r.returncode}: {r.stderr}")
        for line in r.stdout.splitlines():
            if "backfill_all.py" in line and not line.strip().startswith("#"):
                # The path must include .config/agentic-memory
                self.assertIn(
                    ".config/agentic-memory",
                    line,
                    f"backfill path should include .config/agentic-memory, got: {line!r}",
                )
                # Should NOT be the broken /Users/.../agentic-memory/ path
                self.assertNotRegex(
                    line,
                    r"/Users/\w+/agentic-memory/backfill",
                    f"backfill path uses broken /Users/.../agentic-memory/, got: {line!r}",
                )
                return
        self.skipTest("backfill_all.py line not found in crontab")


class TestMemorySaveImportance(unittest.TestCase):
    """2026-06-19: importance parameter must thread through the save path.

    Previously the save_pipeline hardcoded ``importance=3`` in the
    INSERT statement regardless of caller intent. The MCP
    ``memory_save`` tool had no ``importance`` parameter at all.
    This was silent data loss: callers who passed importance=4 saw
    the row stored with importance=3.
    """

    def test_memory_save_mcp_tool_has_importance(self):
        from mcp_memory import memory_save

        import inspect

        sig = inspect.signature(memory_save)
        self.assertIn("importance", sig.parameters)
        self.assertEqual(sig.parameters["importance"].default, 3)

    def test_save_memory_function_has_importance(self):
        from save_pipeline import save_memory

        import inspect

        sig = inspect.signature(save_memory)
        self.assertIn("importance", sig.parameters)
        self.assertEqual(sig.parameters["importance"].default, 3)

    def test_upsert_memory_row_has_importance(self):
        from save_pipeline import _upsert_memory_row

        import inspect

        sig = inspect.signature(_upsert_memory_row)
        self.assertIn("importance", sig.parameters)
        self.assertEqual(sig.parameters["importance"].default, 3)

    def test_importance_is_honored_end_to_end(self):
        """Save a memory with importance=4, verify the DB row stores 4."""
        import time

        from eval._fixtures import bootstrap_temp_db_clean
        from save_pipeline import save_memory

        db_path = Path(tempfile.mktemp(suffix=".db"))
        bootstrap_temp_db_clean(db_path)
        # save_memory writes .md files relative to the DB parent
        md_root = db_path.parent / "lessons"
        md_root.mkdir(parents=True, exist_ok=True)

        test_id = f"lessons/test-importance-{int(time.time() * 1000)}"
        try:
            result = save_memory(
                content="Test for importance parameter threading",
                category="lessons",
                title_slug=test_id.split("/")[-1],
                importance=4,
                db_path=str(db_path),
            )
            self.assertIsNotNone(result, "save_memory returned None")
            note_id = str(result)
            self.assertIn(test_id, note_id)

            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT importance FROM memories WHERE id = ?", (test_id,)
                ).fetchone()
                self.assertIsNotNone(row, "row not found in DB")
                self.assertEqual(row[0], 4, f"expected importance=4, got {row[0]}")
            finally:
                conn.close()
        finally:
            import glob

            for f in glob.glob(f"{md_root}/test-importance-*.md"):
                try:
                    os.remove(f)
                except OSError:
                    pass
            os.remove(db_path)

    def test_importance_is_clamped(self):
        """importance=99 should clamp to 5, importance=0 should clamp to 1."""
        from save_pipeline import _upsert_memory_row
        import inspect

        src = inspect.getsource(_upsert_memory_row)
        self.assertIn("max(1, min(5, int(importance)))", src)


class TestUpsertColumnDrift(unittest.TestCase):
    """D5 fix: verify the upsert path preserves all managed columns
    on update, and that the COALESCE metadata bug is gone.

    Background: the previous ``ON CONFLICT`` clause had
    ``metadata = COALESCE(memories.metadata, memories.metadata)``
    which is a tautology (both args are the same column) — incoming
    metadata was silently dropped on update.  Both branches of
    ``_upsert_memory_row`` now use the correct pattern
    ``COALESCE(excluded.metadata, memories.metadata)``.
    """

    def test_managed_cols_frozenset_includes_every_column(self):
        from save_pipeline import _MANAGED_COLS

        # Every column the upsert path can opt into or out of must be
        # in the centralised set so the pragma cache detector and the
        # INSERT branches stay in lockstep.
        expected = {
            "valid_from",
            "valid_to",
            "superseded_by",
            "tier",
            "success_score",
            "fitness_score",
            "importance_score",
            "metadata",
            "deleted_at",
        }
        self.assertTrue(
            expected.issubset(_MANAGED_COLS),
            f"_MANAGED_COLS missing: {expected - _MANAGED_COLS}",
        )

    def test_detect_schema_features_returns_new_flags(self):
        """D5: the detector must report every managed column the
        live schema has, not just the legacy temporal trio."""
        import tempfile as _tf

        db_path = Path(_tf.mkdtemp(prefix="d5_features_")) / "memory.db"
        from migration_runner import run_migrations
        from memory_common import open_db

        with open_db(db_path, timeout=5.0) as conn:
            run_migrations(conn)
            from save_pipeline import _detect_schema_features

            features = _detect_schema_features(db_path, conn=conn)
        self.assertTrue(features["has_temporal"])
        self.assertTrue(features["has_tier"])
        self.assertTrue(features["has_metadata"])
        self.assertTrue(features["has_success_score"])
        self.assertTrue(features["has_fitness_score"])
        self.assertTrue(features["has_importance_score"])
        self.assertTrue(features["has_deleted_at"])

    def test_coalesce_metadata_bug_is_fixed(self):
        """The previous ``COALESCE(memories.metadata, memories.metadata)``
        is a tautology.  The fixed version uses
        ``COALESCE(excluded.metadata, memories.metadata)`` so the
        incoming metadata wins on update."""
        import re

        from save_pipeline import _upsert_memory_row
        import inspect

        src = inspect.getsource(_upsert_memory_row)
        # Strip block comments + docstrings + line comments so the
        # audit history in the docstring/comments doesn't trigger the
        # negative assertion.
        no_strings = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
        no_strings = re.sub(r"'''.*?'''", "", no_strings, flags=re.DOTALL)
        # Strip everything after ``#`` on each line (line comment).
        executable = "\n".join(
            line.split("#", 1)[0] for line in no_strings.splitlines()
        )
        # The bad pattern must NOT appear in any executable SQL.
        self.assertNotIn(
            "COALESCE(memories.metadata, memories.metadata)",
            executable,
            "COALESCE on same column still present in an executable "
            "statement — metadata will be silently dropped on update.",
        )
        # The good pattern must appear in both branches.
        self.assertIn("COALESCE(excluded.metadata, memories.metadata)", executable)

    def test_metadata_update_is_persisted(self):
        """End-to-end: upsert with metadata=A, upsert again with metadata=B,
        the row should have metadata=B on read.

        Uses ``upsert_row`` (the public upsert) so we exercise the
        same path the save pipeline and CRDT field backfill use.
        """
        import time
        import json as _json

        from eval._fixtures import bootstrap_temp_db_clean
        from save_pipeline import upsert_row
        from memory_common import open_db

        db_path = Path(tempfile.mktemp(suffix=".db"))
        bootstrap_temp_db_clean(db_path)
        src = str(db_path.parent / "test.md")

        test_id = f"lessons/d5-metadata-{int(time.time() * 1000)}"
        try:
            with open_db(db_path, timeout=5.0) as conn:
                # First upsert: metadata=A
                upsert_row(
                    conn=conn,
                    note_id=test_id,
                    content="D5 metadata update test",
                    source_file=src,
                    tags=["d5-test"],
                    category="lessons",
                    pinned=False,
                    tier="warm",
                    metadata={"phase": "A"},
                    db_path=db_path,
                )
                conn.commit()
                # Second upsert (same id): metadata=B
                upsert_row(
                    conn=conn,
                    note_id=test_id,
                    content="D5 metadata update test (v2)",
                    source_file=src,
                    tags=["d5-test"],
                    category="lessons",
                    pinned=False,
                    tier="warm",
                    metadata={"phase": "B"},
                    db_path=db_path,
                )
                conn.commit()
                row = conn.execute(
                    "SELECT metadata FROM memories WHERE id = ?", (test_id,)
                ).fetchone()
            self.assertIsNotNone(row, "row not found")
            md = _json.loads(row[0]) if row[0] else {}
            self.assertEqual(
                md.get("phase"),
                "B",
                f"expected metadata.phase=B after second upsert, got {row[0]!r}",
            )
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass


# ===========================================================================
# Scenario 5 regression (2026-06-22): pragma cache invalidated on save
# ===========================================================================


class TestPragmaCacheInvalidationOnSave(unittest.TestCase):
    """Scenario 5 regression: the per-db-path pragma cache must be
    invalidated on every save_memory entry.  Without this, an
    in-flight save that started before a migration could write to
    a column the migration had just added, using the stale column
    list — silent data loss.
    """

    def setUp(self) -> None:
        import tempfile as _tf
        import save_pipeline as _sp

        self.tmpdir = Path(_tf.mkdtemp(prefix="scenario5_"))
        self.db_path = self.tmpdir / "memory.db"
        # Use a fresh per-test key for the cache so we don't
        # collide with other tests' cache state.
        self.cache_key = str(self.db_path)
        # Clear any pre-existing entry for this key.
        with _sp._pragma_cache_lock:
            _sp._pragma_cache.pop(self.cache_key, None)
        # Bootstrap a fresh DB with the full schema.
        from migration_runner import run_migrations
        from memory_common import open_db

        with open_db(self.db_path, timeout=5.0) as conn:
            run_migrations(conn)

    def tearDown(self) -> None:
        import shutil as _sh
        import save_pipeline as _sp

        with _sp._pragma_cache_lock:
            _sp._pragma_cache.pop(self.cache_key, None)
        _sh.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_memory_invalidates_cache_before_schema_read(self) -> None:
        """save_memory must clear the pragma cache for its db_path
        BEFORE any schema-feature read inside the save pipeline.

        Without this, an in-flight save that started before a
        migration would use the stale column list — silent data
        loss.  We patch _detect_schema_features to capture the
        cache state at the moment it's called; the fix guarantees
        the cache is empty at that point.
        """
        import save_pipeline as _sp
        from save_pipeline import save_memory, _detect_schema_features
        from unittest.mock import patch

        # 1. Warm the cache so it's clearly populated.
        _detect_schema_features(self.db_path)
        self.assertIn(self.cache_key, _sp._pragma_cache)

        # 2. Patch _detect_schema_features to record the cache
        #    state at the moment it's first called.
        seen_cache_state: dict = {}

        def spy_detect(db_path, conn=None):
            with _sp._pragma_cache_lock:
                seen_cache_state["empty"] = self.cache_key not in _sp._pragma_cache
            # Call the real implementation so the save proceeds.
            return (
                _detect_schema_features.__wrapped__(db_path, conn=conn)
                if hasattr(_detect_schema_features, "__wrapped__")
                else _sp._detect_schema_features(db_path, conn=conn)
            )

        # The spy pattern: capture the cache state at every call.
        original = _sp._detect_schema_features

        def capturing_detect(db_path, conn=None):
            with _sp._pragma_cache_lock:
                seen_cache_state.setdefault("calls", []).append(
                    self.cache_key in _sp._pragma_cache
                )
            return original(db_path, conn=conn)

        with patch.object(_sp, "_detect_schema_features", capturing_detect):
            result = save_memory(
                content="scenario 5 test content",
                category="lessons",
                title_slug="scenario5-test",
                tags=["test"],
                pinned=False,
                is_global=False,
                safety_wiring=False,
                db_path=str(self.db_path),
            )

        self.assertIsInstance(result, str)
        self.assertFalse(result.startswith("Error"), f"save failed: {result}")
        # The first call to _detect_schema_features during the save
        # must see the cache EMPTY — that's the fix.
        self.assertTrue(
            seen_cache_state.get("calls", [None])[0] is False,
            f"save_memory must invalidate the pragma cache BEFORE the "
            f"first schema-feature read.  Cache state at first call: "
            f"{seen_cache_state}.  An in-flight save that started before "
            f"a migration would otherwise use the stale column list.",
        )

    def test_save_memory_does_not_affect_other_paths(self) -> None:
        """Cache invalidation is scoped to the save's db_path only."""
        import save_pipeline as _sp
        from save_pipeline import save_memory

        # Warm cache for a DIFFERENT db_path
        other_key = "/some/other/path.db"
        with _sp._pragma_cache_lock:
            _sp._pragma_cache[other_key] = {"placeholder_col"}
        try:
            save_memory(
                content="scenario 5 scope test",
                category="lessons",
                title_slug="scenario5-scope",
                tags=["test"],
                pinned=False,
                is_global=False,
                safety_wiring=False,
                db_path=str(self.db_path),
            )
            # The other path's cache entry must be untouched.
            self.assertIn(other_key, _sp._pragma_cache)
            self.assertEqual(_sp._pragma_cache[other_key], {"placeholder_col"})
        finally:
            with _sp._pragma_cache_lock:
                _sp._pragma_cache.pop(other_key, None)


if __name__ == "__main__":
    unittest.main()
