#!/usr/bin/env python3
"""Verify tool_registry.py matches @mcp.tool() definitions in mcp_*.py.

Usage: python scripts/tool_drift_check.py
Exit: 0 if match, 1 if drift detected.
"""
from pathlib import Path
import re
import sys

# Tools registered in ADMIN_TOOLS but intentionally routed through
# memory_maintenance without their own @mcp.tool() decorator.
# These are not missing from the MCP surface — they are reachable via
# memory_maintenance(operation="...") just like memory_graph_insights
# and memory_graph_evolution.
ADMIN_ROUTED_TOOLS = {
    "memory_recall_status",
    "memory_recall_trace",
    "memory_recall_stats",
    "memory_gdpr_erase",
    "memory_pipeline_coverage",
}


def extract_mcp_tools_from_files() -> set[str]:
    """Extract all @mcp.tool() function names from mcp_*.py files."""
    tools = set()
    for mcp_file in Path(".").glob("mcp_*.py"):
        if mcp_file.name == "mcp_maintenance_ops.py":
            continue  # This file doesn't define @mcp tools
        content = mcp_file.read_text(encoding="utf-8")
        # Match @mcp.tool() followed by any number of decorators, then def function_name(
        # The pattern handles:
        # @mcp.tool()
        # @other_decorator
        # def function_name(
        pattern = r"@mcp\.tool\(\)(?:\s*\n\s*@\w+(?:\([^)]*\))?)*\s*\n\s*def\s+(\w+)\s*\("
        for match in re.finditer(pattern, content):
            tools.add(match.group(1))
    return tools


def main() -> int:
    # Parse tool_registry.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tool_registry import CORE_TOOLS, ADMIN_TOOLS

    registered = set(CORE_TOOLS + ADMIN_TOOLS) - ADMIN_ROUTED_TOOLS

    # Extract tools from source files
    defined = extract_mcp_tools_from_files()

    # Find drift
    missing_in_registry = defined - registered
    extra_in_registry = registered - defined

    if missing_in_registry or extra_in_registry:
        print("DRIFT DETECTED:")
        if missing_in_registry:
            print(f"  Tools defined in mcp_*.py but MISSING from tool_registry.py ({len(missing_in_registry)}):")
            for t in sorted(missing_in_registry):
                print(f"    {t}")
        if extra_in_registry:
            print(f"  Tools in tool_registry.py but NOT defined in mcp_*.py ({len(extra_in_registry)}):")
            for t in sorted(extra_in_registry):
                print(f"    {t}")
        return 1

    print(f"OK: {len(defined)} tools match between mcp_*.py and tool_registry.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())