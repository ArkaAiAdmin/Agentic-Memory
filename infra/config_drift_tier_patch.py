"""Hot-patch the tier table at runtime.

Provides apply_tier_overrides_from_toml() as a callable function
that diffs [drift_tiers] from TOML data against the live _FLAG_TIERS,
applies additive changes, optionally removes overrides, and audits.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from infra.config_drift import (
    DriftSeverity,
    _FLAG_TIERS,
    _HARDCODE_DEFAULTS,
    set_flag_tier,
)
from infra.config_drift_audit import append_audit_event, AuditEvent
from infra.config_drift_policy import resolve_policy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TierPatch:
    env_key: str
    new_tier: Optional[DriftSeverity]
    source: str = "toml"


@dataclass(frozen=True)
class TierPatchResult:
    patched: list[TierPatch]
    rejected: list[tuple[str, str, str]]
    timestamped_at: float


def apply_tier_overrides_from_toml(
    toml_data: dict,
    *,
    audit_path: str | None = None,
    audit_enabled: bool = True,
    strict: bool = False,
) -> TierPatchResult:
    overrides = toml_data.get("drift_tiers") or {}
    patched: list[TierPatch] = []
    rejected: list[tuple[str, str, str]] = []

    for key, value in overrides.items():
        env_key = key.strip().upper()
        if not env_key:
            rejected.append((key, str(value), "empty key"))
            if not strict:
                logger.warning("tier_patch: rejected %r", (key, str(value), "empty key"))
            continue

        if str(value).strip() == "":
            # Empty-string override = "remove the override for this flag".
            # For a BUILT-IN flag restore the hardcoded default; only a truly
            # runtime-added key (not present in _HARDCODE_DEFAULTS) is popped.
            if env_key in _FLAG_TIERS:
                if env_key in _HARDCODE_DEFAULTS:
                    _FLAG_TIERS[env_key] = _HARDCODE_DEFAULTS[env_key]
                else:
                    _FLAG_TIERS.pop(env_key, None)
                patched.append(TierPatch(env_key=env_key, new_tier=None, source="toml"))
            continue

        value_lower = str(value).strip().lower()
        tier = None
        for s in DriftSeverity:
            if s.value == value_lower:
                tier = s
                break
        if tier is None:
            rejected.append((env_key, str(value), f"unknown tier {value!r}"))
            if not strict:
                logger.warning(
                    "tier_patch: rejected %r",
                    (env_key, str(value), f"unknown tier {value!r}"),
                )
            continue

        prev = _FLAG_TIERS.get(env_key)
        if prev != tier:
            set_flag_tier(env_key, tier)
            patched.append(TierPatch(env_key=env_key, new_tier=tier, source="toml"))

    if audit_enabled and patched:
        try:
            policy = resolve_policy()
            append_audit_event(AuditEvent(
                timestamp=time.time(),
                scope=policy.scope,
                decision="tier_patch_applied",
                tier="",
                flag="policy_global",
                mode="",
                policy_hash=policy.policy_hash(),
                extra={
                    "patched_count": len(patched),
                    "rejected_count": len(rejected),
                    "patched": [(p.env_key, (p.new_tier.value if p.new_tier else None)) for p in patched],
                    "rejected": rejected,
                },
            ), audit_path=audit_path)
        except Exception as e:
            logger.warning("tier_patch: audit emit failed: %s", e)

    if strict and rejected:
        raise ValueError(f"tier override(s) rejected: {rejected}")

    return TierPatchResult(patched=patched, rejected=rejected, timestamped_at=time.time())
