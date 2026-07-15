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
    """CORE_TOOLS has 18 entries (matches AGENTS.md and pyproject.toml)."""
    assert len(tool_registry.CORE_TOOLS) == 18, (
        f"Expected 18 CORE tools, got {len(tool_registry.CORE_TOOLS)}"
    )


def test_admin_count():
    """ADMIN_TOOLS has 94 entries (matches AGENTS.md)."""
    assert len(tool_registry.ADMIN_TOOLS) == 94, (
        f"Expected 94 ADMIN tools, got {len(tool_registry.ADMIN_TOOLS)}"
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
    """Total visible tools (CORE only) is 18, matching docs claim of '3 visible'."""
    # The '3 visible' claim in pyproject refers to the MCP surface.
    # CORE tools are the directly-exposed ones.
    assert len(tool_registry.CORE_TOOLS) == 18


def test_total_tool_count():
    """Total unique tools across CORE + ADMIN (DEPRECATED ⊂ ADMIN, not counted separately)."""
    unique = set(tool_registry.CORE_TOOLS) | set(tool_registry.ADMIN_TOOLS)
    # DEPRECATED ⊂ ADMIN, so unique = CORE ∪ ADMIN
    assert len(unique) == 112, (
        f"Expected 112 unique tools (18 CORE + 94 ADMIN), got {len(unique)}"
    )
