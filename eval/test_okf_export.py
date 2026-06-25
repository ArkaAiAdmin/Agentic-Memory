#!/usr/bin/env python3
"""Unit tests for okf_export.py.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_okf_export.py
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from okf_export import okf_export, _memory_to_okf, FRONTMATTER_KEYS


def _make_db(path: Path) -> sqlite3.Connection:
    from _fixtures import bootstrap_temp_db_clean

    bootstrap_temp_db_clean(path)
    conn = sqlite3.connect(str(path))
    return conn


class TestMemoryToOKF(unittest.TestCase):
    def test_basic_memory(self):
        row = {
            "id": "lessons/hello-world",
            "content": "Hello content",
            "tags": '["python", "test"]',
            "pinned": 1,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-02T00:00:00",
            "observed_at": "2026-01-01T12:00:00",
            "valid_from": "",
            "valid_to": "",
            "superseded_by": "",
            "metadata": "{}",
        }
        result = _memory_to_okf(row)
        self.assertIn("---", result)
        self.assertIn("created: 2026-01-01T00:00:00", result)
        self.assertIn("tags: [python, test]", result)
        self.assertIn("pinned: true", result)
        self.assertIn("Hello content", result)
        self.assertIn("Hello World", result)

    def test_unpinned_memory(self):
        row = {
            "id": "sessions/test-session",
            "content": "session content",
            "tags": "[]",
            "pinned": 0,
            "created_at": "",
            "updated_at": "",
            "observed_at": "",
            "valid_from": "",
            "valid_to": "",
            "superseded_by": "",
            "metadata": "{}",
        }
        result = _memory_to_okf(row)
        self.assertIn("pinned: false", result)

    def test_memory_with_metadata_type(self):
        row = {
            "id": "decisions/use-qwen3",
            "content": "decision content",
            "tags": "[]",
            "pinned": 0,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "",
            "observed_at": "",
            "valid_from": "",
            "valid_to": "",
            "superseded_by": "",
            "metadata": '{"type": "decision", "resource": "embedding"}',
        }
        result = _memory_to_okf(row)
        self.assertIn("type: decision", result)
        self.assertIn("resource: embedding", result)

    def test_valid_from_to(self):
        row = {
            "id": "preferences/valid-period",
            "content": "valid content",
            "tags": "[]",
            "pinned": 0,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "",
            "observed_at": "",
            "valid_from": "2026-01-01",
            "valid_to": "2026-12-31",
            "superseded_by": "",
            "metadata": "{}",
        }
        result = _memory_to_okf(row)
        self.assertIn("valid_from: 2026-01-01", result)
        self.assertIn("valid_to: 2026-12-31", result)

    def test_superseded_by(self):
        row = {
            "id": "lessons/superseded",
            "content": "old content",
            "tags": "[]",
            "pinned": 0,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "",
            "observed_at": "",
            "valid_from": "",
            "valid_to": "",
            "superseded_by": "lessons/newer",
            "metadata": "{}",
        }
        result = _memory_to_okf(row)
        self.assertIn("superseded_by: lessons/newer", result)

    def test_tags_as_list(self):
        row = {
            "id": "sessions/tagged",
            "content": "tagged content",
            "tags": '["a", "b"]',
            "pinned": 0,
            "created_at": "",
            "updated_at": "",
            "observed_at": "",
            "valid_from": "",
            "valid_to": "",
            "superseded_by": "",
            "metadata": "{}",
        }
        result = _memory_to_okf(row)
        self.assertIn("[a, b]", result)


class TestOKFExport(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        self.outdir = self.tmpdir / "export"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed(self, conn: sqlite3.Connection):
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES ('lessons/test-note', 'content', 'test.md', '[\"python\"]', "
            "datetime('now'), datetime('now'), datetime('now'))"
        )
        conn.commit()

    def test_export_db_not_found(self):
        result = okf_export("/nonexistent/db", self.outdir)
        self.assertEqual(result["exported"], 0)
        self.assertIn("error", result)

    def test_export_success(self):
        conn = _make_db(self.db_path)
        self._seed(conn)
        conn.close()
        result = okf_export(self.db_path, self.outdir)
        self.assertEqual(result["exported"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertTrue(result["index_path"].endswith("index.md"))
        md_file = self.outdir / "lessons" / "test-note.md"
        self.assertTrue(md_file.exists())
        index_file = self.outdir / "index.md"
        self.assertTrue(index_file.exists())

    def test_export_skipped_existing(self):
        conn = _make_db(self.db_path)
        self._seed(conn)
        conn.close()
        self.outdir.mkdir(parents=True)
        already = self.outdir / "lessons"
        already.mkdir(parents=True, exist_ok=True)
        (already / "test-note.md").write_text("existing")
        result = okf_export(self.db_path, self.outdir, overwrite=False)
        self.assertEqual(result["exported"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_export_overwrite(self):
        conn = _make_db(self.db_path)
        self._seed(conn)
        conn.close()
        self.outdir.mkdir(parents=True)
        already = self.outdir / "lessons"
        already.mkdir(parents=True, exist_ok=True)
        (already / "test-note.md").write_text("existing")
        result = okf_export(self.db_path, self.outdir, overwrite=True)
        self.assertEqual(result["exported"], 1)
        self.assertEqual(result["skipped"], 0)

    def test_export_includes_index(self):
        conn = _make_db(self.db_path)
        self._seed(conn)
        conn.close()
        result = okf_export(self.db_path, self.outdir)
        index_path = Path(result["index_path"])
        self.assertTrue(index_path.exists())
        content = index_path.read_text()
        self.assertIn("OKF Memory Index", content)
        self.assertIn("test-note", content)

    def test_export_skips_deleted_by_default(self):
        conn = _make_db(self.db_path)
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, deleted_at, created_at, updated_at, observed_at) "
            "VALUES ('lessons/deleted', 'gone', 'del.md', '[]', "
            "datetime('now'), datetime('now'), datetime('now'), datetime('now'))"
        )
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES ('lessons/alive', 'here', 'alive.md', '[]', "
            "datetime('now'), datetime('now'), datetime('now'))"
        )
        conn.commit()
        conn.close()
        result = okf_export(self.db_path, self.outdir)
        self.assertEqual(result["exported"], 1)

    def test_export_includes_deleted_with_flag(self):
        conn = _make_db(self.db_path)
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, deleted_at, created_at, updated_at, observed_at) "
            "VALUES ('lessons/deleted', 'gone', 'del.md', '[]', "
            "datetime('now'), datetime('now'), datetime('now'), datetime('now'))"
        )
        conn.commit()
        conn.close()
        result = okf_export(self.db_path, self.outdir, include_deleted=True)
        self.assertEqual(result["exported"], 1)

    def test_export_empty_db(self):
        conn = _make_db(self.db_path)
        conn.close()
        result = okf_export(self.db_path, self.outdir)
        self.assertEqual(result["exported"], 0)
        self.assertEqual(result["errors"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
