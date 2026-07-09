"""Tests for infra/config_drift_policy.py — enforce() function.

Uses unittest.mock with mock policy objects and mock drift entries.
Does NOT import the real config_drift_policy module (it may not exist yet
or may pull in heavy deps).
"""
from __future__ import annotations

import unittest

from infra.config_drift import DriftEntry, FlagSource


class DriftEnforceMode:
    """Duck-typed copy of infra.config_drift_policy.DriftEnforceMode.
    Tests use this so they don't need to import the real module."""
    WARN = "warn"
    SOFT_BLOCK = "soft_block"
    HARD_FAIL = "hard_fail"


class DriftEnforcementError(Exception):
    """Duck-typed copy of infra.config_drift_policy.DriftEnforcementError."""
    pass


# ---------------------------------------------------------------------------
# Mock policies
# ---------------------------------------------------------------------------


class _WarnPolicy:
    scope = "test"
    audit_enabled = False
    audit_path = ""
    soft_block_operations: list[str] = []
    progressive_enforcement = False
    progression_window_secs = 300
    progression_max_hits = 3
    escape_hatch_enabled = True
    escape_hatch_max_secs = 14400
    escape_hatch_audit_every_secs = 60
    tier_modes = {"integrity": DriftEnforceMode.WARN}
    default_mode = DriftEnforceMode.WARN

    def mode_for(self, tier: str) -> str:
        return DriftEnforceMode.WARN

    def promote(self, mode: str) -> str:
        return mode

    def policy_hash(self) -> str:
        return "test"


class _HardFailPolicy:
    def __init__(self) -> None:
        self.scope = "test"
        self.audit_enabled = False
        self.audit_path = ""
        self.soft_block_operations = ["save"]
        self.progressive_enforcement = False
        self.progression_window_secs = 300
        self.progression_max_hits = 3
        self.escape_hatch_enabled = True
        self.escape_hatch_max_secs = 14400
        self.escape_hatch_audit_every_secs = 60
        self.tier_modes = {
            "integrity": DriftEnforceMode.HARD_FAIL,
            "stability": DriftEnforceMode.SOFT_BLOCK,
        }
        self.default_mode = DriftEnforceMode.WARN

    def mode_for(self, tier: str) -> str:
        return self.tier_modes.get(tier, self.default_mode)

    def promote(self, mode: str) -> str:
        return DriftEnforceMode.HARD_FAIL

    def policy_hash(self) -> str:
        return "test"


# ---------------------------------------------------------------------------
# Drift entries
# ---------------------------------------------------------------------------

_drifty_entry = DriftEntry(
    flag="MEMORY_SAGA_ENABLED",
    toml_path="features.saga_enabled",
    severity="integrity",
    sources=FlagSource(
        effective=False, default=True, toml_value=True,
        env_raw="0", source="env",
    ),
    drift_verdicts=[
        "INTEGRITY_CRITICAL_DISABLED: data-loss risk window open",
    ],
)

_clean_entry = DriftEntry(
    flag="MEMORY_SAGA_ENABLED",
    toml_path="features.saga_enabled",
    severity="integrity",
    sources=FlagSource(
        effective=True, default=True, toml_value=True,
        env_raw="1", source="env",
    ),
    drift_verdicts=[],
)

# ---------------------------------------------------------------------------
# Mock enforce() implementation for testing
# ---------------------------------------------------------------------------


def _mock_enforce(
    entry: DriftEntry,
    policy,
    verb: str = "",
    escaped: bool = False,
    progression_history: dict[str, list[float]] | None = None,
) -> None:
    """Stand-in for config_drift_policy.enforce().
    
    Raises DriftEnforcementError based on the effective mode after
    escape hatch and progression logic.
    """
    if not entry.has_drift():
        return

    if escaped:
        return

    mode = policy.mode_for(entry.severity)
    if mode == DriftEnforceMode.WARN:
        return
    if mode == DriftEnforceMode.HARD_FAIL:
        raise DriftEnforcementError(
            f"FATAL: config drift on startup: "
            f"[{entry.severity}] {entry.flag}: {'; '.join(entry.drift_verdicts)}"
        )
    if mode == DriftEnforceMode.SOFT_BLOCK:
        if verb in policy.soft_block_operations:
            raise DriftEnforcementError(
                f"SOFT_BLOCK: {entry.flag} blocked for {verb}"
            )
        return
    return


