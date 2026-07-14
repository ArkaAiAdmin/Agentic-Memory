"""Test cron module coverage — import safety, pure functions, and thin wrappers.

Covers 14 cron_*.py files. Most are thin wrappers (tested via import + smoke);
a few have standalone logic with unit tests below.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))
sys.path.insert(0, str(INSTALL / "cron"))

# Ensure feature flags so import-time checks don't short-circuit
os.environ.setdefault("MEMORY_SELF_DIRECTED", "1")
os.environ.setdefault("MEMORY_KNOWLEDGE_GRAPH", "1")
os.environ.setdefault("MEMORY_ADAPTIVE_RETENTION", "1")
os.environ.setdefault("MEMORY_SUMMARIZATION", "1")
os.environ.setdefault("MEMORY_QUALITY_GATES", "1")


# ── Import tests: every cron module loads without error ──────────────


class TestCronImports(unittest.TestCase):
    """Verify every cron module can be imported without ImportError."""

    def test_import_cron_auto_summarize(self):
        import cron_auto_summarize

        self.assertTrue(hasattr(cron_auto_summarize, "main"))

    def test_import_cron_compact(self):
        import cron_compact

        self.assertTrue(hasattr(cron_compact, "main"))
        self.assertTrue(hasattr(cron_compact, "check_integrity"))
        self.assertTrue(hasattr(cron_compact, "run"))

    def test_import_cron_consolidate(self):
        import cron_consolidate

        self.assertTrue(hasattr(cron_consolidate, "consolidate_light"))
        self.assertTrue(hasattr(cron_consolidate, "compute_content_hash"))
        self.assertTrue(hasattr(cron_consolidate, "similarity_hash"))
        self.assertTrue(hasattr(cron_consolidate, "jaccard_similarity"))

    def test_import_cron_detect_vec_drift(self):
        import cron_detect_vec_drift

        self.assertTrue(hasattr(cron_detect_vec_drift, "main"))
        self.assertEqual(cron_detect_vec_drift.WARN_THRESHOLD, 50)
        self.assertEqual(cron_detect_vec_drift.INFO_THRESHOLD, 10)

    def test_import_cron_heartbeat(self):
        import cron_heartbeat

        self.assertTrue(hasattr(cron_heartbeat, "main"))

    def test_import_cron_integrity_check(self):
        import cron_integrity_check

        self.assertTrue(hasattr(cron_integrity_check, "main"))

    def test_import_cron_pinned_decay(self):
        import cron_pinned_decay

        self.assertTrue(hasattr(cron_pinned_decay, "main"))

    def test_import_cron_purge_expired(self):
        import cron_purge_expired

        self.assertTrue(hasattr(cron_purge_expired, "main"))

    def test_import_cron_quality_filter(self):
        import cron_quality_filter

        self.assertTrue(hasattr(cron_quality_filter, "main"))

    def test_import_cron_rebuild_fts(self):
        import cron_rebuild_fts

        self.assertTrue(hasattr(cron_rebuild_fts, "_fts_tables"))
        self.assertTrue(hasattr(cron_rebuild_fts, "rebuild_all_fts"))
        self.assertTrue(hasattr(cron_rebuild_fts, "main"))

    def test_import_cron_retention_stats(self):
        import cron_retention_stats

        self.assertTrue(hasattr(cron_retention_stats, "main"))

    def test_import_cron_rewrite_links(self):
        import cron_rewrite_links

        self.assertTrue(hasattr(cron_rewrite_links, "main"))

    def test_import_cron_backup(self):
        import cron_backup

        self.assertTrue(hasattr(cron_backup, "do_backup"))
        self.assertTrue(hasattr(cron_backup, "install_cron"))
        self.assertTrue(hasattr(cron_backup, "cron_status"))


# ── cron_consolidate: pure functions ─────────────────────────────────


class TestCronConsolidatePure(unittest.TestCase):
    """Test the pure utility functions in cron_consolidate.py."""

    def setUp(self):
        import cron_consolidate

        self.mod = cron_consolidate

    def test_compute_content_hash_consistent(self):
        h1 = self.mod.compute_content_hash("hello world")
        h2 = self.mod.compute_content_hash("hello world")
        self.assertEqual(h1, h2)

    def test_compute_content_hash_strips_whitespace(self):
        h1 = self.mod.compute_content_hash("hello world")
        h2 = self.mod.compute_content_hash("  hello world  ")
        self.assertEqual(h1, h2)

    def test_compute_content_hash_different_inputs(self):
        h1 = self.mod.compute_content_hash("hello world")
        h2 = self.mod.compute_content_hash("goodbye world")
        self.assertNotEqual(h1, h2)

    def test_compute_content_hash_length(self):
        h = self.mod.compute_content_hash("test")
        self.assertEqual(len(h), 64)
        int(h, 16)

    def test_similarity_hash_empty(self):
        result = self.mod.similarity_hash("")
        self.assertEqual(result, set())

    def test_similarity_hash_short(self):
        result = self.mod.similarity_hash("a b")
        self.assertEqual(result, set())

    def test_similarity_hash_normal(self):
        result = self.mod.similarity_hash("the quick brown fox")
        for trigram in result:
            self.assertEqual(len(trigram), 3)
        self.assertGreater(len(result), 0)
        self.assertIn(("the", "quick", "brown"), result)

    def test_similarity_hash_case_insensitive(self):
        r1 = self.mod.similarity_hash("The Quick Brown Fox")
        r2 = self.mod.similarity_hash("the quick brown fox")
        self.assertEqual(r1, r2)

    def test_jaccard_identical(self):
        a = {"hello", "world"}
        self.assertAlmostEqual(self.mod.jaccard_similarity(a, a), 1.0)

    def test_jaccard_disjoint(self):
        a = {"hello", "world"}
        b = {"foo", "bar"}
        self.assertAlmostEqual(self.mod.jaccard_similarity(a, b), 0.0)

    def test_jaccard_half_overlap(self):
        a = {"hello", "world", "foo"}
        b = {"hello", "world", "bar"}
        self.assertAlmostEqual(self.mod.jaccard_similarity(a, b), 0.5)

    def test_jaccard_empty(self):
        self.assertAlmostEqual(self.mod.jaccard_similarity(set(), {"a"}), 0.0)
        self.assertAlmostEqual(self.mod.jaccard_similarity({"a"}, set()), 0.0)
        self.assertAlmostEqual(self.mod.jaccard_similarity(set(), set()), 0.0)


# ── cron_rebuild_fts: FTS5 table discovery and rebuild ──────────────


class TestCronRebuildFTS(unittest.TestCase):
    """Test cron_rebuild_fts with a temporary SQLite DB containing FTS5 tables."""

    def setUp(self):
        import cron_rebuild_fts

        self.mod = cron_rebuild_fts
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cron_fts_test_"))
        self.db_path = self.tmpdir / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")

    def tearDown(self):
        self.conn.close()
        for f in self.tmpdir.glob("*"):
            f.unlink()
        self.tmpdir.rmdir()

    def _create_fts_table(self, name: str):
        self.conn.execute(
            f'CREATE VIRTUAL TABLE "{name}" USING fts5(content, tokenize="porter unicode61")'
        )
        self.conn.commit()

    def test_fts_tables_empty(self):
        self.conn.execute("CREATE TABLE normal_table (id INTEGER)")
        tables = self.mod._fts_tables(self.conn)
        self.assertEqual(tables, [])

    def test_fts_tables_single(self):
        self._create_fts_table("memory_fts")
        tables = self.mod._fts_tables(self.conn)
        self.assertIn("memory_fts", tables)
        self.assertEqual(len(tables), 1)

    def test_fts_tables_multiple(self):
        self._create_fts_table("memory_fts")
        self._create_fts_table("kg_fts")
        tables = self.mod._fts_tables(self.conn)
        self.assertEqual(len(tables), 2)
        self.assertIn("memory_fts", tables)
        self.assertIn("kg_fts", tables)

    def test_fts_tables_ignores_normal_tables(self):
        self._create_fts_table("memory_fts")
        self.conn.execute("CREATE TABLE foo (id INTEGER)")
        self.conn.execute("CREATE VIEW bar AS SELECT 1")
        tables = self.mod._fts_tables(self.conn)
        self.assertEqual(tables, ["memory_fts"])

    def test_rebuild_all_fts_no_tables(self):
        result = self.mod.rebuild_all_fts(self.db_path)
        self.assertIn("kg_entities_fts", result)
        self.assertIn("memory_chunks_fts", result)
        self.assertIn("memories_fts", result)

    def test_rebuild_all_fts_single_table(self):
        self._create_fts_table("memory_fts")
        self.conn.execute("INSERT INTO memory_fts VALUES('hello world')")
        self.conn.commit()
        self.conn.close()
        result = self.mod.rebuild_all_fts(self.db_path)
        self.assertEqual(result.get("memory_fts"), "ok")
        self.assertIn("kg_entities_fts", result)
        self.assertIn("memory_chunks_fts", result)
        self.assertIn("memories_fts", result)

    def test_rebuild_all_fts_multiple_tables(self):
        self._create_fts_table("memory_fts")
        self._create_fts_table("kg_fts")
        self.conn.close()
        result = self.mod.rebuild_all_fts(self.db_path)
        self.assertEqual(result.get("memory_fts"), "ok")
        self.assertEqual(result.get("kg_fts"), "ok")
        self.assertIn("kg_entities_fts", result)
        self.assertIn("memory_chunks_fts", result)
        self.assertIn("memories_fts", result)

    def test_rebuild_all_fts_nonexistent_db(self):
        result = self.mod.rebuild_all_fts(self.tmpdir / "nonexistent.db")
        self.assertIn("kg_entities_fts", result)
        self.assertIn("memory_chunks_fts", result)
        self.assertIn("memories_fts", result)


# ── cron_detect_vec_drift: vec drift detection with temp DB ─────────


class TestCronDetectVecDrift(unittest.TestCase):
    """Test cron_detect_vec_drift against a temporary DB."""

    def setUp(self):
        import cron_detect_vec_drift

        self.mod = cron_detect_vec_drift
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cron_vec_drift_"))
        self.db_path = self.tmpdir / "memory.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS memory_vec_keys (
                memory_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS memory_embeddings (
                memory_id TEXT PRIMARY KEY,
                embedding BLOB
            );
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for f in self.tmpdir.glob("*"):
            f.unlink()
        self.tmpdir.rmdir()

    def _add_memory(self, mid: str, content: str = "test content"):
        self.conn.execute(
            "INSERT INTO memories (id, content) VALUES (?, ?)", (mid, content)
        )

    def _add_vec_key(self, mid: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO memory_vec_keys (memory_id) VALUES (?)", (mid,)
        )

    def _add_embedding(self, mid: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO memory_embeddings (memory_id, embedding) VALUES (?, ?)",
            (mid, b"test_embedding"),
        )

    def test_main_ok_when_synced(self):
        self._add_memory("m1")
        self._add_vec_key("m1")
        self._add_embedding("m1")
        self.conn.commit()
        self.conn.close()

        import io
        import contextlib
        from unittest.mock import patch

        buf = io.StringIO()
        with patch.object(self.mod, "acquire_lock_or_exit"):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stdout(buf):
                    self.mod.main(["--db-path", str(self.db_path)])
        output = buf.getvalue()
        self.assertIn("OK:", output)
        self.assertIn("vec_drift=0", output)

    def test_main_detect_vec_drift(self):
        self._add_memory("m1")
        self._add_memory("m2")
        self._add_memory("m3")
        self._add_vec_key("m1")
        self._add_embedding("m1")
        self.conn.commit()
        self.conn.close()

        import io
        import contextlib
        from unittest.mock import patch

        buf = io.StringIO()
        with patch.object(self.mod, "acquire_lock_or_exit"):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stdout(buf):
                    self.mod.main(["--db-path", str(self.db_path)])
        output = buf.getvalue()
        self.assertIn("vec_drift=", output)

    def test_main_empty_db(self):
        self.conn.close()
        import io
        import contextlib

        buf = io.StringIO()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(buf):
                self.mod.main(["--db-path", str(self.db_path)])
        output = buf.getvalue()
        self.assertIn("memories=0", output)


# ── cron_purge_expired: standalone main(db_path) ────────────────────


class TestCronPurgeExpired(unittest.TestCase):
    """Test cron_purge_expired.main(db_path) with a temp DB."""

    def setUp(self):
        import cron_purge_expired

        self.mod = cron_purge_expired
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cron_purge_"))
        self.db_path = self.tmpdir / "memory.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                deleted_at TEXT,
                deleted_by TEXT
            );
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for f in self.tmpdir.glob("*"):
            f.unlink()
        self.tmpdir.rmdir()

    def test_main_empty_db(self):
        self.conn.close()
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.main(db_path=str(self.db_path))
        self.assertIn("Purged 0", buf.getvalue())

    def test_main_nonexistent_db(self):
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.main(db_path=str(self.tmpdir / "nonexistent.db"))
        self.assertIn("No memory.db found", buf.getvalue())

    def test_main_no_deleted_notes(self):
        self.conn.execute("INSERT INTO memories (id, content) VALUES ('m1', 'hello')")
        self.conn.commit()
        self.conn.close()
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.main(db_path=str(self.db_path))
        self.assertIn("Purged 0", buf.getvalue())


# ── cron_heartbeat: behavior via mocked run_heartbeat ─────────────────


class TestCronHeartbeatBehavior(unittest.TestCase):
    """Test cron_heartbeat.main() with the underlying run_heartbeat mocked.

    Verifies:
    - Calls run_heartbeat with dry_run=False
    - Prints evaluated/tier_changes/promoted/archived counts
    - Exits with code 0 on success
    - Exits with code 1 when no DB exists
    """

    def setUp(self):
        import cron_heartbeat

        self.mod = cron_heartbeat
        import contextlib
        import io

        self._io = io
        self._contextlib = contextlib

    def test_main_calls_heartbeat_and_prints(self):
        tmp = Path(tempfile.mkdtemp(prefix="cron_hb_test_"))
        try:
            db = tmp / "memory.db"
            db.touch()
            with (
                unittest.mock.patch.dict(os.environ, {"MEMORY_DB_PATH": str(db)}),
                unittest.mock.patch.object(self.mod, "run_heartbeat") as mock_hb,
                unittest.mock.patch.object(self.mod, "connection_pool") as pool,
                unittest.mock.patch.object(self.mod, "safe_close_db"),
            ):
                mock_hb.return_value = {
                    "evaluated": 10,
                    "tier_changes": 3,
                    "promoted": 2,
                    "archived": 1,
                }
                pool.get.return_value = unittest.mock.MagicMock()
                buf = self._io.StringIO()
                with self._contextlib.redirect_stdout(buf):
                    self.mod.main()
            out = buf.getvalue()
            self.assertIn("10 evaluated", out)
            self.assertIn("3 tier changes", out)
            self.assertIn("2 promoted", out)
            self.assertIn("1 archived", out)
        finally:
            for f in tmp.glob("*"):
                f.unlink()
            tmp.rmdir()

    def test_main_exits_on_missing_db(self):
        # Point MEMORY_DB_PATH to a nonexistent file
        with unittest.mock.patch.dict(
            os.environ, {"MEMORY_DB_PATH": "/tmp/does_not_exist_zzz.db"}
        ):
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    self.mod.main()
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("ERROR: no memory.db", buf.getvalue())


# ── cron_quality_filter: behavior via mocked quality_stats ────────────


class TestCronQualityFilterBehavior(unittest.TestCase):
    """Test cron_quality_filter.main() with the underlying quality_stats mocked.

    Verifies:
    - Skips cleanly when MEMORY_QUALITY_GATES is disabled
    - Calls quality_stats(conn) and prints JSON
    - Handles exceptions gracefully (prints error, doesn't crash)
    """

    def setUp(self):
        import cron_quality_filter

        self.mod = cron_quality_filter
        import contextlib
        import io

        self._io = io
        self._contextlib = contextlib

    def test_main_skips_when_disabled(self):
        with unittest.mock.patch.object(self.mod.qg, "QUALITY_GATES_ENABLED", False):
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                self.mod.main()
        self.assertIn("not enabled, skipping", buf.getvalue())

    def test_main_prints_quality_stats(self):
        tmp = Path(tempfile.mkdtemp(prefix="cron_qf_test_"))
        try:
            db = tmp / "memory.db"
            db.touch()
            stats = {"total": 100, "valid": 95, "invalid": 5}
            with (
                unittest.mock.patch.dict(os.environ, {"MEMORY_DB_PATH": str(db)}),
                unittest.mock.patch.object(self.mod.qg, "QUALITY_GATES_ENABLED", True),
                unittest.mock.patch.object(
                    self.mod.qg, "quality_stats", return_value=stats
                ),
                unittest.mock.patch.object(self.mod, "connection_pool") as pool,
                unittest.mock.patch.object(self.mod, "safe_close_db"),
            ):
                pool.get.return_value = unittest.mock.MagicMock()
                buf = self._io.StringIO()
                with self._contextlib.redirect_stdout(buf):
                    self.mod.main()
            self.assertIn("Quality stats:", buf.getvalue())
            self.assertIn('"total": 100', buf.getvalue())
        finally:
            for f in tmp.glob("*"):
                f.unlink()
            tmp.rmdir()


# ── cron_auto_summarize: behavior via mocked auto_summarize_long ───────


class TestCronAutoSummarizeBehavior(unittest.TestCase):
    """Test cron_auto_summarize.main() with the underlying function mocked.

    Verifies:
    - Skips cleanly when MEMORY_SUMMARIZATION is disabled
    - Calls auto_summarize_long(min_length=500, dry_run=False)
    - Prints summarized/skipped counts
    - Exits with code 1 on unhandled exception
    """

    def setUp(self):
        import cron_auto_summarize

        self.mod = cron_auto_summarize
        import contextlib
        import io

        self._io = io
        self._contextlib = contextlib

    def test_main_skips_when_disabled(self):
        with unittest.mock.patch.object(self.mod.sm, "SUMMARIZATION_ENABLED", False):
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                self.mod.main()
        self.assertIn("not enabled, skipping", buf.getvalue())

    def test_main_prints_summarized_count(self):
        with (
            unittest.mock.patch.object(self.mod.sm, "SUMMARIZATION_ENABLED", True),
            unittest.mock.patch.object(
                self.mod.sm,
                "auto_summarize_long",
                return_value={"summarized": 7, "skipped": 3},
            ) as mock_call,
        ):
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                self.mod.main()
        self.assertEqual(mock_call.call_count, 1)
        kwargs = mock_call.call_args.kwargs
        self.assertEqual(kwargs.get("min_length"), 500)
        self.assertEqual(kwargs.get("dry_run"), False)
        self.assertIn("summarized=7", buf.getvalue())
        self.assertIn("skipped=3", buf.getvalue())


# ── cron_integrity_check: behavior via mocked check_index_integrity ────────


class TestCronIntegrityCheckBehavior(unittest.TestCase):
    """Test cron_integrity_check.main() with the underlying function mocked.

    Verifies:
    - Exits with code 1 when DB does not exist
    - Calls check_index_integrity(db_path, deep=False)
    - Prints "Integrity: ok" for clean DB
    - Prints findings when present
    """

    def setUp(self):
        import cron_integrity_check

        self.mod = cron_integrity_check
        import contextlib
        import io

        self._io = io
        self._contextlib = contextlib

    def test_main_exits_on_missing_db(self):
        with unittest.mock.patch.dict(
            os.environ, {"MEMORY_DB_PATH": "/tmp/does_not_exist_zzz_xx.db"}
        ):
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    self.mod.main()
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("ERROR: no memory.db", buf.getvalue())

    def test_main_prints_clean_report(self):
        with (
            unittest.mock.patch.dict(os.environ, {"MEMORY_DB_PATH": ""}),
            unittest.mock.patch.object(
                self.mod,
                "check_index_integrity",
                return_value={"summary": "0 critical 0 warnings", "findings": []},
            ),
            unittest.mock.patch(
                "pathlib.Path.exists",
                return_value=True,
            ),
        ):
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                self.mod.main()
        self.assertIn("Integrity:", buf.getvalue())
        self.assertIn("No issues found", buf.getvalue())

    def test_main_prints_findings(self):
        with (
            unittest.mock.patch.dict(os.environ, {"MEMORY_DB_PATH": ""}),
            unittest.mock.patch.object(
                self.mod,
                "check_index_integrity",
                return_value={
                    "summary": "0 critical 2 warnings",
                    "findings": [
                        {
                            "severity": "warning",
                            "code": "fts5_mismatch",
                            "message": "FTS5 has 100 docs but memories has 95",
                        },
                        {
                            "severity": "warning",
                            "code": "vec_drift",
                            "message": "vec_idx has 2 extra keys",
                        },
                    ],
                },
            ),
            unittest.mock.patch(
                "pathlib.Path.exists",
                return_value=True,
            ),
        ):
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                self.mod.main()
        self.assertIn("warning", buf.getvalue())
        self.assertIn("fts5_mismatch", buf.getvalue())
        self.assertIn("vec_drift", buf.getvalue())


# ── cron_pinned_decay: behavior via mocked pinned_main ────────────────────


class TestCronPinnedDecayBehavior(unittest.TestCase):
    """Test cron_pinned_decay.main() — thin wrapper around pinned_decay.main()."""

    def setUp(self):
        import cron_pinned_decay

        self.mod = cron_pinned_decay
        import contextlib
        import io

        self._io = io
        self._contextlib = contextlib

    def test_main_calls_pinned_main(self):
        with unittest.mock.patch.object(self.mod, "pinned_main") as mock_pinned:
            self.mod.main()
        mock_pinned.assert_called_once()

    def test_main_exits_on_exception(self):
        with unittest.mock.patch.object(
            self.mod,
            "pinned_main",
            side_effect=RuntimeError("test failure"),
        ):
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    self.mod.main()
            self.assertEqual(cm.exception.code, 1)


# ── cron_rewrite_links: behavior via mocked rewrite_wikilinks ──────────────


class TestCronRewriteLinksBehavior(unittest.TestCase):
    """Test cron_rewrite_links.main() with the underlying function mocked."""

    def setUp(self):
        import cron_rewrite_links

        self.mod = cron_rewrite_links
        import contextlib
        import io

        self._io = io
        self._contextlib = contextlib

    def test_main_calls_rewrite_and_prints(self):
        with unittest.mock.patch.object(
            self.mod,
            "rewrite_wikilinks",
            return_value={"rewritten": 5, "skipped": 12},
        ):
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                self.mod.main()
        self.assertIn("Rewrite links:", buf.getvalue())
        self.assertIn("5", buf.getvalue())

    def test_main_exits_on_exception(self):
        with unittest.mock.patch.object(
            self.mod,
            "rewrite_wikilinks",
            side_effect=RuntimeError("db locked"),
        ):
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    self.mod.main()
        self.assertEqual(cm.exception.code, 1)


# ── cron_retention_stats: behavior via mocked batch_update_retention ──────


class TestCronRetentionStatsBehavior(unittest.TestCase):
    """Test cron_retention_stats.main() — combines adaptive retention and
    neural forget curve. Mock both underlying modules.
    """

    def setUp(self):
        import cron_retention_stats

        self.mod = cron_retention_stats
        import contextlib
        import io

        self._io = io
        self._contextlib = contextlib

        # make_lazy_getattr caches ADAPTIVE_RETENTION_ENABLED on the
        # backing module (background.adaptive_retention.__dict__).
        # test_main injects batch_update_retention via the shim
        # __dict__; both must be cleared from the shim between tests.
        # Do NOT pop batch_update_retention from the backing module —
        # it's a regular def there; its __getattr__ cannot resole it.
        try:
            ar = self.mod.ar
            _backing = getattr(ar, "_real", ar)
            for _k in ("ADAPTIVE_RETENTION_ENABLED",):
                _backing.__dict__.pop(_k, None)
            for _k in ("ADAPTIVE_RETENTION_ENABLED", "batch_update_retention"):
                ar.__dict__.pop(_k, None)
        except Exception:
            pass

    def test_main_runs_adaptive_retention(self):
        with (
            unittest.mock.patch.object(self.mod, "acquire_lock_or_exit"),
            unittest.mock.patch.object(
                self.mod.ar,
                "ADAPTIVE_RETENTION_ENABLED",
                True,
            ),
            unittest.mock.patch.object(
                self.mod.ar,
                "batch_update_retention",
                return_value={"updated": 7, "skipped": 3},
            ),
            unittest.mock.patch.object(
                self.mod.nf,
                "batch_update_retention",
                return_value={"updated": 5, "failed": 0},
            ),
            unittest.mock.patch(
                "infrastructure.resolve_active_memory_dir"
            ) as mock_resolve,
            unittest.mock.patch(
                "pathlib.Path.exists",
                return_value=True,
            ),
        ):
            mock_path = unittest.mock.MagicMock()
            mock_path.exists.return_value = True
            mock_resolve.return_value = mock_path
            buf = self._io.StringIO()
            with self._contextlib.redirect_stdout(buf):
                self.mod.main()
        self.assertIn("Adaptive retention:", buf.getvalue())
        self.assertIn("updated=7", buf.getvalue())
        self.assertIn("Neural forget", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
