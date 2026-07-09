"""CI guard for the core config-drift invariant:

  1. Every drift tier is populated (no dead/empty enforcement band).
  2. The policy hash is stable/deterministic across resolves (so fleet
     diffs are meaningful).
  3. The policy hash actually encodes tier posture (not vacuous).

Subprocess-isolated: run with
    OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/python -m pytest eval/test_policy_eligibility.py -q
"""

from __future__ import annotations

import copy

from infra.config_drift import (
    DriftSeverity,
    _FLAG_TIERS,
    set_flag_tier,
)
from infra.config_drift_policy import resolve_policy, reset_policy_cache


def _snapshot_flag_tiers() -> dict:
    return copy.deepcopy(_FLAG_TIERS)


def _restore_flag_tiers(snapshot: dict) -> None:
    _FLAG_TIERS.clear()
    _FLAG_TIERS.update(snapshot)


class TestEveryTierPopulated:
    """TEST 1 — every DriftSeverity tier must have >=1 flag mapped.

    A tier with zero flags is a dead/empty enforcement band: nothing can
    ever drift into it, so its enforcement mode is meaningless and the
    framework's tier contract is broken. Guards against a
    `_FLAG_TIERS` / tier-assignment that forgot to populate a band.
    """

    def test_every_tier_has_at_least_one_flag(self):
        counts: dict[DriftSeverity, int] = {s: 0 for s in DriftSeverity}
        for tier in _FLAG_TIERS.values():
            counts[tier] = counts.get(tier, 0) + 1

        empty = [s.name for s in DriftSeverity if counts.get(s, 0) == 0]
        assert not empty, (
            f"DriftSeverity tier(s) have zero flags mapped in _FLAG_TIERS: "
            f"{empty}. Every tier must be populated or the enforcement band "
            f"is dead. Fix infra/config_drift.py _FLAG_TIERS."
        )

        # Positive assertion: every tier genuinely has at least one flag.
        for s in DriftSeverity:
            assert counts[s] >= 1, f"tier {s.name} should have >=1 flag"


class TestPolicyHashDeterministic:
    """TEST 2 — policy_hash() must be stable across repeated resolves.

    If the hash differs between two identical resolves, it is hashing
    something non-deterministic (set/dict iteration order, a timestamp,
    unsorted data) and fleet drift diffs become meaningless.
    """

    def setup_method(self):
        reset_policy_cache()

    def teardown_method(self):
        reset_policy_cache()

    def test_hash_stable_across_resets(self):
        for _ in range(10):
            reset_policy_cache()
            p1 = resolve_policy().policy_hash()
            reset_policy_cache()
            p2 = resolve_policy().policy_hash()
            assert p1 == p2, (
                f"policy_hash not deterministic: {p1!r} != {p2!r}. "
                f"policy_hash() likely hashes non-deterministic data."
            )

    def test_hash_stable_within_same_cache(self):
        reset_policy_cache()
        p1 = resolve_policy().policy_hash()
        # Second call hits the idempotent cache; must be identical anyway.
        p2 = resolve_policy().policy_hash()
        assert p1 == p2


class TestPolicyHashEncodesTierPosture:
    """TEST 3 — policy_hash() must change when a flag's tier flips.

    This proves the hash is not vacuous: it actually encodes the tier
    posture. Only this test mutates _FLAG_TIERS, and it fully restores it
    so no tier state leaks into other tests.
    """

    def setup_method(self):
        reset_policy_cache()
        self._snapshot = _snapshot_flag_tiers()

    def teardown_method(self):
        # Always restore the canonical tier table, then clear the cache.
        _restore_flag_tiers(self._snapshot)
        reset_policy_cache()

    def test_hash_changes_when_flag_tier_flips(self):
        # Pick a built-in flag and its original tier.
        flag = "MEMORY_EMBEDDING_BACKEND"
        assert flag in _FLAG_TIERS, "expected built-in flag in _FLAG_TIERS"
        original_tier = _FLAG_TIERS[flag]

        # Compute the baseline hash under the canonical tiers.
        reset_policy_cache()
        baseline = resolve_policy().policy_hash()

        # Flip the flag to a *different* tier.
        other = next(
            t for t in DriftSeverity if t is not original_tier
        )
        try:
            set_flag_tier(flag, other)

            # Hash must differ — otherwise it does not encode tier posture.
            reset_policy_cache()
            flipped = resolve_policy().policy_hash()
            assert flipped != baseline, (
                f"policy_hash did not change when {flag} tier flipped "
                f"{original_tier.value!r} -> {other.value!r}; hash is vacuous."
            )
        finally:
            # Restore original tier immediately (teardown also restores table).
            set_flag_tier(flag, original_tier)

        # After restoring, the hash must match the original baseline again.
        reset_policy_cache()
        restored = resolve_policy().policy_hash()
        assert restored == baseline, (
            "policy_hash did not return to baseline after restoring tier; "
            "global tier state leaked."
        )