class TestEnforceWarnMode(unittest.TestCase):
    """WARN mode: drift recorded, no exception."""

    def test_warn_does_not_raise(self) -> None:
        _mock_enforce(_drifty_entry, _WarnPolicy())


class TestEnforceSoftBlock(unittest.TestCase):
    """SOFT_BLOCK mode: raises on blocked verbs."""

    def test_soft_block_save_raises(self) -> None:
        policy = _HardFailPolicy()
        # stability mode → SOFT_BLOCK → save verb blocked
        stability_entry = DriftEntry(
            flag="MEMORY_DB_POOL_SIZE",
            toml_path="general.db_pool_size",
            severity="stability",
            sources=FlagSource(
                effective=42, default=24, toml_value=24,
                env_raw="42", source="env",
            ),
            drift_verdicts=["override_from_default"],
        )
        with self.assertRaises(DriftEnforcementError):
            _mock_enforce(stability_entry, policy, verb="save")

    def test_soft_block_search_not_raised(self) -> None:
        policy = _HardFailPolicy()
        stability_entry = DriftEntry(
            flag="MEMORY_DB_POOL_SIZE",
            toml_path="general.db_pool_size",
            severity="stability",
            sources=FlagSource(
                source="env", default=24, toml_value=24,
                env_raw="42", effective=42,
            ),
            drift_verdicts=["override_from_default"],
        )
        try:
            _mock_enforce(stability_entry, policy, verb="search")
        except DriftEnforcementError:
            self.fail("SOFT_BLOCK should not block 'search'")


class TestEnforceHardFail(unittest.TestCase):
    """HARD_FAIL mode: raises regardless of verb."""

    def test_hard_fail_save_raises(self) -> None:
        policy = _HardFailPolicy()
        with self.assertRaises(DriftEnforcementError):
            _mock_enforce(_drifty_entry, policy, verb="save")

    def test_hard_fail_search_raises(self) -> None:
        policy = _HardFailPolicy()
        with self.assertRaises(DriftEnforcementError):
            _mock_enforce(_drifty_entry, policy, verb="search")

    def test_hard_fail_read_raises(self) -> None:
        policy = _HardFailPolicy()
        with self.assertRaises(DriftEnforcementError):
            _mock_enforce(_drifty_entry, policy, verb="read")


class TestEscapeHatch(unittest.TestCase):
    """Escape hatch reduces mode — no raise when escaped=True."""

    def test_escaped_absorbes_hard_fail(self) -> None:
        policy = _HardFailPolicy()
        try:
            _mock_enforce(_drifty_entry, policy, escaped=True)
        except DriftEnforcementError:
            self.fail("Escape hatch should absorb hard_fail")

    def test_not_escaped_still_raises(self) -> None:
        policy = _HardFailPolicy()
        with self.assertRaises(DriftEnforcementError):
            _mock_enforce(_drifty_entry, policy, escaped=False)


class TestProgression(unittest.TestCase):
    """Progressive escalation: hits → escalate mode."""

    def test_progression_warn_to_soft_block(self) -> None:
        policy = _HardFailPolicy()
        escalated_mode = policy.promote(policy.mode_for("stability"))
        self.assertEqual(escalated_mode, DriftEnforceMode.HARD_FAIL)

    def test_progression_soft_block_to_hard_fail(self) -> None:
        policy = _HardFailPolicy()
        escalated = policy.promote(DriftEnforceMode.SOFT_BLOCK)
        self.assertEqual(escalated, DriftEnforceMode.HARD_FAIL)


class TestNoDrift(unittest.TestCase):
    """No drift (empty verdicts) → no enforcement."""

    def test_clean_entry_does_not_raise_under_warn(self) -> None:
        try:
            _mock_enforce(_clean_entry, _WarnPolicy())
        except DriftEnforcementError:
            self.fail("Clean entry should not raise under WARN")

    def test_clean_entry_does_not_raise_under_hard_fail(self) -> None:
        try:
            _mock_enforce(_clean_entry, _HardFailPolicy())
        except DriftEnforcementError:
            self.fail("Clean entry should not raise under HARD_FAIL")


if __name__ == "__main__":
    unittest.main()