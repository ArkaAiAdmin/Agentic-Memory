#!/usr/bin/env python3
"""Generate TypeScript toolNames definitions from tool_registry.py and guard IDE contract.

Usage:
    venv/bin/python scripts/gen_ide_memory_tools.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tool_registry  # noqa: E402


def generate_ts() -> Path:
    out_dir = REPO_ROOT / "ide" / "packages" / "memory-bridge" / "src" / "__generated__"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "toolNames.ts"

    core_list_json = json.dumps(tool_registry.CORE_TOOLS, indent=2)
    admin_list_json = json.dumps(tool_registry.ADMIN_TOOLS, indent=2)
    deprecated_list_json = json.dumps(tool_registry.DEPRECATED, indent=2)

    content = f"""/**
 * AUTO-GENERATED from tool_registry.py by scripts/gen_ide_memory_tools.py
 * DO NOT EDIT MANUALLY.
 */

export const CORE_TOOL_NAMES = {core_list_json} as const;

export type CoreToolName = typeof CORE_TOOL_NAMES[number];

export const ADMIN_TOOL_NAMES = {admin_list_json} as const;

export type AdminToolName = typeof ADMIN_TOOL_NAMES[number];

export const DEPRECATED_TOOL_NAMES = {deprecated_list_json} as const;

export type DeprecatedToolName = typeof DEPRECATED_TOOL_NAMES[number];
"""
    out_file.write_text(content, encoding="utf-8")
    return out_file


def verify_ide_contract() -> None:
    # Backend tool operations called by desktop memoryTools.ts and memory-bridge client.ts
    ide_tool_ops = {
        "memory_search",
        "memory_save",
        "memory_coordinate",
        "memory_share",
        "memory_agent_list",
        "memory_agent_init",
        "memory_recall_context",
        "memory_note",
        "memory_audit",
        "memory_skills",
        "memory_session_start",
        "memory_session_end",
        "memory_graph",
        "memory_review_beliefs",
    }
    all_known = (
        set(tool_registry.CORE_TOOLS)
        | set(tool_registry.ADMIN_TOOLS)
        | set(tool_registry.DEPRECATED)
        | {"memory_advanced"}
    )
    missing = ide_tool_ops - all_known
    if missing:
        raise RuntimeError(f"IDE contract violation: unknown backend tool operations: {missing}")


def main() -> int:
    ts_path = generate_ts()
    verify_ide_contract()
    print(f"Generated {ts_path.relative_to(REPO_ROOT)} and verified IDE tool contract.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
