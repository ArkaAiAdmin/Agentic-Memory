#!/usr/bin/env python3
"""Unit tests for tier_migration.assign_tier.

Covers M25 fix: the function was previously inconsistent with the
file-system tier model at the top of tier_migration.py. The docstring
now documents that there are TWO distinct tier systems. This test pins
the DB-tier rules.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

import tier_migration


class TestAssignTier(unittest.TestCase):
    def test_pinned_is_hot(self):
        # Pinned always hot regardless of age or importance.
        old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        tier = tier_migration.assign_tier(
            importance=1, pinned=True, last_accessed=old, created_at=old
        )
        self.assertEqual(tier, "hot")

    def test_high_importance_is_hot(self):
        old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        tier = tier_migration.assign_tier(
            importance=5, pinned=False, last_accessed=old, created_at=old
        )
        self.assertEqual(tier, "hot")

    def test_recent_access_is_hot(self):
        # Accessed today = within 7-day window.
        today = datetime.now(timezone.utc).isoformat()
        tier = tier_migration.assign_tier(
            importance=1, pinned=False, last_accessed=today, created_at=today
        )
        self.assertEqual(tier, "hot")

    def test_mid_importance_is_warm(self):
        # Importance >= 3 OR accessed within 30 days = warm.
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        tier = tier_migration.assign_tier(
            importance=3, pinned=False, last_accessed=old, created_at=old
        )
        self.assertEqual(tier, "warm")

    def test_old_low_importance_is_cold(self):
        # Old + low importance = cold.
        very_old = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
        tier = tier_migration.assign_tier(
            importance=1, pinned=False, last_accessed=very_old, created_at=very_old
        )
        self.assertEqual(tier, "cold")

    def test_no_dates_is_cold(self):
        tier = tier_migration.assign_tier(
            importance=1, pinned=False, last_accessed=None, created_at=None
        )
        self.assertEqual(tier, "cold")


if __name__ == "__main__":
    unittest.main()
