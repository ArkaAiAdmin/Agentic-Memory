#!/usr/bin/env python3
"""Regenerate dynamic badges and counts in README.md from live code.

Reads the current README, updates hardcoded badges (schema version,
CORE tools count, test count, LOC) from live source of truth, and
writes the file back.  Only touches badge lines — prose is preserved.

Usage:
    venv/bin/python scripts/gen_readme_badges.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def _count_tests() -> int:
    """Count test functions from eval/ directory."""
    count = 0
    for f in (ROOT / "eval").glob("test_*.py"):
        try:
            for line in f.read_text(errors="replace").splitlines():
                if line.strip().startswith("def test_"):
                    count += 1
        except Exception:
            pass
    return count


def _get_test_count_from_suite() -> int | None:
    """Read test count from the most recent full suite log."""
    import glob as glob_mod
    logs = sorted(glob_mod.glob("/tmp/full_suite*.log"), key=lambda p: Path(p).stat().st_mtime, reverse=True)
    for log in logs:
        try:
            text = Path(log).read_text(errors="replace")
            m = re.search(r"SUMMARY:\s*(\d+)p", text)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return None


def _count_loc(patterns: list[str], exclude_dirs: list[str] | None = None) -> int:
    """Count lines of code matching file patterns."""
    exclude = set(exclude_dirs or [])
    total = 0
    for pat in patterns:
        for f in ROOT.rglob(pat):
            if any(d in f.parts for d in exclude):
                continue
            if "venv" in f.parts or "__pycache__" in f.parts:
                continue
            try:
                total += sum(1 for _ in f.open(errors="replace"))
            except Exception:
                pass
    return total


def _get_schema_version() -> str:
    """Read SCHEMA_VERSION from infra/migration_runner.py."""
    try:
        text = (ROOT / "infra" / "migration_runner.py").read_text()
        m = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", text)
        if m:
            return f"v{m.group(1)}"
    except Exception:
        pass
    return "v?"


def _count_core_tools() -> int:
    """Count CORE tools from tool_registry.py."""
    try:
        text = (ROOT / "tool_registry.py").read_text()
        m = re.search(r"CORE_TOOLS\s*=\s*\[(.*?)\]", text, re.DOTALL)
        if m:
            return len([l for l in m.group(1).splitlines() if '"' in l])
    except Exception:
        pass
    return 0


def _count_cron_scripts() -> int:
    """Count cron/cron_*.py scripts."""
    return len(list((ROOT / "cron").glob("cron_*.py")))


def _count_hooks() -> int:
    """Count hook scripts."""
    return len(list((ROOT / "hooks").glob("*.py")))


def _count_tables() -> int:
    """Count visible tables from AGENTS.md auto-gen."""
    try:
        text = (ROOT / "AGENTS.md").read_text()
        m = re.search(r"num_tables_visible.*?(\d+)", text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0


def main() -> int:
    if not README.exists():
        print(f"README not found at {README}")
        return 1

    content = README.read_text()
    original = content

    schema = _get_schema_version()
    core_tools = _count_core_tools()
    test_count = _get_test_count_from_suite() or _count_tests()
    cron_count = _count_cron_scripts()
    hook_count = _count_hooks()
    table_count = _count_tables()

    # Update badge lines
    content = re.sub(
        r"(badge/tests-)[^-\"]+",
        f"\\g<1>{test_count:,}\\+",
        content,
    )
    content = re.sub(
        r"(badge/schema-)[^-]+",
        f"\\g<1>{schema}",
        content,
    )
    content = re.sub(
        r"(badge/MCP-)\d+(%20CORE)",
        f"\\g<1>{core_tools}\\2",
        content,
    )

    # Update mermaid diagram numbers
    content = re.sub(
        r"H\[\d+ MCP tools\]",
        f"H[{core_tools} MCP tools]",
        content,
    )
    content = re.sub(
        r"I\[\d+ cron scripts",
        f"I[{cron_count} cron scripts",
        content,
    )
    content = re.sub(
        r"J\[\d+ hooks\]",
        f"J[{hook_count} hooks]",
        content,
    )

    # Update search pipeline phase count in Features section
    # Count phases in the features table
    phase_count = len(re.findall(r"^\| \d+", content, re.MULTILINE))
    if phase_count > 0:
        content = re.sub(
            r"### Search — \d+-Phase Hybrid Pipeline",
            f"### Search — {phase_count}-Phase Hybrid Pipeline",
            content,
        )
        content = re.sub(
            r"\d+-Phase Search Pipeline",
            f"{phase_count}-Phase Search Pipeline",
            content,
        )

    if content == original:
        print("README badges already up to date.")
        return 0

    README.write_text(content)
    print(f"Updated README.md:")
    print(f"  Schema: {schema}")
    print(f"  CORE tools: {core_tools}")
    print(f"  Tests: {test_count:,}+")
    print(f"  Cron scripts: {cron_count}")
    print(f"  Hooks: {hook_count}")
    print(f"  Search phases: {phase_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
