import time
import unittest
import numpy as np
from search.scoring import TemporalAttentionModel

class TestTemporalAttentionModel(unittest.TestCase):
    def test_cold_start_initialization(self):
        """Test that model initializes with zeros when no weights are provided."""
        model = TemporalAttentionModel(weights=None)
        self.assertFalse(model.has_learned_weights)
        self.assertEqual(model.score("note_1"), 0.5)

    def test_initialization_with_learned_weights(self):
        """Test initialization with a 58-weight array."""
        # W_readout (8) + b_readout (1) + W_input (8 * 6 = 48) + b_input (1) = 58 weights
        weights = np.random.uniform(-1, 1, 58)
        model = TemporalAttentionModel(weights=weights)
        self.assertTrue(model.has_learned_weights)
        
        # Check that dimensions are set correctly
        self.assertEqual(model.W_readout.shape, (8,))
        self.assertEqual(model.W_input.shape, (8, 6))

    def test_observe_and_score(self):
        """Test observing events and scoring notes."""
        weights = np.random.uniform(-1, 1, 58)
        model = TemporalAttentionModel(weights=weights)
        
        query_emb_5 = np.array([0.1, -0.2, 0.3, 0.4, -0.5])
        
        # Observe click
        model.observe("note_1", query_emb_5, clicked=True, hours_since_access=0.0)
        score_clicked = model.score("note_1")
        self.assertTrue(0.0 <= score_clicked <= 1.0)
        
        # Observe dismissal
        model.observe("note_2", query_emb_5, dismissed=True, hours_since_access=0.0)
        score_dismissed = model.score("note_2")
        self.assertTrue(0.0 <= score_dismissed <= 1.0)

    def test_decay_factor(self):
        """Test that hours_since_access decays the hidden state."""
        weights = np.ones(58)  # Set weights to ones to get non-zero updates
        model = TemporalAttentionModel(weights=weights)
        query_emb_5 = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
        
        # Immediate access
        model.observe("note_1", query_emb_5, clicked=True, hours_since_access=0.0)
        h_immediate = model._hidden["note_1"].copy()
        
        # Delayed access (decay should reduce hidden state amplitude)
        model.observe("note_2", query_emb_5, clicked=True, hours_since_access=10.0)
        h_delayed = model._hidden["note_2"].copy()
        
        self.assertTrue(np.all(np.abs(h_delayed) < np.abs(h_immediate)))

    def test_pruning(self):
        """Test pruning stale hidden states."""
        model = TemporalAttentionModel(weights=None)
        query_emb_5 = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
        
        model.observe("note_1", query_emb_5)
        self.assertIn("note_1", model._hidden)
        
        # Mock last access timestamp to be older than 30 days (720 hours)
        model._last_access_ts["note_1"] = time.time() - (750 * 3600)
        
        pruned_count = model.prune(older_than_hours=720)
        self.assertEqual(pruned_count, 1)
        self.assertNotIn("note_1", model._hidden)

    def test_serialization_roundtrip(self):
        """Test serialization to config string and back."""
        weights = np.random.uniform(-1, 1, 58)
        model1 = TemporalAttentionModel(weights=weights)
        
        config_str = model1.to_config_str()
        self.assertIsInstance(config_str, str)
        self.assertEqual(len(config_str.split(",")), 58)
        
        # Recreate from config string
        parts = [float(x) for x in config_str.split(",")]
        model2 = TemporalAttentionModel(weights=np.array(parts))
        
        np.testing.assert_array_almost_equal(model1.W_readout, model2.W_readout, decimal=5)
        np.testing.assert_array_almost_equal(model1.W_input, model2.W_input, decimal=5)
        self.assertAlmostEqual(model1.b_readout, model2.b_readout, places=5)
        self.assertAlmostEqual(model1.b_input, model2.b_input, places=5)
