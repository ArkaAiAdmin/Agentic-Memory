#!/usr/bin/env python3
"""On-demand TOML reload hook.

Reads optional JSON from stdin (reason), calls reset_config +
reset_policy_cache + apply_tier_overrides_from_toml, returns before/after
policy_hashes as JSON.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import infra._bootstrap_path  # noqa: F401

from infra.config_drift_policy import reset_policy_cache
from infra.config import reset_config
from infra.config_drift_tier_patch import apply_tier_overrides_from_toml


def main() -> int:
    reason = ""
    try:
        raw = json.load(sys.stdin)
        reason = raw.get("reason", "") if isinstance(raw, dict) else ""
    except Exception:
        pass

    before_hash = ""
    try:
        from infra.config_drift_policy import resolve_policy
        before_hash = resolve_policy().policy_hash()
    except Exception:
        pass

    reset_config()
    reset_policy_cache()

    try:
        from infra.config import _read_toml, _TOML_PATH
        if _TOML_PATH.exists():
            apply_tier_overrides_from_toml(_read_toml(_TOML_PATH))
    except Exception as e:
        sys.stderr.write(f"tier reload warning: {e}\n")

    after_hash = ""
    try:
        from infra.config_drift_policy import resolve_policy
        after_hash = resolve_policy().policy_hash()
    except Exception:
        pass

    print(json.dumps({
        "schema_version": 1,
        "reason": reason,
        "before_policy_hash": before_hash,
        "after_policy_hash": after_hash,
        "changed": before_hash != after_hash,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
