"""Tests for self_directed.py (was 10% coverage — second-highest gap).

Focus on the testable pure functions:
- _assign_tier: tier bucket based on importance
- _parse_ts_to_epoch: timestamp parsing
- tier_stats: distribution query
- compute_importance: scoring with mocked access patterns
"""

import datetime
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))

from _fixtures import bootstrap_temp_db_clean


class TestAssignTier(unittest.TestCase):
    """_assign_tier maps an importance score to a tier bucket."""

    def test_high_importance_is_hot(self):
        from self_directed import _assign_tier

        self.assertEqual(_assign_tier(0.95), "hot")
        self.assertEqual(_assign_tier(0.71), "hot")
        # Exact threshold
        self.assertEqual(_assign_tier(0.7), "hot")

    def test_mid_importance_is_warm(self):
        from self_directed import _assign_tier

        self.assertEqual(_assign_tier(0.5), "warm")
        self.assertEqual(_assign_tier(0.31), "warm")
        # Exact threshold
        self.assertEqual(_assign_tier(0.3), "warm")

    def test_low_importance_is_cold(self):
        from self_directed import _assign_tier

        self.assertEqual(_assign_tier(0.29), "cold")
        self.assertEqual(_assign_tier(0.0), "cold")
        self.assertEqual(_assign_tier(-0.5), "cold")  # negative is also cold


class TestParseTsToEpoch(unittest.TestCase):
    """_parse_ts_to_epoch handles str/int/float/None inputs."""

    def test_none_returns_fallback(self):
        from self_directed import _parse_ts_to_epoch

        self.assertEqual(_parse_ts_to_epoch(None, 100.0), 100.0)

    def test_int_returns_float(self):
        from self_directed import _parse_ts_to_epoch

        self.assertEqual(_parse_ts_to_epoch(1234567890, 0.0), 1234567890.0)

    def test_float_returns_float(self):
        from self_directed import _parse_ts_to_epoch

        self.assertEqual(_parse_ts_to_epoch(1234.5678, 0.0), 1234.5678)

    def test_iso_string_parsed(self):
        from self_directed import _parse_ts_to_epoch

        # 2026-01-01T00:00:00Z
        result = _parse_ts_to_epoch("2026-01-01T00:00:00Z", 0.0)
        self.assertGreater(result, 0)
        # Should match the expected epoch (Jan 1 2026 UTC)
        expected = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        ).timestamp()
        self.assertAlmostEqual(result, expected, places=0)

    def test_numeric_string_parsed(self):
        from self_directed import _parse_ts_to_epoch

        self.assertEqual(_parse_ts_to_epoch("1234.5", 0.0), 1234.5)

    def test_unparseable_string_returns_fallback(self):
        from self_directed import _parse_ts_to_epoch

        self.assertEqual(_parse_ts_to_epoch("not a date", 999.0), 999.0)


class TestTierStats(unittest.TestCase):
    """tier_stats returns distribution by tier."""

    def test_empty_db_returns_empty_tiers(self):
        from self_directed import tier_stats

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "memory.db"
            bootstrap_temp_db_clean(db_path)
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("DELETE FROM memories")
                conn.commit()
                result = tier_stats(conn)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["pinned"], 0)
        self.assertEqual(result["tiers"], {})

    def test_db_with_mixed_tiers(self):
        from self_directed import tier_stats

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "memory.db"
            bootstrap_temp_db_clean(db_path)
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("DELETE FROM memories")
                # Insert notes with different tiers and importance
                notes = [
                    ("lessons/hot1", "hot", 0.9, 0),
                    ("lessons/warm1", "warm", 0.5, 0),
                    ("lessons/cold1", "cold", 0.1, 1),  # pinned
                    ("lessons/cold2", "cold", 0.05, 0),
                ]
                for nid, tier, importance, pinned in notes:
                    conn.execute(
                        """INSERT INTO memories
                           (id, content, source_file, version_vector, logical_clock,
                            created_at, updated_at, observed_at,
                            tier, importance_score, pinned)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            nid,
                            "content",
                            f"{nid}.md",
                            json.dumps({"a": 1}),
                            1,
                            "2026-01-01T00:00:00",
                            "2026-01-01T00:00:00",
                            "2026-01-01T00:00:00",
                            tier,
                            importance,
                            pinned,
                        ),
                    )
                conn.commit()
                result = tier_stats(conn)
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["pinned"], 1)
        self.assertIn("hot", result["tiers"])
        self.assertEqual(result["tiers"]["hot"]["count"], 1)
        self.assertIn("warm", result["tiers"])
        self.assertEqual(result["tiers"]["warm"]["count"], 1)
        self.assertIn("cold", result["tiers"])
        self.assertEqual(result["tiers"]["cold"]["count"], 2)


class TestArchiveLowImportance(unittest.TestCase):
    """archive_low_importance moves cold+old notes to archived state."""

    def test_empty_db_no_archive(self):
        from self_directed import archive_low_importance

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "memory.db"
            bootstrap_temp_db_clean(db_path)
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("DELETE FROM memories")
                conn.commit()
                result = archive_low_importance(conn, dry_run=True)
        self.assertEqual(result.get("archived", 0), 0)


if __name__ == "__main__":
    unittest.main()
