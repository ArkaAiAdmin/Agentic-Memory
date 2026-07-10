#!/usr/bin/env python3
"""Regenerate docs/_meta.json from the single canonical live-code gatherer.

Reads all live meta from agents_md_generator.get_meta_for_json() (which
itself calls gather()), overlays onto existing _meta.json preserving
non-computed fields (provenance block, timestamps), and writes back.

Usage:
    python scripts/gen_doc_meta.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "infra"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agents_md_generator import get_meta_for_json  # sole source of truth

META_PATH = REPO_ROOT / "docs" / "_meta.json"

# Canonical live fields — must match keys returned by get_meta_for_json().
# Adding a new field to gather() requires adding it here too.
KNOWN_META_FIELDS = [
    "schema_version",
    "num_migrations",
    "num_mcp_modules",
    "num_core_tools",
    "num_admin_tools",
    "num_deprecated_tools",
    "num_total_tools",
    "num_cron_scripts",
    "num_hooks",
    "num_test_files",
    "num_test_functions",
    "num_tables_visible",
    "loc_production",
    "loc_test",
    "loc_total",
]

PROVENANCE_BLOCK = {
    "what_this_system_is_auto_gen_key": "what_this_system_is",
    "tool_surface_auto_gen_key": "mcp_surface_contract",
    "hard_rule_4_auto_gen_key": "hard_rule_4",
    "hard_rule_6_auto_gen_key": "hard_rule_6",
    "critical_path_auto_gen_key": "critical_path",
    "current_state_auto_gen_key": "current_state",
    "schema_doc": "docs/architecture.md",
    "mcp_doc": "docs/MCP_SURFACE.md",
    "truth_rank_1": "_meta.json (machine-enforced)",
    "truth_rank_2": "AGENTS.md AUTO-GEN sections (via agents_md_generator.py -> gen_doc_meta.py)",
    "truth_rank_3": "docs/MCP_SURFACE.md + docs/architecture.md (manual, cross-check via gen_schema_doc.py)",
    "last_meta_regenerated": date.today().isoformat(),
}


def main() -> int:
    live = get_meta_for_json()

    meta: dict = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text())

    for field in KNOWN_META_FIELDS:
        if live.get(field) is not None:
            meta[field] = live[field]

    if "provenance" not in meta:
        meta["provenance"] = {}
    for k, v in PROVENANCE_BLOCK.items():
        meta["provenance"].setdefault(k, v)
    meta["provenance"]["last_meta_regenerated"] = date.today().isoformat()
    meta["last_verified"] = date.today().isoformat()

    META_PATH.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {META_PATH} (last_verified={meta['last_verified']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
