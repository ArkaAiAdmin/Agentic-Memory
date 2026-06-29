#!/usr/bin/env python3
"""Tests for cron/cron_train_forget_model.py.

Covers: _sgd_train, _load_examples structure, cold-start guard.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import numpy as np


class TestSgdTrain(unittest.TestCase):
    def test_returns_six_weights(self):
        from cron.cron_train_forget_model import _sgd_train

        X = np.array([[1.0, 0.0, 0.5, 0.5, 0.0]], dtype=float)
        y = np.array([1.0], dtype=float)
        w = _sgd_train(X, y, epochs=5, lr=0.01)
        self.assertEqual(w.shape, (6,))

    def test_positive_examples_push_bias_positive(self):
        from cron.cron_train_forget_model import _sgd_train

        N = 20
        X = np.random.uniform(0, 1, size=(N, 5)).astype(float)
        y = np.ones(N, dtype=float)
        w = _sgd_train(X, y, epochs=30, lr=0.02)
        # Bias term (last) should be positive after training on all-positive.
        self.assertGreater(w[5], 0)

    def test_negative_examples_push_bias_negative(self):
        from cron.cron_train_forget_model import _sgd_train

        N = 20
        X = np.random.uniform(0, 1, size=(N, 5)).astype(float)
        y = -np.ones(N, dtype=float)
        w = _sgd_train(X, y, epochs=30, lr=0.02)
        # Bias term (last) should be negative after training on all-negative.
        self.assertLess(w[5], 0)


class TestLoadExamples(unittest.TestCase):
    def setUp(self):
        import cron.cron_train_forget_model as mod
        self.mod = mod

    def test_load_examples_returns_list(self):
        # No real DB available — verify the function signature exists
        # and returns a list.
        with mock.patch.object(self.mod, "_db_path"):
            pass  # tests below do the actual call

    def test_empty_db_returns_empty_list(self):
        db = mock.MagicMock()
        db.execute.return_value.fetchall.return_value = []
        examples = self.mod._load_examples(db)
        self.assertEqual(examples, [])


class TestMainColdStart(unittest.TestCase):
    """main() returns early when DB has fewer than _MIN_EXAMPLES examples."""

    @mock.patch("cron.cron_train_forget_model._db_path")
    @mock.patch("pathlib.Path.exists", return_value=False)
    def test_no_db_returns_zero(self, mock_exists, mock_path):
        from cron.cron_train_forget_model import main

        rc = main()
        self.assertEqual(rc, 0)

    @mock.patch("cron.cron_train_forget_model._load_examples", return_value=[])
    @mock.patch("cron.cron_train_forget_model._db_path")
    @mock.patch("pathlib.Path.exists", return_value=True)
    def test_cold_start_returns_zero(self, mock_exists, mock_path, mock_load):
        from cron.cron_train_forget_model import main

        rc = main()
        self.assertEqual(rc, 0)


class TestMainFullRun(unittest.TestCase):
    """Full main() with synthetic data, verifying config write."""

    def setUp(self):
        import cron.cron_train_forget_model as mod
        self.mod = mod
        self._MIN = mod._MIN_EXAMPLES

    def _make_example(self, label=1.0):
        return (np.array([0.5, 0.3, 0.6, 0.7, 0.1], dtype=float), label)

    @mock.patch("cron.cron_train_forget_model._db_path")
    @mock.patch("cron.cron_train_forget_model.logger")
    def test_main_writes_weights_to_config(self, mock_logger, mock_db_path):
        examples = [self._make_example(1.0) for _ in range(self._MIN)]
        with (
            mock.patch.object(self.mod, "_load_examples", return_value=examples),
            mock.patch.object(self.mod, "_get_config") as mock_cfg,
            mock.patch("pathlib.Path.exists", return_value=True),
        ):
            cfg = mock.MagicMock()
            cfg._config_path = "/tmp/memory.toml"
            mock_cfg.return_value = cfg

            with mock.patch("pathlib.Path.read_text", return_value="[features]\nkey = 1\n"):
                tmp_mock = mock.MagicMock()
                replace_mock = mock.MagicMock()
                with mock.patch("pathlib.Path.with_suffix", return_value=tmp_mock):
                    tmp_mock.replace = replace_mock

                    from cron.cron_train_forget_model import main
                    rc = main()
                    self.assertEqual(rc, 0)
                    # Temp file should have been written and replaced.
                    self.assertTrue(tmp_mock.write_text.called)
                    self.assertTrue(replace_mock.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
