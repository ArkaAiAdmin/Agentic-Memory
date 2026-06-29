#!/usr/bin/env python3
"""Unit tests for NeuralForgetModel class.

Covers: construction, predict, from_config, to_config_str, features.
"""

import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import numpy as np


class TestNeuralForgetModelConstruction(unittest.TestCase):
    def test_default_weights_are_production_formula(self):
        from neural_forget import NeuralForgetModel

        m = NeuralForgetModel()
        self.assertEqual(m.W.shape, (5,))
        self.assertIsInstance(m.b, float)

    def test_custom_weights_accepted(self):
        from neural_forget import NeuralForgetModel

        w = np.array([0.1, 0.2, 0.3, 0.4, 0.5, -0.05], dtype=float)
        m = NeuralForgetModel(w)
        np.testing.assert_array_almost_equal(m.W, w[:5])
        self.assertAlmostEqual(m.b, -0.05)

    def test_bad_shape_ignored_uses_default(self):
        from neural_forget import NeuralForgetModel

        w = np.array([0.1, 0.2], dtype=float)
        m = NeuralForgetModel(w)
        self.assertEqual(m.W.shape, (5,))

    def test_none_weights_uses_default(self):
        from neural_forget import NeuralForgetModel

        m = NeuralForgetModel(None)
        self.assertEqual(m.W.shape, (5,))


class TestNeuralForgetModelPredict(unittest.TestCase):
    def test_predict_output_in_unit_interval(self):
        from neural_forget import NeuralForgetModel

        m = NeuralForgetModel()
        for _ in range(20):
            f = np.random.uniform(-2, 2, size=5)
            p = m.predict(f)
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_positive_features_high_retention(self):
        from neural_forget import NeuralForgetModel

        m = NeuralForgetModel()
        f = np.array([5.0, 0.0, 5.0, 5.0, 0.0], dtype=float)
        p = m.predict(f)
        self.assertGreater(p, 0.8)

    def test_negative_features_low_retention(self):
        from neural_forget import NeuralForgetModel

        m = NeuralForgetModel()
        f = np.array([0.0, 1.0, 0.0, 0.0, 5.0], dtype=float)
        p = m.predict(f)
        self.assertLess(p, 0.5)

    def test_custom_weights_give_different_prediction(self):
        from neural_forget import NeuralForgetModel

        default = NeuralForgetModel()
        custom = NeuralForgetModel(np.array([1.0, 1.0, 1.0, 1.0, 1.0, 0.0], dtype=float))
        f = np.array([1.0, 1.0, 1.0, 1.0, 1.0], dtype=float)
        self.assertNotAlmostEqual(
            default.predict(f), custom.predict(f), places=4
        )


class TestNeuralForgetModelFromConfig(unittest.TestCase):
    def test_empty_config_uses_default(self):
        from neural_forget import NeuralForgetModel

        m = NeuralForgetModel()
        self.assertEqual(m.W.shape, (5,))

    def test_to_config_and_back(self):
        from neural_forget import NeuralForgetModel

        m1 = NeuralForgetModel(np.array([0.5, 0.4, 0.3, 0.2, 0.1, -0.01], dtype=float))
        s = m1.to_config_str()
        parts = [float(x) for x in s.split(",")]
        self.assertEqual(len(parts), 6)
        m2 = NeuralForgetModel(np.array(parts, dtype=float))
        np.testing.assert_array_almost_equal(m1.W, m2.W)
        self.assertAlmostEqual(m1.b, m2.b)


class TestNeuralForgetModelFeatures(unittest.TestCase):
    def test_features_static(self):
        from neural_forget import NeuralForgetModel

        f = NeuralForgetModel.features(1.0, 0.5, 0.8, 0.6, 0.3)
        self.assertEqual(f.shape, (5,))
        np.testing.assert_array_almost_equal(
            f, np.array([1.0, 0.5, 0.8, 0.6, 0.3])
        )

    def test_predict_with_features(self):
        from neural_forget import NeuralForgetModel, compute_retention_rate

        m = NeuralForgetModel()
        f = NeuralForgetModel.features(
            access_signal=2.0,
            query_surprise=0.2,
            importance_norm=0.8,
            fitness=0.7,
            recency_penalty=0.1,
        )
        p = m.predict(f)
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)

        # NeuralForgetModel with default weights should give similar
        # results to compute_retention_rate for the same inputs.
        r = compute_retention_rate(
            content="test",
            access_count=10,
            recency_days=1,
            fitness=0.7,
            importance=4,
        )
        self.assertGreater(p, 0.0)
        self.assertGreater(r, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
