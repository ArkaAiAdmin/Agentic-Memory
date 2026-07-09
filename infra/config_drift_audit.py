"""Drift enforcement audit trail.

Append-only JSONL log of every enforcement decision.  The file is
rotated when ``audit_max_bytes`` is reached.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEvent:
    timestamp: float
    scope: str
    decision: str
    tier: str
    flag: str
    mode: str
    operator_id: str = ""
    reason: str = ""
    progression_hits: int = 0
    policy_hash: str = ""
    extra: dict = field(default_factory=dict)

    def to_jsonl(self) -> str:
        d = asdict(self)
        return json.dumps(d, default=str)


_audit_lock = threading.Lock()
_max_bytes: int = 50_102_400


def _resolve_audit_path(configured: str) -> Path:
    if not configured:
        configured = "memory/config_drift_audit.jsonl"
    p = Path(configured)
    if not p.is_absolute():
        install_root = os.environ.get(
            "MEMORY_INSTALL_ROOT",
            os.path.expanduser("~/.config/agentic-memory"),
        )
        p = (Path(install_root) / p).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _max_bytes:
            backup = path.with_suffix(path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            path.rename(backup)
            logger.info("audit: rotated %s -> %s", path, backup)
    except OSError as e:
        logger.warning("audit: rotation failed: %s", e)


def append_audit_event(event: AuditEvent, audit_path: Optional[str] = None) -> None:
    cfg_path = audit_path or os.environ.get(
        "MEMORY_DRIFT_AUDIT_PATH", "memory/config_drift_audit.jsonl",
    )
    path = _resolve_audit_path(cfg_path)
    line = event.to_jsonl() + "\n"
    with _audit_lock:
        _rotate_if_needed(path)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            logger.warning("audit: failed to write %s: %s", path, e)


def read_audit_events(
    audit_path: Optional[str] = None,
    *,
    since_ts: float = 0.0,
    decision_filter: Optional[str] = None,
    limit: int = 100,
) -> list[AuditEvent]:
    path = _resolve_audit_path(
        audit_path or os.environ.get(
            "MEMORY_DRIFT_AUDIT_PATH", "memory/config_drift_audit.jsonl",
        ),
    )
    if not path.exists():
        return []
    events: list[AuditEvent] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    d: dict = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if d.get("timestamp", 0) < since_ts:
                    continue
                if decision_filter and d.get("decision") != decision_filter:
                    continue
                events.append(AuditEvent(
                    timestamp=d.get("timestamp", 0.0),
                    scope=d.get("scope", ""),
                    decision=d.get("decision", ""),
                    tier=d.get("tier", ""),
                    flag=d.get("flag", ""),
                    mode=d.get("mode", ""),
                    operator_id=d.get("operator_id", ""),
                    reason=d.get("reason", ""),
                    progression_hits=d.get("progression_hits", 0),
                    policy_hash=d.get("policy_hash", ""),
                    extra=d.get("extra", {}),
                ))
    except OSError as e:
        logger.warning("audit: read failed: %s", e)
    return events[-limit:]