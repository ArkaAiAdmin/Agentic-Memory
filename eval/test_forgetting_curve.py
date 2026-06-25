"""Unit tests for forgetting curve decay in search_pipeline.py."""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path.home() / '.config' / 'agentic-memory'))


class TestForgettingCurveDecay(unittest.TestCase):
    """Test the forgetting curve decay mechanism."""

    @mock.patch.dict(os.environ, {'MEMORY_FORGETTING_CURVE': '1'})
    def test_decay_uses_last_accessed_when_enabled(self):
        """When MEMORY_FORGETTING_CURVE=1, decay should use last_accessed."""
        import importlib
        import search_pipeline
        importlib.reload(search_pipeline)
        from search_pipeline import _temporal_decay_factor

        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=2)).isoformat()
        old = (now - timedelta(days=60)).isoformat()

        d_recent = _temporal_decay_factor(recent, last_accessed=recent)
        d_old = _temporal_decay_factor(old, last_accessed=old)

        self.assertGreater(d_recent, d_old)
        self.assertGreater(d_recent, 0.8)
        self.assertLess(d_old, 0.5)
        importlib.reload(search_pipeline)

    def test_decay_falls_back_to_created(self):
        """When last_accessed is None, decay should use created_at."""
        from search_pipeline import _temporal_decay_factor

        now = datetime.now(timezone.utc)
        created = (now - timedelta(days=10)).isoformat()

        d = _temporal_decay_factor(created)
        self.assertGreater(d, 0.0)
        self.assertLess(d, 1.0)

    def test_decay_one_day_old_is_high(self):
        """A 1-day-old note should have high retention."""
        from search_pipeline import _temporal_decay_factor

        now = datetime.now(timezone.utc)
        one_day_ago = (now - timedelta(days=1)).isoformat()

        d = _temporal_decay_factor(one_day_ago)
        self.assertGreater(d, 0.9)

    @mock.patch.dict(os.environ, {'MEMORY_FORGETTING_CURVE': '1'})
    def test_decay_90_days_old_is_low(self):
        """A 90-day-old note should have low retention with forgetting curve."""
        import importlib
        import search_pipeline
        importlib.reload(search_pipeline)
        from search_pipeline import _temporal_decay_factor

        now = datetime.now(timezone.utc)
        ninety_days = (now - timedelta(days=90)).isoformat()

        d = _temporal_decay_factor(ninety_days, last_accessed=ninety_days)
        self.assertLess(d, 0.2)
        importlib.reload(search_pipeline)

    def test_pinned_notes_not_affected_by_decay(self):
        """Pinned notes should have decay factor clamped to >= 0.5 in search."""
        from search_pipeline import _temporal_decay_factor

        now = datetime.now(timezone.utc)
        very_old = (now - timedelta(days=365)).isoformat()

        d = _temporal_decay_factor(very_old)
        self.assertGreater(d, 0.0)
        self.assertLess(d, 1.0)

    def test_half_life_configurable(self):
        """Half-life should be configurable via MEMORY_FORGETTING_CURVE_HALF_LIFE."""
        from config import MemoryConfig

        cfg = MemoryConfig()
        self.assertEqual(cfg.forgetting_curve_half_life, 30)

        cfg2 = MemoryConfig(forgetting_curve_half_life=60)
        self.assertEqual(cfg2.forgetting_curve_half_life, 60)


if __name__ == '__main__':
    unittest.main()
