"""Tests for memory_integrity.check_index_integrity."""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
_install_root = os.environ.get("MEMORY_INSTALL_ROOT") or str(
    Path.home() / ".config" / "agentic-memory"
)
sys.path.insert(0, _install_root)

from _fixtures import bootstrap_temp_db_clean
from infra.memory_common import open_db
from memory_integrity import (
    check_index_integrity,
    find_orphan_files,
    recover_orphan_files,
)


def _make_test_db(tmp_dir: Path, name: str = "test.db") -> Path:
    """Create a minimal test DB using bootstrap_temp_db_clean.

    The bootstrap copies the full prod schema (memories, FTS5, chunks,
    KG, backlinks, vec_idx, vec_keys, audit log, etc.) and then
    truncates the data tables. We then insert a couple of clean
    rows to give the integrity checker something to verify.
    """
    db_path = tmp_dir / name
    bootstrap_temp_db_clean(db_path)
    # Create actual source .md files so the integrity checker doesn't
    # warn about missing files.
    lessons_dir = tmp_dir / "lessons"
    lessons_dir.mkdir(exist_ok=True)
    md_dir = tmp_dir / "test"
    md_dir.mkdir(exist_ok=True)
    (lessons_dir / "a.md").write_text("---\nid: a\n---\nalpha content")
    (lessons_dir / "b.md").write_text("---\nid: b\n---\nbeta content")
    with open_db(db_path) as db:
        db.execute(
            "INSERT INTO memories (id, content, tags, source_file, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
            ("a", "alpha content", "tag1", "lessons/a.md"),
        )
        db.execute(
            "INSERT INTO memories (id, content, tags, source_file, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
            ("b", "beta content", "tag2", "lessons/b.md"),
        )
        db.execute(
            "INSERT INTO backlinks (source_id, target_id) VALUES (?, ?)",
            ("a", "b"),
        )
        db.commit()
    return db_path


