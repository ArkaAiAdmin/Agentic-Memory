#!/usr/bin/env python3
"""Unit tests for okf_import.py.

Tests the parser, frontmatter handling, and error paths.
The actual save_memory path requires a live DB — tested in e2e tests.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_okf_import.py
"""

import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from okf_import import okf_import, _strip_fm_keys, _coerce_tag


class TestCoerceTag(unittest.TestCase):
    def test_strips_quotes(self):
        self.assertEqual(_coerce_tag('"hello"'), "hello")
        self.assertEqual(_coerce_tag("'hello'"), "hello")

    def test_plain_string(self):
        self.assertEqual(_coerce_tag("hello"), "hello")


class TestStripFMKeys(unittest.TestCase):
    def test_passthrough(self):
        self.assertEqual(_strip_fm_keys("body"), "body")


class TestOKFImportErrors(unittest.TestCase):
    def test_nonexistent_dir(self):
        result = okf_import("/nonexistent/okf_dir")
        self.assertEqual(result["imported"], 0)
        self.assertIn("error", result)

    def test_empty_dir(self):
        tmpdir = Path(tempfile.mkdtemp())
        result = okf_import(tmpdir)
        self.assertEqual(result["imported"], 0)


class TestOKFImportDryRun(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.source = self.tmpdir / "okf_source"
        self.cat_dir = self.source / "lessons"
        self.cat_dir.mkdir(parents=True)
        (self.cat_dir / "test-note.md").write_text(
            "---\ntitle: Test Note\ntags: [python, test]\npinned: true\n---\n\n# Test Note\n\nHello world\n"
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dry_run_counts(self):
        result = okf_import(self.source, dry_run=True)
        self.assertEqual(result["imported"], 1)
        self.assertTrue(result["dry_run"])

    def test_import_without_dry_run_fails_no_db(self):
        import os

        nonexistent = self.tmpdir / "no_such_db" / "memory.db"
        old_env = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(nonexistent)
        try:
            result = okf_import(self.source, dry_run=False)
            self.assertGreaterEqual(result["errors"], 1)
        finally:
            if old_env is not None:
                os.environ["MEMORY_DB_PATH"] = old_env
            else:
                os.environ.pop("MEMORY_DB_PATH", None)


class TestOKFImportMultipleFiles(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.source = self.tmpdir / "okf_multi"
        lessons = self.source / "lessons"
        sessions = self.source / "sessions"
        lessons.mkdir(parents=True)
        sessions.mkdir(parents=True)
        (lessons / "note-a.md").write_text("---\ntitle: Note A\n---\nA content\n")
        (sessions / "session-1.md").write_text(
            "---\ntitle: Session 1\n---\nSession content\n"
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_finds_all_md_files(self):
        result = okf_import(self.source, dry_run=True)
        self.assertEqual(result["imported"], 2)

    def test_skips_index_md(self):
        (self.source / "index.md").write_text("# Index\n")
        result = okf_import(self.source, dry_run=True)
        self.assertEqual(result["imported"], 2)


class TestOKFImportBadFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.cat_dir = self.tmpdir / "lessons"
        self.cat_dir.mkdir(parents=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_non_utf8_file_counts_as_error(self):
        (self.cat_dir / "bad.md").write_bytes(b"\xff\xfe\x00\x01")
        result = okf_import(self.tmpdir, dry_run=True)
        self.assertGreaterEqual(result["errors"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
