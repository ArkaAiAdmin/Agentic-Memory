#!/usr/bin/env python3
"""Unit tests for embedding_recompute.py.

Tests model config detection, comparison logic, and error paths.
The actual subprocess rebuild is only triggered when model changes.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_embedding_recompute.py
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 2026-06-29 fix: resolve from the test file location, not the user's home
# dir. On CI runners the ~/.config/agentic-memory install dir does not exist.
INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))


class TestEmbeddingRecomputeConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.orig_vec_meta = None
        import infra.embedding_recompute as embedding_recompute

        if hasattr(embedding_recompute, "VEC_META_FILE"):
            self.orig_vec_meta = str(embedding_recompute.VEC_META_FILE)
            embedding_recompute.VEC_META_FILE = self.tmpdir / "vec_index.meta.json"

    def tearDown(self):
        import infra.embedding_recompute as embedding_recompute

        if self.orig_vec_meta:
            embedding_recompute.VEC_META_FILE = Path(self.orig_vec_meta)
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_current_model_config_returns_dict(self):
        from infra.embedding_recompute import get_current_model_config

        config = get_current_model_config()
        self.assertIsInstance(config, dict)
        self.assertIn("model", config)
        self.assertIn("api_base", config)
        self.assertIn("dimensions", config)

    def test_get_stored_config_no_file(self):
        from infra.embedding_recompute import get_stored_model_config

        config = get_stored_model_config()
        self.assertEqual(config, {})

    def test_get_stored_config_with_file(self):
        from infra.embedding_recompute import get_stored_model_config, save_model_config

        expected = {
            "model": "BAAI/bge-base-en-v1.5",
            "api_base": "local",
            "dimensions": 768,
        }
        save_model_config(expected)
        config = get_stored_model_config()
        self.assertEqual(config, expected)

    def test_get_stored_config_bad_json(self):
        import infra.embedding_recompute as embedding_recompute

        embedding_recompute.VEC_META_FILE.write_text("not json")
        config = embedding_recompute.get_stored_model_config()
        self.assertEqual(config, {})

    def test_check_and_rebuild_no_change(self):
        from infra.embedding_recompute import check_and_rebuild, save_model_config

        save_model_config(
            {
                "model": "BAAI/bge-base-en-v1.5",
                "api_base": "local",
                "dimensions": 768,
            }
        )
        result = check_and_rebuild()
        self.assertFalse(result["changed"])
        self.assertFalse(result["rebuilt"])

    @mock.patch("subprocess.run")
    def test_check_and_rebuild_force(self, mock_run):
        from infra.embedding_recompute import check_and_rebuild, save_model_config

        mock_run.return_value = mock.MagicMock(returncode=0)
        save_model_config(
            {
                "model": "BAAI/bge-base-en-v1.5",
                "api_base": "local",
                "dimensions": 768,
            }
        )
        result = check_and_rebuild(force=True)
        self.assertTrue(result["changed"])
        mock_run.assert_called_once()

    def test_check_and_rebuild_dry_run(self):
        from infra.embedding_recompute import check_and_rebuild, save_model_config

        save_model_config(
            {"model": "old-model", "api_base": "remote", "dimensions": 128}
        )
        result = check_and_rebuild(dry_run=True)
        self.assertTrue(result["changed"])
        self.assertFalse(result["rebuilt"])
        self.assertIn("dry run", result["details"])

    def test_save_model_config_writes_file(self):
        from infra.embedding_recompute import (
            save_model_config,
            VEC_META_FILE,
            get_stored_model_config,
        )

        config = {"model": "test-model", "api_base": "test", "dimensions": 64}
        save_model_config(config)
        self.assertTrue(VEC_META_FILE.exists())
        stored = get_stored_model_config()
        self.assertEqual(stored, config)


if __name__ == "__main__":
    unittest.main(verbosity=2)
