#!/usr/bin/env python3
"""Runtime tier-patch hook.

Reads JSON from stdin:
  {"flag": "MEMORY_X", "tier": "compliance"}  -> set/override a flag's tier
  {"flag": "MEMORY_Y", "tier": null}          -> remove the override (restore built-in)
  {"reset": true}                             -> restore all hardcoded defaults

Builds a synthetic {"drift_tiers": {flag: tier_or_empty}} and delegates to
apply_tier_overrides_from_toml. Prints {"patched": [...], "rejected": [...]}.
For reset, _FLAG_TIERS is rebuilt from _HARDCODE_DEFAULTS.

Import-safe: all side effects live inside main() / the __main__ guard.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import infra._bootstrap_path  # noqa: F401

from infra.config_drift import DriftSeverity, _FLAG_TIERS
from infra.config_drift_tier_patch import (
    apply_tier_overrides_from_toml,
    _HARDCODE_DEFAULTS,
)


def main() -> int:
    payload: dict = {}
    try:
        raw = json.load(sys.stdin)
        payload = raw if isinstance(raw, dict) else {}
    except Exception:
        pass

    patched: list[dict] = []
    rejected: list[dict] = []

    if payload.get("reset"):
        # Restore every built-in default and drop runtime-added keys.
        for k, v in _HARDCODE_DEFAULTS.items():
            _FLAG_TIERS[k] = v
        for k in list(_FLAG_TIERS.keys()):
            if k not in _HARDCODE_DEFAULTS:
                _FLAG_TIERS.pop(k, None)
        patched = [
            {
                "flag": k,
                "tier": v.value if isinstance(v, DriftSeverity) else str(v),
            }
            for k, v in _HARDCODE_DEFAULTS.items()
        ]
    else:
        flag = (payload.get("flag") or "").strip().upper()
        tier = payload.get("tier")
        if not flag:
            rejected.append({"flag": "", "value": "", "reason": "missing flag"})
        else:
            tier_value = "" if tier is None else tier
            result = apply_tier_overrides_from_toml(
                {"drift_tiers": {flag: tier_value}}
            )
            patched = [
                {
                    "flag": p.env_key,
                    "tier": (p.new_tier.value if p.new_tier else None),
                }
                for p in result.patched
            ]
            rejected = [
                {"flag": f[0], "value": f[1], "reason": f[2]}
                for f in result.rejected
            ]

    print(json.dumps(
        {"schema_version": 1, "patched": patched, "rejected": rejected},
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
