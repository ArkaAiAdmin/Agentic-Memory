"""Feature-flag drift detection — typed decomposition, severity-tiered alerts.

Three responsibilities:
  1. Decompose every flag into default / toml / env / effective.
  2. Apply per-tier drift verdicts against the decomposition.
  3. Persist / load snapshots atomically so cron can compute deltas.

Pure data layer (no cron, no MCP).  Consumers: mcp_maintenance_ops._op_config_drift,
cron/cron_check_config_drift.py, memory_health_check.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from infra.config import (
    _resolve,
    _TOML_PATH,
    _read_toml,
    _deep_get,
    get_config,
    _INTEGRITY_CRITICAL_FLAGS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Severity tiers — drive alert priority.
# ---------------------------------------------------------------------------


class DriftSeverity(str, Enum):
    INTEGRITY = "integrity"       # saga, journal, crdt, quality_gates → data loss risk
    STABILITY = "stability"       # db_path, pool_size, flock → reliability
    COMPLIANCE = "compliance"     # retention, audit → audit trail completeness
    OPERATIONAL = "operational"   # retries, timeouts, embeddings → throughput
    NEUTRAL = "neutral"           # search weights, decay curves → cosmetic


# Per-flag tier table.  Default for unlisted flags is NEUTRAL.
_FLAG_TIERS: dict[str, DriftSeverity] = {
    "MEMORY_SAGA_ENABLED":            DriftSeverity.INTEGRITY,
    "MEMORY_CRDT_ENABLED":            DriftSeverity.INTEGRITY,
    "MEMORY_WRITE_JOURNAL_ENABLED":   DriftSeverity.INTEGRITY,
    "MEMORY_QUALITY_GATES":           DriftSeverity.INTEGRITY,
    "MEMORY_TEMPORAL_KG":             DriftSeverity.INTEGRITY,
    "MEMORY_BELIEF_LAYER":            DriftSeverity.INTEGRITY,
    "MEMORY_DB_PATH":                 DriftSeverity.STABILITY,
    "MEMORY_DB_POOL_SIZE":            DriftSeverity.STABILITY,
    "MEMORY_DB_FLOCK":                DriftSeverity.STABILITY,
    "MEMORY_WAL_CHECKPOINT_STARTUP":  DriftSeverity.STABILITY,
    "MEMORY_WAL_CHECKPOINT_INTERVAL_S": DriftSeverity.STABILITY,
    "MEMORY_AUDIT_LOGGING":           DriftSeverity.COMPLIANCE,
    "MEMORY_ADAPTIVE_RETENTION":      DriftSeverity.COMPLIANCE,
    "MEMORY_RETENTION_DAYS":          DriftSeverity.COMPLIANCE,
    "MEMORY_ACCESS_LOGGING_ENABLED":  DriftSeverity.COMPLIANCE,
    "MEMORY_EMBEDDING_BACKEND":       DriftSeverity.OPERATIONAL,
    "MEMORY_RERANKER_DISABLED":       DriftSeverity.OPERATIONAL,
    "MEMORY_RECONCILER_N_WORKERS":    DriftSeverity.OPERATIONAL,
    "MEMORY_DB_CONNECT_TIMEOUT_S":    DriftSeverity.OPERATIONAL,
    # NEUTRAL: search weights / decay curves — cosmetic, no enforcement posture.
    # Explicitly enumerated (not just the implicit default) so the NEUTRAL
    # tier is never empty; an empty tier would be a dead, meaningless
    # enforcement band in the drift framework.
    "MEMORY_RERANK_WEIGHTS":          DriftSeverity.NEUTRAL,
    "MEMORY_QUERY_TYPE_WEIGHTS":      DriftSeverity.NEUTRAL,
    "MEMORY_FORGETTING_CURVE_HALF_LIFE": DriftSeverity.NEUTRAL,
    "MEMORY_TEMPORAL_HALF_LIFE":      DriftSeverity.NEUTRAL,
    "MEMORY_TEMPORAL_DECAY_MODE":     DriftSeverity.NEUTRAL,
}

# Snapshot of the ORIGINAL built-in tier table, captured at import time
# BEFORE any [drift_tiers] TOML override is applied. Used by
# config_drift_tier_patch to restore hardcoded defaults on override removal.
# Values are immutable DriftSeverity enums, so a shallow copy is sufficient.
_HARDCODE_DEFAULTS: dict[str, DriftSeverity] = dict(_FLAG_TIERS)


def set_flag_tier(env_key: str, severity: DriftSeverity) -> None:
    """Extend the tier table at runtime. Used by deployment-specific code."""
    _FLAG_TIERS[env_key] = severity


def reset_flag_tiers() -> None:
    """Restore _FLAG_TIERS to the canonical hardcoded defaults (drops TOML/runtime overrides)."""
    _FLAG_TIERS.clear()
    _FLAG_TIERS.update(_HARDCODE_DEFAULTS)


def _tier_for(env_key: str) -> DriftSeverity:
    return _FLAG_TIERS.get(env_key, DriftSeverity.NEUTRAL)


# ---------------------------------------------------------------------------
# Per-flag decomposition dataclasses.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlagSource:
    """The four sources for a single flag, decomposed."""
    effective: Any
    default: Any
    toml_value: Any
    env_raw: Optional[str]
    source: str  # "env" | "toml" | "default" | "env_invalid" | "toml_invalid"

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("effective", "default", "toml_value"):
            v = d[k]
            if isinstance(v, Enum):
                d[k] = v.value
        return d


@dataclass(frozen=True)
class DriftEntry:
    """Computed verdict for one flag."""
    flag: str
    toml_path: str | None
    severity: str
    sources: FlagSource
    drift_verdicts: list[str] = field(default_factory=list)
    effective_hash: str = ""

    def has_drift(self) -> bool:
        return bool(self.drift_verdicts)


@dataclass(frozen=True)
class DriftReport:
    """A full snapshot of all flags."""
    generated_at: float
    schema_version: int  # 1
    host: str
    agent_id: str
    entries: list[DriftEntry]
    total_flags: int
    drift_count_by_severity: dict[str, int]

    def critical_drift(self) -> list[DriftEntry]:
        return [e for e in self.entries
                if e.severity == DriftSeverity.INTEGRITY.value and e.has_drift()]

    def has_any_drift(self) -> bool:
        return any(e.has_drift() for e in self.entries)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "host": self.host,
            "agent_id": self.agent_id,
            "total_flags": self.total_flags,
            "drift_count_by_severity": self.drift_count_by_severity,
            "entries": [_entry_to_dict(e) for e in self.entries],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, Enum):
        return o.value
    return str(o)


def _entry_to_dict(e: DriftEntry) -> dict:
    d = asdict(e)
    d["sources"] = e.sources.as_dict()
    return d


# ---------------------------------------------------------------------------
# The flag registry — single source of truth.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlagSpec:
    """Operator-facing metadata for one flag."""
    env_var: str
    toml_path: str | None
    default: Any
    py_type: type = field(default_factory=lambda: type(None))


def _all_flag_specs() -> list[FlagSpec]:
    """Enumerate every flag the system reads.

    Two sources:
      1. ``get_feature_flags()`` for [features] / [search] / [cache] flags.
      2. Hardcoded operational list for db_path / pool_size / flock / etc.
    """
    from infra.config import get_feature_flags
    specs: list[FlagSpec] = []
    for name, meta in get_feature_flags().items():
        specs.append(FlagSpec(
            env_var=meta["env_var"],
            toml_path=meta["toml_path"],
            default=meta["default"],
            py_type=bool,
        ))
    operational = [
        ("MEMORY_DB_PATH",                  "general.db_path",              "memory/memory.db", str),
        ("MEMORY_DB_POOL_SIZE",             "general.db_pool_size",         24, int),
        ("MEMORY_DB_FLOCK",                 None,                           True, bool),
        ("MEMORY_WAL_CHECKPOINT_STARTUP",   "general.wal_checkpoint_startup", True, bool),
        ("MEMORY_WAL_CHECKPOINT_INTERVAL_S","general.wal_checkpoint_interval_s", 300, int),
        ("MEMORY_DB_CONNECT_TIMEOUT_S",     "general.db_connect_timeout_s", 30, int),
        ("MEMORY_RERANKER_DISABLED",        "search.reranker_disabled",     False, bool),
        ("MEMORY_RECONCILER_N_WORKERS",     None,                           1, int),
        ("MEMORY_JOURNAL_MAX_RETRIES",      None,                           3, int),
    ]
    for env_var, toml_path, default, py_t in operational:
        specs.append(FlagSpec(
            env_var=env_var, toml_path=toml_path, default=default, py_type=py_t,
        ))
    return specs


# ---------------------------------------------------------------------------
# Decomposition per spec.
# ---------------------------------------------------------------------------


def _decompose(spec: FlagSpec) -> FlagSource:
    """Pull all four sources for one flag from the live process state."""
    toml_data = _read_toml(_TOML_PATH) if _TOML_PATH.exists() else {}
    env_raw = os.environ.get(spec.env_var)
    toml_path = spec.toml_path or ""
    toml_value = _deep_get(toml_data or {}, toml_path) if toml_path else None

    parse_failed = False
    try:
        effective = _resolve(spec.env_var, toml_path, spec.default, toml_data) if toml_path else spec.default
    except Exception as exc:
        logger.warning("drift: resolve failed for %s: %s", spec.env_var, exc)
        effective = spec.default
        parse_failed = True

    if env_raw is not None:
        source = "env"
        if parse_failed:
            source = "env_invalid"
    elif toml_value is not None:
        source = "toml"
        if not _is_type_compatible(toml_value, spec.default):
            source = "toml_invalid"
    else:
        source = "default"

    return FlagSource(
        effective=effective,
        default=spec.default,
        toml_value=toml_value,
        env_raw=env_raw,
        source=source,
    )


def _is_type_compatible(value: Any, default: Any) -> bool:
    """Loose type check — TOML's int→float and bool→int relaxations allowed."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int) and not isinstance(default, bool):
        return isinstance(value, (int, bool))
    if isinstance(default, float):
        return isinstance(value, (int, float))
    if isinstance(default, str):
        return isinstance(value, str)
    if default is None:
        return value is None or isinstance(value, dict)
    return True


