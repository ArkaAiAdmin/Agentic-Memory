#!/usr/bin/env python3
"""Daily config-drift surveillance cron.

Compares today's drift report to yesterday's; emits a structured alert for
any new INTEGRITY- or STABILITY-tier drift.  Writes a JSON summary to
memory/.drift_cron_<date>.json so on-call tooling can read it.
"""
import argparse
import datetime
import json
import logging
import os
import sys

os.environ.setdefault("MEMORY_KNOWLEDGE_GRAPH", "1")
# This cron is a drift *surveillance* tool: it detects and reports drift
# rather than failing at import time. Explicit fail-fast is available via
# the --enforce-scope flag, which calls enforce() directly.
os.environ.setdefault("MEMORY_CONFIG_DRIFT_SKIP_ENFORCEMENT", "1")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

# Lock — don't overlap with other config-drift runs
try:
    from _flock import acquire_lock_or_exit
except ImportError:
    def acquire_lock_or_exit(name: str, max_attempts: int = 5) -> None:
        logger.error("cron_check_config_drift: _flock module not available, cannot acquire lock")
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily config-drift surveillance.")
    parser.add_argument("--severity-floor", default="stability",
                        choices=["neutral", "operational", "compliance", "stability", "integrity"],
                        help="Minimum severity to alert on. Default: stability.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute report but don't persist or alert.")
    parser.add_argument("--alert-stdout", action="store_true",
                        help="Print alert to stdout (for cron log capture).")
    parser.add_argument("--enforce-scope", action="store_true",
                        help="Fail-fast on drift before computing report. "
                             "Exits 78 if drift detected at enforcement level.")
    parser.add_argument("--reload-policy", action="store_true",
                        help="Reload policy cache and reapply tier overrides "
                             "from memory.toml before building drift report.")
    parser.add_argument("--apply-tier-patches", action="store_true",
                        help="Reload policy cache and reapply tier overrides "
                             "from memory.toml before building drift report.")
    args = parser.parse_args()

    if args.reload_policy or args.apply_tier_patches:
        try:
            from infra.config_drift_policy import reset_policy_cache
            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml
            from infra.config import _read_toml, _TOML_PATH
            reset_policy_cache()
            if _TOML_PATH.exists():
                apply_tier_overrides_from_toml(_read_toml(_TOML_PATH))
        except Exception as e:
            logger.warning("cron: tier/policy reload failed: %s", e)

    if args.enforce_scope:
        from infra.config_drift import build_drift_report
        from infra.config_drift_policy import enforce, DriftEnforcementError
        try:
            enforce(build_drift_report(), verb="admin")
        except DriftEnforcementError as e:
            print(json.dumps(e.to_dict(), indent=2))
            sys.exit(78)

    acquire_lock_or_exit("cron_check_config_drift")

    from infra.config_drift import (
        build_drift_report, persist_drift_report, load_last_drift_snapshot,
        diff_reports, DriftSeverity,
    )
    from infra.memory_common import atomic_write
    from infra.infrastructure import resolve_active_memory_dir

    current = build_drift_report()
    prev = load_last_drift_snapshot()
    new_drift = diff_reports(prev, current)

    eligible_alerts = [
        d for d in new_drift
        if any(sev in d for sev in (
            f"[{DriftSeverity.INTEGRITY.value}]",
            f"[{DriftSeverity.STABILITY.value}]",
        ))
    ]

    summary: dict = {
        "schema_version": 1,
        "run_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "total_flags": current.total_flags,
        "drift_count_by_severity": current.drift_count_by_severity,
        "delta_count": len(eligible_alerts),
        "delta_alerts": eligible_alerts,
        "host": current.host,
        "agent_id": current.agent_id,
    }

    if not args.dry_run:
        persist_drift_report(current)
        mem_dir = resolve_active_memory_dir()
        archive_path = mem_dir / f".drift_cron_{datetime.date.today().isoformat()}.json"
        atomic_write(archive_path, json.dumps(summary, indent=2))
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
        for p in mem_dir.glob(".drift_cron_*.json"):
            try:
                file_date_str = p.stem[len(".drift_cron_"):]
                file_date = datetime.date.fromisoformat(file_date_str)
                if datetime.datetime.combine(file_date, datetime.time(0), tzinfo=datetime.timezone.utc) < cutoff:
                    p.unlink()
            except (ValueError, OSError) as e:
                logger.debug("drift cron: failed to prune %s: %s", p, e)

    if args.alert_stdout or eligible_alerts:
        for line in eligible_alerts:
            print(line, file=sys.stdout)

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
