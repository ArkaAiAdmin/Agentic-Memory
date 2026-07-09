"""Tests for the canonical tier-reset primitive (`reset_flag_tiers`) and its
integration into `reset_config()`.

Subprocess-isolated: run with
    OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/python -m pytest eval/test_config_drift_tier_reset.py -q
"""

from __future__ import annotations

import copy

from infra.config_drift import (
    DriftSeverity,
    _FLAG_TIERS,
    _HARDCODE_DEFAULTS,
    reset_flag_tiers,
)
from infra.config_drift_policy import resolve_policy, reset_policy_cache
from infra.config_drift_tier_patch import apply_tier_overrides_from_toml
from infra.config import reset_config


def _snapshot_flag_tiers() -> dict:
    return copy.deepcopy(_FLAG_TIERS)


def _restore_flag_tiers(snapshot: dict) -> None:
    _FLAG_TIERS.clear()
    _FLAG_TIERS.update(snapshot)


class TestResetFlagTiers:
    def setup_method(self):
        # Preserve global mutable state so we don't leak into other tests.
        self._flag_tiers_snapshot = _snapshot_flag_tiers()

    def teardown_method(self):
        _restore_flag_tiers(self._flag_tiers_snapshot)
        reset_policy_cache()

    def test_reset_restores_builtin_and_drops_runtime_key(self):
        baseline = dict(_HARDCODE_DEFAULTS)

        # (a) Mutate a built-in flag via the TOML override path.
        apply_tier_overrides_from_toml(
            {"drift_tiers": {"MEMORY_SAGA_ENABLED": "operational"}},
            audit_enabled=False,
        )
        # (b) Add a runtime-only key not present in _HARDCODE_DEFAULTS.
        _FLAG_TIERS["ZZZ_RUNTIME_FLAG"] = DriftSeverity.STABILITY

        assert _FLAG_TIERS["MEMORY_SAGA_ENABLED"] == DriftSeverity.OPERATIONAL
        assert "ZZZ_RUNTIME_FLAG" in _FLAG_TIERS

        # Act.
        reset_flag_tiers()

        # Built-in reverted to hardcoded default.
        assert _FLAG_TIERS["MEMORY_SAGA_ENABLED"] == baseline["MEMORY_SAGA_ENABLED"]
        assert "ZZZ_RUNTIME_FLAG" not in _FLAG_TIERS
        assert dict(_FLAG_TIERS) == baseline

    def test_reset_config_resets_both_tiers_and_policy_cache(self):
        baseline = dict(_HARDCODE_DEFAULTS)

        # Mutate tiers via the TOML override path.
        apply_tier_overrides_from_toml(
            {"drift_tiers": {"MEMORY_TEMPORAL_KG": "operational"}},
            audit_enabled=False,
        )
        assert _FLAG_TIERS["MEMORY_TEMPORAL_KG"] == DriftSeverity.OPERATIONAL

        # Populate the policy cache, then capture its identity.
        policy_before = resolve_policy()
        assert dict(_FLAG_TIERS) != baseline

        # Act — single call resets both the config singleton AND live tiers.
        reset_config()

        # Tiers restored to canonical defaults.
        assert dict(_FLAG_TIERS) == baseline

        # Policy cache was cleared; a fresh resolve re-derives a new object.
        policy_after = resolve_policy()
        assert policy_after is not policy_before
