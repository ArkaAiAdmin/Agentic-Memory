#!/usr/bin/env python3
"""tool_drift_check.py — detect drift between tool_registry.py and
the @mcp.tool() definitions in mcp_tools.py.

Returns non-zero exit code if drift is detected. Run from CI to
prevent the kind of drift we found in the 2026-06-15 audit:
8 phantom tools in the registry + 2 actual tools not in the registry.
"""

import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tool_registry


def main() -> int:
    # Scan all mcp_*.py files (but not mcp_tools.py itself which only has
    # comments about @mcp.tool()) for actual @mcp.tool() decorator definitions.
    pattern = re.compile(
        r"^@mcp\.tool\(\)"
        r"(?:\s*@\w+(?:\.\w+)*(?:\([^)]*\))?)*"  # 0-N decorators (with or without parens)
        r"\s*(?:async\s+)?def\s+(\w+)",
        re.MULTILINE,
    )
    defined: set[str] = set()
    for f in sorted(ROOT.glob("mcp_*.py")):
        if f.name == "mcp_tools.py":
            continue
        src = f.read_text()
        defined.update(pattern.findall(src))

    core = set(tool_registry.CORE_TOOLS)
    admin = set(tool_registry.ADMIN_TOOLS)
    listed = core | admin

    drift = []
    only_defined = defined - listed
    only_listed = (core | admin) - defined

    # Check tier placement: a tool is "core" if it should be in core
    # (no special reason to be hidden). Admin tools are those that
    # are explicitly admin.
    for t in only_defined:
        drift.append(("EXPOSED_BUT_NOT_LISTED", t, "actual @mcp.tool not in registry"))
    for t in only_listed:
        if t in core:
            drift.append(("LISTED_BUT_NOT_DEFINED_CORE", t, "in CORE but not defined"))
        else:
            drift.append(
                ("LISTED_BUT_NOT_DEFINED_ADMIN", t, "in ADMIN but not defined")
            )

    # Core/Admin tier sanity
    for t in defined:
        if t == "memory_maintenance":
            continue
        if t in core and t in admin:
            drift.append(("DUPLICATE", t, "in both core and admin"))
        elif t in core:
            pass  # ok
        elif t in admin:
            pass  # ok
        else:
            # not in either — this is the bug we want to flag
            drift.append(
                ("UNLISTED", t, "tool exists in mcp_tools.py but is not in registry")
            )

    if not drift:
        print(f"OK: {len(defined)} tools defined, all listed in registry.")
        return 0

    print(f"DRIFT: {len(drift)} issues between tool_registry and mcp_tools.py\n")
    by_kind: dict[str, Any] = {}
    for kind, name, msg in drift:
        by_kind.setdefault(kind, []).append((name, msg))
    for kind, items in by_kind.items():
        print(f"--- {kind} ({len(items)}) ---")
        for name, msg in items:
            print(f"  {name:<45}  {msg}")
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
