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


def _get_test_count_from_pytest() -> int | None:
    """Run pytest --collect-only and parse the total test count."""
    import subprocess
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header", "eval/"],
            capture_output=True, text=True, timeout=120,
        )
        m = re.search(r"(\d+)\s+tests?\s+collected", result.stdout)
        if m:
            return int(m.group(1))
    except Exception:
        pass
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
            # Count only tool entries — skip comment lines (e.g. the
            # "# ... "Self-editing" section ..." note inside the list).
            return len([
                l for l in m.group(1).splitlines()
                if '"' in l and not l.strip().startswith("#")
            ])
    except Exception:
        pass
    return 0


def _count_cron_scripts() -> int:
    """Count scheduled cron jobs from the canonical ``JOBS`` registry.

    Uses the ``cron/jobs.py`` ``JOBS`` dict (the single source of truth for
    scheduled jobs) rather than globbing ``cron_*.py`` files, which also
    matches over helper/utility modules that are not scheduled jobs.
    """
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("cron_jobs", ROOT / "cron" / "jobs.py")
        if spec is None or spec.loader is None:
            return len(list((ROOT / "cron").glob("cron_*.py")))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return len(getattr(mod, "JOBS", {}))
    except Exception:
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
    test_count = _get_test_count_from_pytest() or _count_tests()
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
    print("Updated README.md:")
    print(f"  Schema: {schema}")
    print(f"  CORE tools: {core_tools}")
    print(f"  Tests: {test_count:,}+")
    print(f"  Cron scripts: {cron_count}")
    print(f"  Hooks: {hook_count}")
    print(f"  Tables: {table_count}")
    print(f"  Search phases: {phase_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
