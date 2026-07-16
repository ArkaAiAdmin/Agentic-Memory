#!/usr/bin/env python3
"""Tests for tool_registry.py — the source of truth for MCP tool tiers.

Verifies:
  1. CORE/ADMIN/DEPRECATED counts match documentation
  2. No duplicate tool names across tiers
  3. All CORE tools have valid names (lowercase, underscores only)
  4. DEPRECATED tools are a subset of ADMIN_TOOLS
"""

import sys
from pathlib import Path

# Make the package importable.
_WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKTREE))

import tool_registry  # noqa: E402


def test_core_count():
    """CORE_TOOLS has 22 entries (matches AGENTS.md and pyproject.toml)."""
    assert len(tool_registry.CORE_TOOLS) == 22, (
        f"Expected 22 CORE tools, got {len(tool_registry.CORE_TOOLS)}"
    )


def test_admin_count():
    """ADMIN_TOOLS has 92 entries (matches AGENTS.md)."""
    assert len(tool_registry.ADMIN_TOOLS) == 92, (
        f"Expected 92 ADMIN tools, got {len(tool_registry.ADMIN_TOOLS)}"
    )


def test_deprecated_count():
    """DEPRECATED has 3 entries."""
    assert len(tool_registry.DEPRECATED) == 3, (
        f"Expected 3 DEPRECATED tools, got {len(tool_registry.DEPRECATED)}"
    )


def test_no_duplicates_across_tiers():
    """No tool name appears in more than one tier."""
    all_names = []
    for tier_name, tier_list in [
        ("CORE", tool_registry.CORE_TOOLS),
        ("ADMIN", tool_registry.ADMIN_TOOLS),
        ("DEPRECATED", tool_registry.DEPRECATED),
    ]:
        for name in tier_list:
            all_names.append((tier_name, name))

    seen = {}
    for tier, name in all_names:
        if name in seen:
            # DEPRECATED ⊂ ADMIN is allowed
            if {tier, seen[name]} == {"DEPRECATED", "ADMIN"}:
                continue
            raise AssertionError(
                f"Duplicate tool '{name}' in {seen[name]} and {tier}"
            )
        seen[name] = tier


def test_deprecated_subset_of_admin():
    """All DEPRECATED tools are also in ADMIN_TOOLS."""
    admin_set = set(tool_registry.ADMIN_TOOLS)
    for name in tool_registry.DEPRECATED:
        assert name in admin_set, (
            f"DEPRECATED tool '{name}' not found in ADMIN_TOOLS"
        )


def test_core_names_valid():
    """All CORE tool names are valid identifiers (lowercase, underscores)."""
    import re
    pattern = re.compile(r"^memory_[a-z_]+$")
    for name in tool_registry.CORE_TOOLS:
        assert pattern.match(name), (
            f"CORE tool '{name}' doesn't match pattern memory_[a-z_]+"
        )


def test_admin_names_valid():
    """All ADMIN tool names are valid identifiers (lowercase, underscores)."""
    import re
    pattern = re.compile(r"^memory_[a-z_]+$")
    for name in tool_registry.ADMIN_TOOLS:
        assert pattern.match(name), (
            f"ADMIN tool '{name}' doesn't match pattern memory_[a-z_]+"
        )


def test_total_visible():
    """Total visible tools (CORE only) is 22 after promoting the 3 self-editing
    skill tools (memory_list_skills, memory_extract_skills, memory_compile_skill)
    from ADMIN to CORE. See AGENTS.md 'Agent Self-Editing' section.
    """
    assert len(tool_registry.CORE_TOOLS) == 22


def test_total_tool_count():
    """Total unique tools across CORE + ADMIN (DEPRECATED ⊂ ADMIN, not counted separately)."""
    unique = set(tool_registry.CORE_TOOLS) | set(tool_registry.ADMIN_TOOLS)
    # DEPRECATED ⊂ ADMIN, so unique = CORE ∪ ADMIN
    assert len(unique) == 114, (
        f"Expected 114 unique tools (22 CORE + 92 ADMIN), got {len(unique)}"
    )


def test_all_admin_tools_reachable_via_router():
    """Every ADMIN_TOOLS name maps to a MaintenanceOp enum + handler.

    The `memory_maintenance(operation="...")` router is the only supported
    path to ADMIN tools (Hard Rule 6). Each tool name must have a matching
    MaintenanceOp enum member and a callable handler in MAINTENANCE_HANDLERS.

    A few ADMIN_TOOLS names use a different prefix than the MaintenanceOp
    value (e.g. memory_pinned_decay_check → pinned_decay); those are mapped
    explicitly below rather than via the naive "strip memory_" rule.
    """
    from mcp_maintenance import MaintenanceOp
    from mcp_maintenance_ops import MAINTENANCE_HANDLERS

    # ADMIN_TOOLS name → MaintenanceOp value (overrides naive prefix strip)
    _OP_ALIASES = {
        "memory_pinned_decay_check": "pinned_decay",
        "memory_run_tier_migration": "tier_migration",
        "memory_check_embedding_model": "embedding_model_check",
        "memory_admin_policy_hash": "policy_hash_status",
    }

    # memory_maintenance itself is the router, not a routed op
    admin_ops = [t for t in tool_registry.ADMIN_TOOLS if t != "memory_maintenance"]

    missing_enum = []
    missing_handler = []
    for name in admin_ops:
        op_value = _OP_ALIASES.get(name, name[len("memory_"):])
        try:
            op_enum = MaintenanceOp(op_value)
        except ValueError:
            missing_enum.append(name)
            continue
        if op_enum not in MAINTENANCE_HANDLERS:
            missing_handler.append(name)

    assert not missing_enum, (
        f"ADMIN tools without MaintenanceOp enum: {missing_enum}"
    )
    assert not missing_handler, (
        f"ADMIN tools without handler: {missing_handler}"
    )


def test_pipeline_coverage_registered():
    """pipeline_coverage is a registered ADMIN op with a working handler."""
    from mcp_maintenance import MaintenanceOp
    from mcp_maintenance_ops import MAINTENANCE_HANDLERS

    assert "memory_pipeline_coverage" in tool_registry.ADMIN_TOOLS
    op_enum = MaintenanceOp("pipeline_coverage")
    assert op_enum in MAINTENANCE_HANDLERS
    assert callable(MAINTENANCE_HANDLERS[op_enum])