# ---------------------------------------------------------------------------
# Verdict rules
# ---------------------------------------------------------------------------


def _verdicts(spec: FlagSpec, src: FlagSource) -> list[str]:
    """Apply all verdict rules to one flag."""
    verdicts: list[str] = []

    if src.env_raw is not None and src.toml_value is not None:
        try:
            env_typed = _coerce_typed(src.env_raw, spec.py_type, spec.default)
        except Exception:
            env_typed = src.env_raw
        if str(env_typed) != str(src.toml_value):
            verdicts.append(
                f"source_conflict: env={src.env_raw!r} (typed={env_typed!r}) "
                f"vs toml={src.toml_value!r}"
            )

    if src.source == "env_invalid":
        verdicts.append(
            f"parse_failure: env={src.env_raw!r} could not be coerced to {spec.py_type.__name__}"
        )
    if src.source == "toml_invalid":
        verdicts.append(
            f"type_mismatch: toml={src.toml_value!r} has wrong type for {spec.py_type.__name__}"
        )

    if src.env_raw is not None and src.effective != spec.default:
        verdicts.append(
            f"override_from_default: effective={src.effective!r} (default was {spec.default!r})"
        )

    if src.env_raw is not None and src.effective == spec.default:
        try:
            env_normalized = _coerce_typed(src.env_raw, spec.py_type, spec.default)
        except Exception:
            env_normalized = src.env_raw
        if env_normalized != spec.default:
            verdicts.append(
                f"explicit_default_via_env_mismatch: env={src.env_raw!r} but effective={src.effective!r}"
            )

    if spec.env_var in _INTEGRITY_CRITICAL_FLAGS and src.effective is False:
        verdicts.append("INTEGRITY_CRITICAL_DISABLED: data-loss risk window open")

    return verdicts