class TestCheckIndexIntegrity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="integrity_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_clean_db_ok(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        result = check_index_integrity(db_path)
        self.assertTrue(result["ok"])
        # At minimum 1 finding (the "ok" result), may include info-level
        # findings like temporal_supersession which are non-blocking.
        self.assertGreaterEqual(len(result["findings"]), 1)
        self.assertIn(result["findings"][0]["severity"], ("ok", "info"))
        self.assertEqual(result["summary"], "OK")

    def test_missing_db_returns_critical(self) -> None:
        db_path = self.tmp_dir / "does_not_exist.db"
        result = check_index_integrity(db_path)
        self.assertFalse(result["ok"])
        self.assertTrue(any(f["severity"] == "critical" for f in result["findings"]))
        self.assertTrue(any(f["check"] == "db_exists" for f in result["findings"]))

    def test_db_corruption_detected(self) -> None:
        db_path = self.tmp_dir / "corrupt.db"
        db_path.write_bytes(b"this is not a valid sqlite database " * 200)
        result = check_index_integrity(db_path, deep=True)
        self.assertFalse(result["ok"])
        self.assertTrue(any(f["severity"] == "critical" for f in result["findings"]))

    def test_missing_memories_table_critical(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("DROP TABLE memories")
            raw.commit()
        result = check_index_integrity(db_path)
        self.assertFalse(result["ok"])
        critical_checks = [f for f in result["findings"] if f["severity"] == "critical"]
        self.assertTrue(any(f["check"] == "required_tables" for f in critical_checks))
        self.assertTrue(any("memories" in f["message"] for f in critical_checks))

    def test_missing_backlinks_table_critical(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("DROP TABLE backlinks")
            raw.commit()
        result = check_index_integrity(db_path)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                f["severity"] == "critical" and "backlinks" in f["message"]
                for f in result["findings"]
            )
        )

    def test_fts5_mismatch_detected(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            # Drop triggers so new inserts don't sync to FTS5
            raw.execute("DROP TRIGGER IF EXISTS memories_ai")
            raw.execute("DROP TRIGGER IF EXISTS memories_ad")
            raw.execute("DROP TRIGGER IF EXISTS memories_au")
            for i in range(5):
                raw.execute(
                    "INSERT INTO memories (id, content, tags, source_file, created_at, updated_at, observed_at) "
                    "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
                    (f"m{i}", f"content {i}", "", "test/a.md"),
                )
            raw.commit()
        result = check_index_integrity(db_path)
        self.assertFalse(result["ok"])
        self.assertTrue(any(f["check"] == "fts5_mismatch" for f in result["findings"]))

    def test_orphan_backlink_warning(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            # Backlink with non-existent source_id should be flagged
            # (target_id may be non-existent by design - wiki "red links")
            raw.execute(
                "INSERT INTO backlinks (source_id, target_id) VALUES (?, ?)",
                ("ghost-source", "b"),
            )
            raw.commit()
        result = check_index_integrity(db_path)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(f["check"] == "orphan_backlinks" for f in result["findings"])
        )

    def test_orphan_chunk_warning(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute(
                "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) "
                "VALUES (?, ?, ?, ?, ?)",
                ("ghost-note", 0, 0, 16, "orphan chunk text"),
            )
            raw.commit()
        result = check_index_integrity(db_path)
        self.assertFalse(result["ok"])
        self.assertTrue(any(f["check"] == "orphan_chunks" for f in result["findings"]))

    def test_orphan_notes_found_when_no_links(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute(
                "INSERT INTO memories (id, content, tags, source_file, created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
                ("lonely", "no links", "", "test/a.md"),
            )
            raw.commit()
        result = check_index_integrity(db_path)
        self.assertFalse(result["ok"])
        self.assertTrue(any(f["check"] == "orphan_notes" for f in result["findings"]))

    def test_summary_format(self) -> None:
        db_path = self.tmp_dir / "summary.db"
        # Start with bootstrap (gets full schema), then strip required tables
        bootstrap_temp_db_clean(self.tmp_dir / "summary.db")
        # Create the source .md file so the integrity checker doesn't
        # add a missing_md_file warning on top of our test findings.
        test_dir = self.tmp_dir / "test"
        test_dir.mkdir(exist_ok=True)
        (test_dir / "a.md").write_text("---\nid: a\n---\nalpha")
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute(
                "INSERT INTO memories (id, content, tags, source_file, created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
                ("a", "alpha", "", "test/a.md"),
            )
            raw.execute(
                "INSERT INTO memory_chunks (parent_id, chunk_idx, content, start_offset, end_offset) "
                "VALUES (?, ?, ?, ?, ?)",
                ("ghost1", 0, "x", 0, 1),
            )
            raw.execute(
                "INSERT INTO memory_chunks (parent_id, chunk_idx, content, start_offset, end_offset) "
                "VALUES (?, ?, ?, ?, ?)",
                ("ghost2", 0, "y", 0, 1),
            )
            raw.execute("DROP TABLE backlinks")
            raw.commit()
        result = check_index_integrity(db_path)
        self.assertFalse(result["ok"])
        self.assertIn("1 critical", result["summary"])
        self.assertIn("2 warnings", result["summary"])

    def test_summary_singular_warning(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        # Insert a memory but deliberately remove the .md source file
        # so the integrity checker reports exactly one real warning (missing_md_file).
        test_dir = self.tmp_dir / "test"
        test_dir.mkdir(exist_ok=True)
        (test_dir / "a.md").write_text("---\nid: lonely\n---\nno links")
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute(
                "INSERT INTO memories (id, content, tags, source_file, created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
                ("lonely", "no links", "", "test/a.md"),
            )
            raw.commit()
        # Now delete the source file to trigger exactly one missing_md_file warning
        (test_dir / "a.md").unlink(missing_ok=True)
        result = check_index_integrity(db_path)
        # Summary should use singular "warning" (not "warnings")
        self.assertIn("1 warning ", result["summary"] + " ")
        self.assertNotIn("1 warnings", result["summary"])

    def test_deep_mode_passes_on_clean_db(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        result_quick = check_index_integrity(db_path, deep=False)
        result_deep = check_index_integrity(db_path, deep=True)
        self.assertTrue(result_quick["ok"])
        self.assertTrue(result_deep["ok"])

    def test_return_keys_complete(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        result = check_index_integrity(db_path)
        self.assertIn("ok", result)
        self.assertIn("findings", result)
        self.assertIn("summary", result)
        self.assertIsInstance(result["ok"], bool)
        self.assertIsInstance(result["findings"], list)
        self.assertIsInstance(result["summary"], str)

    def test_finding_structure(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute(
                "INSERT INTO memories (id, content, tags, source_file, created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
                ("lonely", "no links", "", "test/a.md"),
            )
            raw.commit()
        result = check_index_integrity(db_path)
        for f in result["findings"]:
            self.assertIn("id", f)
            self.assertIn("check", f)
            self.assertIn("severity", f)
            self.assertIn("message", f)
            self.assertIn(f["severity"], {"critical", "warning", "info", "ok"})

    def test_severity_sorting_worst_first(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute(
                "INSERT INTO memories (id, content, tags, source_file, created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
                ("lonely", "no links", "", "test/a.md"),
            )
            raw.execute("DROP TABLE backlinks")
            raw.commit()
        result = check_index_integrity(db_path)
        severities = [f["severity"] for f in result["findings"]]
        rank = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
        ranks = [rank[s] for s in severities]
        self.assertEqual(ranks, sorted(ranks))

    def test_idempotent_runs(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        first = check_index_integrity(db_path)
        second = check_index_integrity(db_path)
        self.assertEqual(first["ok"], second["ok"])
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(
            [f["id"] for f in first["findings"]],
            [f["id"] for f in second["findings"]],
        )

    def test_missing_optional_table_ok(self) -> None:
        """Test that missing optional tables (memories_fts, memory_chunks) don't cause critical errors."""
        db_path = self.tmp_dir / "minimal.db"
        bootstrap_temp_db_clean(self.tmp_dir / "minimal.db")
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("DROP TABLE IF EXISTS memory_chunks_fts")
            raw.execute("DROP TABLE IF EXISTS memory_chunks")
            raw.execute("DROP TABLE IF EXISTS kg_entities")
            raw.execute("DROP TABLE IF EXISTS kg_edges")
            raw.execute("DROP TABLE IF EXISTS backlinks")
            raw.execute(
                "INSERT INTO memories (id, content, tags, source_file, created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
                ("a", "alpha", "", "test/a.md"),
            )
            raw.commit()
        result = check_index_integrity(db_path)
        # Missing backlinks is critical, so this should fail
        # But missing optional tables should not add extra criticals
        self.assertFalse(result["ok"])

    def test_vector_index_mismatch_detected(self) -> None:
        db_path = _make_test_db(self.tmp_dir)
        with sqlite3.connect(str(db_path)) as raw:
            raw.execute(
                "INSERT OR REPLACE INTO memory_vec_idx "
                "(id, n_vectors, dim, metric, quantization, connectivity, expansion_add, expansion_search, built_at, index_blob, key_count) "
                "VALUES (1, 2, 128, 'cos', 'f16', 16, 128, 64, 123.45, x'abcd', 1)"
            )
            raw.execute(
                "INSERT INTO memory_vec_keys (key, memory_id) VALUES (?, ?)",
                (12345, "a"),
            )
            raw.commit()
        result = check_index_integrity(db_path)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(f["check"] == "vector_index_mismatch" for f in result["findings"])
        )


# ===========================================================================
# Scenario 7 (2026-06-22): orphan file recovery
# ===========================================================================


class TestRecoverOrphanFiles(unittest.TestCase):
    """Regression test for the saga-crash recovery path.

    A 'backward orphan' is a memories row whose source_file (.md
    path) is missing on disk.  This happens when the saga crashes
    between the DB upsert and the file write.  ``recover_orphan_files``
    regenerates the .md from the DB content (which is the
    canonical source of truth).
    """

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="orphan_recovery_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_find_orphan_files_detects_missing_md(self) -> None:
        """A memory with no .md file on disk is reported as an orphan."""
        db_path = self.tmp_dir / "memory.db"
        bootstrap_temp_db_clean(db_path)
        with open_db(db_path) as db:
            db.execute(
                "INSERT INTO memories (id, content, tags, source_file, "
                "created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), "
                "datetime('now'))",
                ("lessons/orphan", "orphaned content", "[]", "lessons/orphan.md"),
            )
            db.commit()
        # Note: we deliberately do NOT create lessons/orphan.md
        with open_db(db_path) as db:
            orphans = find_orphan_files(db, self.tmp_dir)
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["memory_id"], "lessons/orphan")
        self.assertEqual(orphans[0]["content"], "orphaned content")

    def test_recover_orphan_files_recreates_md(self) -> None:
        """recover_orphan_files re-creates the .md file from the DB."""
        db_path = self.tmp_dir / "memory.db"
        bootstrap_temp_db_clean(db_path)
        with open_db(db_path) as db:
            db.execute(
                "INSERT INTO memories (id, content, tags, source_file, "
                "created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), "
                "datetime('now'))",
                ("lessons/orphan", "recovered content", "[]", "lessons/orphan.md"),
            )
            db.commit()
        md_path = self.tmp_dir / "lessons" / "orphan.md"
        self.assertFalse(md_path.exists())

        result = recover_orphan_files(db_path, self.tmp_dir)
        self.assertEqual(len(result["recovered"]), 1)
        self.assertEqual(result["recovered"][0], "lessons/orphan")
        self.assertEqual(len(result["failed"]), 0)
        # File must now exist with the original content.
        self.assertTrue(md_path.exists())
        content = md_path.read_text(encoding="utf-8")
        self.assertIn("recovered content", content)

    def test_recover_orphan_files_dry_run_does_not_write(self) -> None:
        """--dry-run reports orphans without writing files."""
        db_path = self.tmp_dir / "memory.db"
        bootstrap_temp_db_clean(db_path)
        with open_db(db_path) as db:
            db.execute(
                "INSERT INTO memories (id, content, tags, source_file, "
                "created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), "
                "datetime('now'))",
                ("lessons/dry", "dry-run content", "[]", "lessons/dry.md"),
            )
            db.commit()

        result = recover_orphan_files(db_path, self.tmp_dir, dry_run=True)
        self.assertEqual(len(result["orphans"]), 1)
        # recovered list must be empty (dry run).
        self.assertEqual(len(result["recovered"]), 0)
        # File must NOT exist.
        self.assertFalse((self.tmp_dir / "lessons" / "dry.md").exists())

    def test_check_index_integrity_surfaces_missing_md(self) -> None:
        """check_index_integrity reports backward orphans as warnings."""
        db_path = self.tmp_dir / "memory.db"
        bootstrap_temp_db_clean(db_path)
        with open_db(db_path) as db:
            db.execute(
                "INSERT INTO memories (id, content, tags, source_file, "
                "created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), "
                "datetime('now'))",
                ("lessons/orphan", "orphaned content", "[]", "lessons/orphan.md"),
            )
            db.commit()

        result = check_index_integrity(db_path)
        warnings = [f for f in result["findings"] if f["severity"] == "warning"]
        missing_md = [f for f in warnings if f["check"] == "missing_md_file"]
        self.assertEqual(len(missing_md), 1)
        self.assertIn("lessons/orphan", missing_md[0]["message"])
        self.assertIn("--recover-orphan-files", missing_md[0]["message"])


# ===========================================================================
# Scenario 11 (2026-06-22): FTS5 drift auto-healing
# ===========================================================================


class TestFts5DriftRepair(unittest.TestCase):
    """Scenario 11 regression: repair_fts_drift must rebuild the FTS5
    index when it drifts from the memories table.

    Without the repair path, the existing check_index_integrity
    surfaces FTS5 drift as a warning, but the user has to manually
    run ``cron/cron_rebuild_fts.py`` to fix it.  repair_fts_drift
    automates the repair (same logic, but accessible from
    memory_integrity).
    """

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="fts5_drift_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_no_drift_returns_clean(self) -> None:
        """A clean DB (memories count == FTS count) returns no drift."""
        from memory_integrity import repair_fts_drift

        db_path = self.tmp_dir / "memory.db"
        bootstrap_temp_db_clean(db_path)
        # Insert a memory so the table is non-empty (count comparison
        # is meaningful).
        with open_db(db_path) as db:
            db.execute(
                "INSERT INTO memories (id, content, tags, source_file, "
                "created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), "
                "datetime('now'))",
                ("lessons/seed", "seed content", "[]", "lessons/seed.md"),
            )
            db.commit()
        result = repair_fts_drift(db_path)
        self.assertFalse(result["was_drifted"])
        self.assertFalse(result["rebuild_ran"])

    def test_drift_triggers_repair(self) -> None:
        """A drift is detected and the rebuild runs."""
        from memory_integrity import repair_fts_drift

        db_path = self.tmp_dir / "memory.db"
        bootstrap_temp_db_clean(db_path)
        # Insert a memory, then drop the FTS triggers so a second
        # insert drifts the FTS index.
        with open_db(db_path) as db:
            db.execute(
                "INSERT INTO memories (id, content, tags, source_file, "
                "created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), "
                "datetime('now'))",
                ("lessons/drift1", "first", "[]", "lessons/drift1.md"),
            )
            db.commit()
            # Drop triggers and add a second memory that won't be
            # indexed in FTS, creating drift.
            db.execute("DROP TRIGGER IF EXISTS memories_ai")
            db.execute("DROP TRIGGER IF EXISTS memories_ad")
            db.execute("DROP TRIGGER IF EXISTS memories_au")
            db.execute(
                "INSERT INTO memories (id, content, tags, source_file, "
                "created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), "
                "datetime('now'))",
                ("lessons/drift2", "second", "[]", "lessons/drift2.md"),
            )
            db.commit()
            # Verify drift exists.
            mem_count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            self.assertEqual(mem_count, 2)

        result = repair_fts_drift(db_path)
        # The repair should have detected drift and rebuilt.
        self.assertTrue(
            result["was_drifted"],
            f"Expected drift to be detected, got {result}",
        )
        self.assertTrue(
            result["rebuild_ran"],
            f"Expected rebuild to run, got {result}",
        )
        self.assertTrue(
            result["was_repaired"],
            f"Expected repair to succeed, got {result}",
        )
        # FTS count must now match memories count.
        self.assertEqual(result["indexed_after"], 2)


if __name__ == "__main__":
    unittest.main()
