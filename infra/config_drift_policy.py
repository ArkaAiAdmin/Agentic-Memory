"""Drift enforcement policy — driven by TOML + scope.

Three responsibilities:
  1. Resolve the active policy for the current scope.
  2. Apply enforcement modes (WARN / SOFT_BLOCK / HARD_FAIL) per tier.
  3. Run the progression tracker so repeated drift escalates over time.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional, cast

from infra.config_drift import (
    DriftEntry,
    build_drift_report,
)
from infra.config_drift_runtime import (
    record_drift,
    get_hits,
    should_escalate,
    mark_escalated,
)
from infra.config_drift_audit import AuditEvent, append_audit_event
from infra.config_drift_escape import (
    active_escape_hatch,
    is_ignored,
)

logger = logging.getLogger(__name__)


class DriftEnforceMode(str, enum.Enum):
    WARN = "warn"
    SOFT_BLOCK = "soft_block"
    HARD_FAIL = "hard_fail"


_MODE_PRIORITY = {
    DriftEnforceMode.WARN: 0,
    DriftEnforceMode.SOFT_BLOCK: 1,
    DriftEnforceMode.HARD_FAIL: 2,
}


@dataclass(frozen=True)
class DriftPolicy:
    scope: str
    detect_on_startup: bool
    default_mode: DriftEnforceMode
    tier_modes: dict[str, DriftEnforceMode]
    soft_block_operations: list[str]
    audit_enabled: bool
    audit_path: str
    progressive_enforcement: bool
    progression_window_secs: int
    progression_max_hits: int
    escape_hatch_enabled: bool
    escape_hatch_max_secs: int
    escape_hatch_audit_every_secs: int

    def mode_for(self, tier: str) -> DriftEnforceMode:
        return self.tier_modes.get(tier, self.default_mode)

    def promote(self, current: DriftEnforceMode) -> DriftEnforceMode:
        if current == DriftEnforceMode.WARN:
            return DriftEnforceMode.SOFT_BLOCK
        if current == DriftEnforceMode.SOFT_BLOCK:
            return DriftEnforceMode.HARD_FAIL
        return DriftEnforceMode.HARD_FAIL

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "default_mode": self.default_mode.value,
            "tier_modes": {k: v.value for k, v in self.tier_modes.items()},
        }

    def policy_hash(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


_production_default = {
    "scope": "production", "detect_on_startup": True,
    "default_mode": DriftEnforceMode.HARD_FAIL,
    "tier_modes": {
        "integrity": DriftEnforceMode.HARD_FAIL,
        "stability": DriftEnforceMode.HARD_FAIL,
        "compliance": DriftEnforceMode.SOFT_BLOCK,
        "operational": DriftEnforceMode.SOFT_BLOCK,
        "neutral": DriftEnforceMode.WARN,
    },
    "soft_block_operations": ["save", "search", "auto_save"],
    "audit_enabled": True, "audit_path": "memory/config_drift_audit.jsonl",
    "progressive_enforcement": True,
    "progression_window_secs": 300, "progression_max_hits": 3,
    "escape_hatch_enabled": True,
    "escape_hatch_max_secs": 14400, "escape_hatch_audit_every_secs": 60,
}

_staging_default = {
    "scope": "staging", "detect_on_startup": True,
    "default_mode": DriftEnforceMode.SOFT_BLOCK,
    "tier_modes": {
        "integrity": DriftEnforceMode.HARD_FAIL,
        "stability": DriftEnforceMode.SOFT_BLOCK,
        "compliance": DriftEnforceMode.WARN,
        "operational": DriftEnforceMode.WARN,
        "neutral": DriftEnforceMode.WARN,
    },
    "soft_block_operations": ["save"],
    "audit_enabled": True, "audit_path": "memory/config_drift_audit.jsonl",
    "progressive_enforcement": True,
    "progression_window_secs": 600, "progression_max_hits": 3,
    "escape_hatch_enabled": True,
    "escape_hatch_max_secs": 28800, "escape_hatch_audit_every_secs": 300,
}

_test_default = {
    "scope": "test", "detect_on_startup": True,
    "default_mode": DriftEnforceMode.WARN,
    "tier_modes": {t: DriftEnforceMode.WARN for t in (
        "integrity", "stability", "compliance", "operational", "neutral",
    )},
    "soft_block_operations": [],
    "audit_enabled": True, "audit_path": "memory/config_drift_audit.jsonl",
    "progressive_enforcement": False,
    "progression_window_secs": 300, "progression_max_hits": 3,
    "escape_hatch_enabled": True,
    "escape_hatch_max_secs": 28800, "escape_hatch_audit_every_secs": 60,
}

_development_default = {
    "scope": "development", "detect_on_startup": True,
    "default_mode": DriftEnforceMode.WARN,
    "tier_modes": {t: DriftEnforceMode.WARN for t in (
        "integrity", "stability", "compliance", "operational", "neutral",
    )},
    "soft_block_operations": [],
    "audit_enabled": True, "audit_path": "memory/config_drift_audit.jsonl",
    "progressive_enforcement": True,
    "progression_window_secs": 600, "progression_max_hits": 5,
    "escape_hatch_enabled": True,
    "escape_hatch_max_secs": 28800, "escape_hatch_audit_every_secs": 300,
}

_SCOPE_DEFAULTS = {
    "production": _production_default,
    "staging": _staging_default,
    "test": _test_default,
    "development": _development_default,
}


def _resolve_policy_default(scope: str) -> DriftPolicy:
    d = cast(dict[str, Any], _SCOPE_DEFAULTS.get(scope, _development_default))
    return DriftPolicy(**d)


_active_policy: Optional[DriftPolicy] = None
_TOML_HOT_RELOAD_SUBSCRIBED: bool = False
_last_resolved_toml_mtime: float = 0.0


def _record_toml_reload_event(mtime: float) -> None:
    from infra.config_drift_audit import AuditEvent, append_audit_event
    policy = resolve_policy()
    try:
        append_audit_event(AuditEvent(
            timestamp=time.time(),
            scope=policy.scope,
            decision="toml_hot_reload",
            tier="",
            flag="policy_global",
            mode="",
            policy_hash=policy.policy_hash(),
            extra={"toml_mtime": mtime},
        ), audit_path=policy.audit_path)
    except Exception as e:
        logger.warning("policy: failed to audit hot-reload: %s", e)


def _on_toml_change(new_mtime: float) -> None:
    global _active_policy, _active_has_inited, _last_resolved_toml_mtime
    logger.info("policy: TOML change detected (mtime=%.0f), reloading", new_mtime)
    try:
        from infra.config import _read_toml
        from infra.toml_watch import get_toml_path
        toml_data = _read_toml(get_toml_path())
        from infra.config_drift_tier_patch import apply_tier_overrides_from_toml
        # Prefer the policy's own audit_path so everything lands in one file
        _audit_path = None
        if _active_policy is not None:
            _audit_path = _active_policy.audit_path
        apply_tier_overrides_from_toml(toml_data, audit_enabled=True, audit_path=_audit_path)
    except Exception as e:
        logger.warning("policy: failed to reload tiers from TOML: %s", e)
    _active_policy = None
    _active_has_inited = False
    _last_resolved_toml_mtime = 0.0
    _record_toml_reload_event(new_mtime)


def resolve_policy(scope: str | None = None) -> DriftPolicy:
    """Compute the active policy for the current process. Idempotent cache."""
    global _active_policy, _last_resolved_toml_mtime, _TOML_HOT_RELOAD_SUBSCRIBED

    if not _TOML_HOT_RELOAD_SUBSCRIBED:
        env_val = os.environ.get("MEMORY_TOML_HOT_RELOAD", "")
        if env_val in ("1", "true", "yes"):
            try:
                from infra.toml_watch import subscribe, start_watcher
                subscribe(_on_toml_change)
                start_watcher()
                _TOML_HOT_RELOAD_SUBSCRIBED = True
                logger.info(
                    "policy: hot-reload subscribed, MEMORY_TOML_HOT_RELOAD=%r", env_val,
                )
            except Exception as e:
                logger.warning("policy: hot-reload subscription failed: %s", e)
        else:
            _TOML_HOT_RELOAD_SUBSCRIBED = True  # mark as checked

    env_val = os.environ.get("MEMORY_TOML_HOT_RELOAD", "")
    if env_val in ("1", "true", "yes"):
        try:
            from infra.toml_watch import current_mtime
            mtime_now = current_mtime()
            if _last_resolved_toml_mtime != 0.0 and _last_resolved_toml_mtime != mtime_now:
                _active_policy = None
        except Exception:
            pass

    if _active_policy is not None:
        return _active_policy

    if scope is None:
        try:
            from infra.scope import resolve_scope as _resolve
            scope = _resolve().value
        except Exception:
            scope = "development"

    policy = _resolve_policy_default(scope)

    # Overlay TOML [drift] section
    try:
        from infra.config import _read_toml, _TOML_PATH
        if _TOML_PATH.exists():
            toml_data = _read_toml(_TOML_PATH)
            drift = toml_data.get("drift") or {}
            kwargs: dict[str, Any] = {}
            if "default_mode" in drift:
                kwargs["default_mode"] = DriftEnforceMode(drift["default_mode"])
            if "tier_modes" in drift:
                merged: dict[str, DriftEnforceMode] = dict(policy.tier_modes)
                for k, v in drift["tier_modes"].items():
                    merged[k] = DriftEnforceMode(v)
                kwargs["tier_modes"] = merged
            for scalar_key in (
                "soft_block_operations", "audit_enabled", "audit_path",
                "progressive_enforcement", "progression_window_secs",
                "progression_max_hits", "escape_hatch_enabled",
                "escape_hatch_max_secs", "escape_hatch_audit_every_secs",
                "detect_on_startup",
            ):
                if scalar_key in drift:
                    kwargs[scalar_key] = drift[scalar_key]
            if kwargs:
                policy = DriftPolicy(**{**asdict(policy), **kwargs})
    except Exception as e:
        logger.warning("policy: failed to overlay [drift] TOML: %s", e)

    # Legacy env-var opt-out —
    #   MEMORY_FAIL_ON_INTEGRITY_DRIFT=0 downgrades integrity hard_fail → warn.
    _legacy = os.environ.get("MEMORY_FAIL_ON_INTEGRITY_DRIFT", "")
    if _legacy in ("0", "false", "no"):
        legacy_merged: dict[str, DriftEnforceMode] = dict(policy.tier_modes)
        legacy_merged["integrity"] = DriftEnforceMode.WARN
        policy = DriftPolicy(**{**asdict(policy), "tier_modes": legacy_merged})

    try:
        from infra.toml_watch import current_mtime
        _last_resolved_toml_mtime = current_mtime()
    except Exception:
        pass
    _active_policy = policy
    return policy


def reset_policy_cache() -> None:
    global _active_policy
    _active_policy = None


_active_has_inited: bool = False


def run_startup_enforcement() -> None:
    """Called once per process at startup from get_config()."""
    global _active_has_inited
    if _active_has_inited:
        return

    if _is_test_environment():
        _active_has_inited = True
        return

    # Opt-out for processes that perform their own drift handling
    # (e.g. the config-drift surveillance cron, which detects and reports
    # drift via --enforce-scope rather than failing at import time).
    if os.environ.get("MEMORY_CONFIG_DRIFT_SKIP_ENFORCEMENT", "") in ("1", "true", "yes"):
        _active_has_inited = True
        return

    from infra.scope import is_test_scope
    if is_test_scope():
        _active_has_inited = True
        return

    policy = resolve_policy()
    if not policy.detect_on_startup:
        _active_has_inited = True
        return

    try:
        drift_report = build_drift_report()
        enforce(drift_report, policy=policy, verb="init")
    except DriftEnforcementError as e:
        sys.stderr.write(
            f"FATAL: config drift on startup\n"
            f"  flag={e.flag}\n"
            f"  tier={e.tier}\n"
            f"  mode={e.mode.value}\n"
            f"  verdicts={e.verdicts!r}\n"
            f"  scope={policy.scope}\n"
            f"  policy_hash={policy.policy_hash()}\n"
            f"  escape: MEMORY_ESCAPE_HATCH='ignore-{e.tier};<reason>;<op-id>;<secs>;60'\n"
        )
        raise SystemExit(78)
    except Exception:
        logger.warning("startup enforcement: unexpected error, continuing")
    finally:
        _active_has_inited = True


class DriftEnforcementError(Exception):
    def __init__(self, *, mode: DriftEnforceMode, tier: str, flag: str,
                 verdicts: list[str], escaped: bool = False,
                 operator_id: str = "", reason: str = "",
                 progression_hits: int = 0, policy_hash: str = ""):
        self.mode = mode
        self.tier = tier
        self.flag = flag
        self.verdicts = verdicts
        self.escaped = escaped
        self.operator_id = operator_id
        self.reason = reason
        self.progression_hits = progression_hits
        self.policy_hash = policy_hash
        super().__init__(
            f"drift enforcement {mode.value} on {flag} (tier={tier}): "
            f"{'; '.join(verdicts)}"
        )

    def to_dict(self) -> dict:
        return {
            "error": "DriftEnforcementError",
            "mode": self.mode.value,
            "tier": self.tier,
            "flag": self.flag,
            "verdicts": self.verdicts,
            "escaped": self.escaped,
            "operator_id": self.operator_id,
            "reason": self.reason,
            "progression_hits": self.progression_hits,
            "policy_hash": self.policy_hash,
        }


def _is_test_environment() -> bool:
    """True when running under pytest/unittest (not a real process).

    The drift-enforcement framework is for live processes. During the
    test suite the conftest deliberately sets drift-inducing env vars
    (e.g. MEMORY_WRITE_JOURNAL_ENABLED=0); enforcing there would fail
    every save/search operation. The enforcement behaviour is still
    covered by the subprocess-isolated config-drift tests, whose helper
    processes do not import unittest/pytest.
    """
    return (
        "pytest" in sys.modules
        or "unittest" in sys.modules
        or os.environ.get("PYTEST_CURRENT_TEST") is not None
    )


def enforce(report_or_entry, *, policy=None, verb: str = "save") -> None:
    if _is_test_environment():
        return
    if policy is None:
        policy = resolve_policy()

    if hasattr(report_or_entry, "entries"):
        raw_entries = [e for e in report_or_entry.entries if e.has_drift()]
    else:
        raw_entries = [report_or_entry] if report_or_entry.has_drift() else []

    if not raw_entries:
        return

    hatch = active_escape_hatch(policy=policy)

    for e in raw_entries:
        tier = e.severity
        mode = policy.mode_for(tier)
        if policy.progressive_enforcement:
            record_drift(tier, window_secs=policy.progression_window_secs)
            if should_escalate(tier, policy):
                mode = policy.promote(mode)
                mark_escalated(tier)
        effective_mode = mode
        hatch_cover = False
        if hatch is not None and is_ignored(tier, policy=policy):
            effective_mode = DriftEnforceMode.WARN
            hatch_cover = True
        _enforce_one(e, mode=effective_mode, verb=verb, policy=policy,
                     hatch=hatch, progression_hits=get_hits(tier),
                     hatch_cover=hatch_cover)


def _enforce_one(entry: DriftEntry, *, mode: DriftEnforceMode, verb: str,
                 policy, hatch, progression_hits: int,
                 hatch_cover: bool = False) -> None:
    if mode == DriftEnforceMode.HARD_FAIL:
        evt = AuditEvent(
            timestamp=time.time(),
            scope=policy.scope,
            decision="hard_fail",
            tier=entry.severity,
            flag=entry.flag,
            mode=mode.value,
            operator_id=(hatch.operator_id if hatch else ""),
            reason=(hatch.reason if hatch else ""),
            progression_hits=progression_hits,
            policy_hash=policy.policy_hash(),
            extra={"verdicts": entry.drift_verdicts},
        )
        if policy.audit_enabled:
            append_audit_event(evt, audit_path=policy.audit_path)
        raise DriftEnforcementError(
            mode=mode, tier=entry.severity, flag=entry.flag,
            verdicts=entry.drift_verdicts,
            progression_hits=progression_hits,
            policy_hash=policy.policy_hash(),
        )

    if mode == DriftEnforceMode.SOFT_BLOCK:
        if verb not in policy.soft_block_operations:
            return
        evt = AuditEvent(
            timestamp=time.time(),
            scope=policy.scope,
            decision="soft_block",
            tier=entry.severity,
            flag=entry.flag,
            mode=mode.value,
            operator_id=(hatch.operator_id if hatch else ""),
            reason=(hatch.reason if hatch else ""),
            progression_hits=progression_hits,
            policy_hash=policy.policy_hash(),
            extra={"verdicts": entry.drift_verdicts, "escaped": hatch_cover},
        )
        if policy.audit_enabled:
            append_audit_event(evt, audit_path=policy.audit_path)
        raise DriftEnforcementError(
            mode=mode, tier=entry.severity, flag=entry.flag,
            verdicts=entry.drift_verdicts, escaped=hatch_cover,
            operator_id=(hatch.operator_id if hatch else ""),
            reason=(hatch.reason if hatch else ""),
            progression_hits=progression_hits,
            policy_hash=policy.policy_hash(),
        )

    if policy.audit_enabled and entry.severity in ("integrity", "stability"):
        evt = AuditEvent(
            timestamp=time.time(),
            scope=policy.scope,
            decision="warn",
            tier=entry.severity,
            flag=entry.flag,
            mode=mode.value,
            operator_id=(hatch.operator_id if hatch else ""),
            reason=(hatch.reason if hatch else ""),
            progression_hits=progression_hits,
            policy_hash=policy.policy_hash(),
            extra={"verdicts": entry.drift_verdicts},
        )
        append_audit_event(evt, audit_path=policy.audit_path)

    logger.warning(
        "drift WARN [%s/%s]: %s — %s",
        entry.severity, mode.value, entry.flag,
        "; ".join(entry.drift_verdicts),
    )