def _coerce_typed(raw: str, py_type: type, default: Any) -> Any:
    """Replicate _resolve() coercion for parse-failure detection."""
    if py_type is bool or isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if py_type is int or isinstance(default, int):
        return int(raw)
    if py_type is float or isinstance(default, float):
        return float(raw)
    return raw


def _hash_effective(value: Any) -> str:
    """Stable hash for delta detection across cron runs."""
    return hashlib.sha256(repr(value).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Top-level report builder.
# ---------------------------------------------------------------------------


def build_drift_report() -> DriftReport:
    """Compute a DriftReport from live process state. Pure function (no I/O)."""
    specs = _all_flag_specs()
    cfg = get_config()
    agent_id = getattr(cfg.general, "agent_id", "") or ""

    entries: list[DriftEntry] = []
    drift_count: dict[str, int] = {s.value: 0 for s in DriftSeverity}

    for spec in specs:
        sources = _decompose(spec)
        verdicts = _verdicts(spec, sources)
        severity = _tier_for(spec.env_var).value

        if verdicts:
            drift_count[severity] += 1

        entries.append(DriftEntry(
            flag=spec.env_var,
            toml_path=spec.toml_path,
            severity=severity,
            sources=sources,
            drift_verdicts=verdicts,
            effective_hash=_hash_effective(sources.effective),
        ))

    return DriftReport(
        generated_at=time.time(),
        schema_version=1,
        host=socket.gethostname(),
        agent_id=agent_id,
        entries=entries,
        total_flags=len(entries),
        drift_count_by_severity=drift_count,
    )


# ---------------------------------------------------------------------------
# Snapshot persistence
# ---------------------------------------------------------------------------

_DRIFT_SNAPSHOT_PATH = (
    Path(os.environ.get("MEMORY_INSTALL_ROOT", Path.home() / ".config" / "agentic-memory"))
    / "memory" / "last_drift_snapshot.json"
)


def persist_drift_report(report: DriftReport) -> Path:
    """Atomically write the snapshot. Reuses infra.memory_common.atomic_write."""
    from infra.memory_common import atomic_write
    _DRIFT_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(_DRIFT_SNAPSHOT_PATH, report.to_json())
    return _DRIFT_SNAPSHOT_PATH


def load_last_drift_snapshot() -> Optional[DriftReport]:
    """Load the most recent persisted snapshot. None if missing or unreadable."""
    if not _DRIFT_SNAPSHOT_PATH.exists():
        return None
    try:
        with open(_DRIFT_SNAPSHOT_PATH, "r") as f:
            data = json.load(f)
        if data.get("schema_version") != 1:
            logger.warning(
                "drift: snapshot schema_version=%s, expected 1; ignoring",
                data.get("schema_version"),
            )
            return None
        return _drift_report_from_dict(data)
    except Exception as exc:
        logger.warning("drift: failed to load snapshot: %s", exc)
        return None


def _drift_report_from_dict(d: dict) -> DriftReport:
    """Convert a JSON-loaded dict back into a DriftReport."""
    entries = []
    for e in d.get("entries", []):
        src = e.get("sources", {})
        entries.append(DriftEntry(
            flag=e["flag"],
            toml_path=e["toml_path"],
            severity=e["severity"],
            sources=FlagSource(**src),
            drift_verdicts=e.get("drift_verdicts", []),
            effective_hash=e.get("effective_hash", ""),
        ))
    return DriftReport(
        schema_version=d.get("schema_version", 1),
        generated_at=d.get("generated_at", 0.0),
        host=d.get("host", ""),
        agent_id=d.get("agent_id", ""),
        entries=entries,
        total_flags=d.get("total_flags", 0),
        drift_count_by_severity=d.get("drift_count_by_severity", {}),
    )


# ---------------------------------------------------------------------------
# Delta detection across snapshots.
# ---------------------------------------------------------------------------


def diff_reports(prev: Optional[DriftReport], current: DriftReport) -> list[str]:
    """Return human-readable diffs for new drift introduced since the
    last snapshot. Stable drift does NOT appear in the delta.
    """
    if prev is None:
        return [f"[{e.severity}] {e.flag}: {'; '.join(e.drift_verdicts)}"
                for e in current.entries if e.has_drift()]

    prev_by_flag = {e.flag: e for e in prev.entries}
    diffs: list[str] = []
    for e in current.entries:
        prev_entry = prev_by_flag.get(e.flag)
        if prev_entry is None:
            if e.has_drift():
                diffs.append(f"[{e.severity}] {e.flag}: NEW FLAG: {'; '.join(e.drift_verdicts)}")
            continue
        if prev_entry.drift_verdicts != e.drift_verdicts or prev_entry.effective_hash != e.effective_hash:
            if e.has_drift():
                diffs.append(
                    f"[{e.severity}] {e.flag}: {'; '.join(e.drift_verdicts)} "
                    f"(effective_hash: {prev_entry.effective_hash} -> {e.effective_hash})"
                )
            elif prev_entry.has_drift():
                diffs.append(f"[{e.severity}] {e.flag}: drift cleared")
    return diffs


# ---------------------------------------------------------------------------
# TOML tier override loading
# ---------------------------------------------------------------------------

def _apply_tier_overrides_from_toml() -> None:
    """Read [drift_tiers] from memory.toml and override _FLAG_TIERS.

    Unknown / malformed entries are logged and ignored — never raise,
    so a typo in someone's TOML doesn't break drift detection entirely.
    """
    try:
        if not _TOML_PATH.exists():
            return
        toml_data = _read_toml(_TOML_PATH)
        overrides = (toml_data.get("drift_tiers") or {})
        for key, value in overrides.items():
            env_key = key.upper() if key.islower() else key
            value_lower = str(value).strip().lower()
            tier = None
            for s in DriftSeverity:
                if s.value == value_lower:
                    tier = s
                    break
            if tier is None:
                logger.warning(
                    "drift: unknown tier %r for %s in memory.toml [drift_tiers]; ignoring",
                    value, env_key,
                )
                continue
            set_flag_tier(env_key, tier)
            logger.info("drift: tier override %s = %s (from memory.toml)", env_key, tier.value)
    except Exception as e:
        logger.warning("drift: failed to apply tier overrides: %s", e)


# Apply at import time — runs once when the module is first loaded.
# Can be suppressed by setting MEMORY_SKIP_IMPORT_TIER_OVERRIDE=1 (useful in tests
# to avoid side-effects from a corrupted or absent memory.toml at import time).
if os.environ.get("MEMORY_SKIP_IMPORT_TIER_OVERRIDE") is None:
    _apply_tier_overrides_from_toml()
