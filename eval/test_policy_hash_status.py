"""Tests for memory_maintenance(operation='policy_hash_status')."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


def _clean_memory_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_")}
    return env


class TestPolicyHashStatus(unittest.TestCase):
    def setUp(self):
        from infra.rate_limiter import configure_rate_limits
        configure_rate_limits()
        self._rl_patcher = patch("infra.infrastructure.rate_limit_check", return_value=True)
        self._rl_patcher.start()

    def tearDown(self):
        self._rl_patcher.stop()

    def _call(self, **kwargs):
        from mcp_surface.mcp_maintenance import memory_maintenance
        from infra.config_drift_policy import reset_policy_cache
        reset_policy_cache()
        return memory_maintenance("policy_hash_status", **kwargs)

    def test_no_peers_returns_empty_summary(self):
        with patch.dict(os.environ, _clean_memory_env(), clear=False):
            out = json.loads(self._call())
        self.assertEqual(out["summary"]["total_peers"], 0)

    def test_local_policy_hash_present(self):
        with patch.dict(os.environ, _clean_memory_env(), clear=False):
            out = json.loads(self._call())
        self.assertIn("policy_hash", out["local"])
        self.assertEqual(len(out["local"]["policy_hash"]), 16)

    def test_schema_version_present(self):
        with patch.dict(os.environ, _clean_memory_env(), clear=False):
            out = json.loads(self._call())
        self.assertEqual(out["schema_version"], 1)

    def test_summary_keys_present(self):
        with patch.dict(os.environ, _clean_memory_env(), clear=False):
            out = json.loads(self._call())
        for k in ("total_peers", "aligned", "divergent", "unreachable", "pending"):
            self.assertIn(k, out["summary"])

    def test_force_refresh_bypasses_cache(self):
        with patch.dict(os.environ, _clean_memory_env(), clear=False):
            r1 = self._call(force_refresh=True)
            r2 = self._call(force_refresh=True)
        self.assertEqual(json.loads(r1)["schema_version"], 1)
        self.assertEqual(json.loads(r2)["schema_version"], 1)

    def test_timeout_budget_respected(self):
        with patch.dict(os.environ, _clean_memory_env(), clear=False):
            out = json.loads(self._call(peer_timeout_s=0.5))
        self.assertEqual(out["schema_version"], 1)

    def test_include_full_policy_flag(self):
        with patch.dict(os.environ, _clean_memory_env(), clear=False):
            out = json.loads(self._call(include_full_policy=True))
        self.assertEqual(out["schema_version"], 1)

    def test_cache_ttl_configurable(self):
        with patch.dict(os.environ, _clean_memory_env(), clear=False):
            out = json.loads(self._call(cache_ttl_s=120))
        self.assertEqual(out["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
