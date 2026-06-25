"""Tests for memory_maintenance dispatch with **kwargs (Phase 3 refactor).

The signature changed from 40 explicit params to `**kwargs`. These
tests cover:
  * Unknown op returns INVALID_PARAMS error
  * Unknown op error includes helpful diagnostic of passed kwargs
  * Known no-param op dispatches correctly
  * Kwargs are forwarded to the handler
  * Kwargs not consumed by the handler are silently dropped
  * Non-string return values are coerced to str
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


class TestMemoryMaintenanceUnknownOp(unittest.TestCase):
    def test_unknown_op_returns_error(self):
        from mcp_maintenance import memory_maintenance
        from mcp_common import ErrorCode

        result = memory_maintenance("totally_made_up_op_xyz")
        self.assertIn(ErrorCode.INVALID_PARAMS.value, result)
        self.assertIn("totally_made_up_op_xyz", result)

    def test_unknown_op_with_kwargs_shows_them_in_error(self):
        from mcp_maintenance import memory_maintenance
        from mcp_common import ErrorCode

        result = memory_maintenance("nonexistent_op", dry_run=True, threshold=0.5)
        self.assertIn(ErrorCode.INVALID_PARAMS.value, result)
        self.assertIn("dry_run", result)
        self.assertIn("threshold", result)

    def test_dash_normalized_to_underscore(self):
        from mcp_maintenance import memory_maintenance
        from mcp_common import ErrorCode

        result = memory_maintenance("totally-made-up-op-xyz")
        self.assertIn(ErrorCode.INVALID_PARAMS.value, result)
        self.assertIn("totally_made_up_op_xyz", result)


class TestMemoryMaintenanceDispatch(unittest.TestCase):
    def test_known_op_dispatches(self):
        from mcp_maintenance import memory_maintenance
        from mcp_maintenance import MaintenanceOp

        with patch("mcp_maintenance_ops._get_handlers") as mock_h:
            mock_h.return_value = {
                MaintenanceOp.TIER_STATS: lambda **_: "TIER_STATS_OK",
            }
            result = memory_maintenance("tier_stats")
        self.assertEqual(result, "TIER_STATS_OK")

    def test_kwargs_forwarded_to_handler(self):
        from mcp_maintenance import memory_maintenance
        from mcp_maintenance import MaintenanceOp

        captured = {}

        def capture_handler(*, dry_run, **_):
            captured["dry_run"] = dry_run
            return "captured"

        with patch("mcp_maintenance_ops._get_handlers") as mock_h:
            mock_h.return_value = {
                MaintenanceOp.HEARTBEAT: capture_handler,
            }
            result = memory_maintenance("heartbeat", dry_run=True)
        self.assertEqual(result, "captured")
        self.assertEqual(captured["dry_run"], True)

    def test_ignored_kwargs_dont_error(self):
        from mcp_maintenance import memory_maintenance
        from mcp_maintenance import MaintenanceOp

        with patch("mcp_maintenance_ops._get_handlers") as mock_h:
            mock_h.return_value = {
                MaintenanceOp.TIER_STATS: lambda **_: "OK",
            }
            result = memory_maintenance(
                "tier_stats",
                bogus_param_1="x",
                bogus_param_2=42,
                bogus_param_3=[1, 2, 3],
            )
        self.assertEqual(result, "OK")

    def test_non_string_return_coerced_to_str(self):
        from mcp_maintenance import memory_maintenance
        from mcp_maintenance import MaintenanceOp

        with patch("mcp_maintenance_ops._get_handlers") as mock_h:
            mock_h.return_value = {
                MaintenanceOp.TIER_STATS: lambda **_: 12345,
            }
            result = memory_maintenance("tier_stats")
        self.assertEqual(result, "12345")

    def test_dict_return_coerced_to_str(self):
        from mcp_maintenance import memory_maintenance
        from mcp_maintenance import MaintenanceOp

        with patch("mcp_maintenance_ops._get_handlers") as mock_h:
            mock_h.return_value = {
                MaintenanceOp.TIER_STATS: lambda **_: {"k": "v"},
            }
            result = memory_maintenance("tier_stats")
        self.assertEqual(result, str({"k": "v"}))

    def test_handler_receives_all_kwargs(self):
        from mcp_maintenance import memory_maintenance
        from mcp_maintenance import MaintenanceOp

        captured = {}

        def capture(*, dry_run, threshold, **_):
            captured["dry_run"] = dry_run
            captured["threshold"] = threshold
            return "ok"

        with patch("mcp_maintenance_ops._get_handlers") as mock_h:
            mock_h.return_value = {
                MaintenanceOp.DUPLICATES: capture,
            }
            result = memory_maintenance("duplicates", dry_run=True, threshold=0.9)
        self.assertEqual(result, "ok")
        self.assertEqual(captured["dry_run"], True)
        self.assertEqual(captured["threshold"], 0.9)
