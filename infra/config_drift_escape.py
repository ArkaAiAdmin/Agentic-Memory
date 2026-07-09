"""Time-bounded escape hatch for failing-safe drift enforcement.

When drift enforcement would block a process (HARD_FAIL on init, or
SOFT_BLOCK on a critical op), an operator can issue a bounded-time
escape by setting MEMORY_ESCAPE_HATCH in the environment:

    MEMORY_ESCAPE_HATCH='scope;reason;operator-id;duration_secs;reaffirm_secs'

The hatch is enforced as follows:
  - Reason and operator-id are MANDATORY (for compliance)
  - Duration cannot exceed the policy's escape_hatch_max_secs
  - Auto-expires after duration_seconds; subsequent drift triggers again
  - Requires re-affirmation every `affirmation_interval_secs` seconds
  - Every hatch activation is recorded to audit JSONL
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from infra.config_drift_audit import AuditEvent, append_audit_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EscapeHatch:
    scope: str
    reason: str
    operator_id: str
    issued_at: float
    duration_secs: int
    max_duration_secs: int
    affirmation_interval_secs: int

    def is_active(self, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        return now < self.issued_at + self.duration_secs

    def age_secs(self, now: float | None = None) -> int:
        if now is None:
            now = time.time()
        return max(0, int(now - self.issued_at))

    def seconds_remaining(self, now: float | None = None) -> int:
        if now is None:
            now = time.time()
        return max(0, int(self.issued_at + self.duration_secs - now))

    def requires_reaffirmation(self, now: float | None = None) -> bool:
        return self.age_secs(now) > self.affirmation_interval_secs

    def to_audit_dict(self) -> dict:
        return {
            "scope": self.scope,
            "reason": self.reason,
            "operator_id": self.operator_id,
            "issued_at": self.issued_at,
            "duration_secs": self.duration_secs,
            "max_duration_secs": self.max_duration_secs,
            "is_active": self.is_active(),
            "age_secs": self.age_secs(),
            "seconds_remaining": self.seconds_remaining(),
            "requires_reaffirmation": self.requires_reaffirmation(),
        }


_active_hatch: Optional[EscapeHatch] = None


def register_escape_hatch(policy, *, env_value: str | None = None) -> Optional[EscapeHatch]:
    """Parse MEMORY_ESCAPE_HATCH; if valid and within the policy's max,
    register it as the active global hatch. Returns the registered hatch
    or None if no valid hatch was set.
    """
    global _active_hatch
    if env_value is None:
        env_value = os.environ.get("MEMORY_ESCAPE_HATCH", "")
    if not (env_value or "").strip():
        return None

    if not policy.escape_hatch_enabled:
        logger.warning("escape: MEMORY_ESCAPE_HATCH set but escape_hatch_enabled=False in policy")
        return None

    parts = env_value.split(";")
    if len(parts) != 5:
        logger.warning(
            "escape: MEMORY_ESCAPE_HATCH must be in format "
            "'scope;reason;operator-id;duration_secs;reaffirm_secs', got: %r",
            env_value[:200],
        )
        return None
    scope_str, reason, operator_id, duration_str, reaffirm_str = parts
    if not reason.strip() or not operator_id.strip():
        logger.warning("escape: MEMORY_ESCAPE_HATCH requires non-empty reason and operator_id")
        return None

    try:
        duration = int(duration_str)
        reaffirm = int(reaffirm_str)
    except ValueError:
        logger.warning("escape: duration/reaffirm must be integers")
        return None

    if duration > policy.escape_hatch_max_secs:
        logger.warning(
            "escape: requested duration %d exceeds policy max %d; clamping",
            duration, policy.escape_hatch_max_secs,
        )
        duration = policy.escape_hatch_max_secs

    if scope_str not in ("ignore-integrity", "ignore-stability", "ignore-all"):
        logger.warning(
            "escape: unknown scope %r; expected ignore-integrity / ignore-stability / ignore-all",
            scope_str,
        )
        return None

    hatch = EscapeHatch(
        scope=scope_str,
        reason=reason.strip(),
        operator_id=operator_id.strip(),
        issued_at=time.time(),
        duration_secs=max(duration, 60),
        max_duration_secs=policy.escape_hatch_max_secs,
        affirmation_interval_secs=max(reaffirm, 30),
    )
    _active_hatch = hatch
    logger.warning(
        "escape: ACTIVATED scope=%s reason=%r operator=%s duration=%ds (max=%ds)",
        hatch.scope, hatch.reason, hatch.operator_id,
        hatch.duration_secs, hatch.max_duration_secs,
    )

    try:
        evt = AuditEvent(
            timestamp=time.time(),
            scope=policy.scope,
            decision="escape_hatch_active",
            tier=scope_str.replace("ignore-", ""),
            flag="MEMORY_ESCAPE_HATCH",
            mode="warn",
            operator_id=hatch.operator_id,
            reason=hatch.reason,
            policy_hash=policy.policy_hash() if hasattr(policy, "policy_hash") else "",
        )
        append_audit_event(evt, audit_path=getattr(policy, "audit_path", "memory/config_drift_audit.jsonl"))
    except Exception as e:
        logger.warning("escape: audit write failed: %s", e)

    return hatch


def active_escape_hatch(*, policy=None) -> Optional[EscapeHatch]:
    if policy is not None:
        register_escape_hatch(policy)
    return _active_hatch if _active_hatch and _active_hatch.is_active() else None


def is_ignored(tier: str, *, policy=None) -> bool:
    h = active_escape_hatch(policy=policy)
    if h is None:
        return False
    if h.requires_reaffirmation():
        return False
    return h.scope in ("ignore-all", f"ignore-{tier}")


def reset_escape_hatch() -> None:
    global _active_hatch
    _active_hatch = None
