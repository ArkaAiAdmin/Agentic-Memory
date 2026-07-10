#!/usr/bin/env python3
"""Regenerate the computed fields of docs/_meta.json from live code.

Reads the existing docs/_meta.json, recomputes the fields that
``verify_doc_meta.py`` checks against live code, writes them back in
place, sets ``last_verified`` to today's date, and preserves any other
fields (e.g. ``num_agents_ported``) that are not derived from code.

Usage:
    python scripts/gen_doc_meta.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
META_PATH = REPO_ROOT / "docs" / "_meta.json"

# Fields recomputed from live code (mirrors verify_doc_meta._get_live_values).
COMPUTED_FIELDS = [
    "schema_version",
    "num_migrations",
    "num_core_tools",
    "num_admin_tools",
    "num_cron_scripts",
    "num_tests",
    "loc_production",
    "loc_test",
    "loc_total",
]


def main() -> int:
    sys.path.insert(0, str(SCRIPTS_DIR))
    sys.path.insert(0, str(REPO_ROOT))
    from verify_doc_meta import _get_live_values  # type: ignore[import-untyped]

    live = _get_live_values()

    meta: dict = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text())

    for field in COMPUTED_FIELDS:
        if live.get(field) is not None:
            meta[field] = live[field]

    # Keep derived tool totals consistent with the updated counts.
    core = meta.get("num_core_tools")
    admin = meta.get("num_admin_tools")
    if core is not None and admin is not None:
        try:
            from tool_registry import DEPRECATED  # type: ignore[import-untyped]

            meta["num_deprecated_tools"] = len(DEPRECATED)
        except ImportError:
            pass
        dep = meta.get("num_deprecated_tools", 0)
        meta["num_total_tools"] = core + admin + (dep or 0)

    # Mark freshness without clobbering any non-computed fields.
    meta["last_verified"] = date.today().isoformat()

    META_PATH.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {META_PATH} (last_verified={meta['last_verified']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
