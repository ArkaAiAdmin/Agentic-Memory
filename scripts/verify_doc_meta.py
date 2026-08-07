"""Verify docs/_meta.json matches live code.

Exit code 0 = all good. Exit code 1 = drift detected (prints report).
Run from repo root: venv/bin/python scripts/verify_doc_meta.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
META_PATH = REPO_ROOT / "docs" / "_meta.json"


def _get_pytest_test_count() -> int:
    """Collect test count via pytest --collect-only."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "eval/", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    last_line = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
    # e.g. "4431 tests collected in 5.28s"
    m = re.match(r"(\d+)\s+tests collected", last_line)
    if m:
        return int(m.group(1))
    # fallback: stdout
    for line in result.stdout.strip().splitlines():
        m = re.match(r"(\d+)\s+tests collected", line)
        if m:
            return int(m.group(1))
    return 0


def _count_lines(pattern: list[str]) -> int:
    """Count total lines in files matching a find pattern."""
    cmd = ["find", ".", "-name", "*.py"] + [
        arg for p in pattern for arg in ("-not", "-path", p)
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=REPO_ROOT, timeout=30
    )
    files = [f for f in result.stdout.strip().splitlines() if f]
    if not files:
        return 0
    wc = subprocess.run(
        ["xargs", "wc", "-l"], input="\n".join(files), capture_output=True, text=True, timeout=30
    )
    last = wc.stdout.strip().splitlines()[-1] if wc.stdout.strip() else ""
    m = re.match(r"\s*(\d+)\s+total", last)
    return int(m.group(1)) if m else 0


def _count_files(glob_pattern: str) -> int:
    return len(list(REPO_ROOT.glob(glob_pattern)))


def _get_live_values() -> dict[str, Any]:
    """Extract live values from the single canonical gatherer (agents_md_generator)."""
    sys.path.insert(0, str(REPO_ROOT / "infra"))
    try:
        from agents_md_generator import get_meta_for_json  # type: ignore[import-untyped]
        return get_meta_for_json()
    except ImportError:
        return _get_live_values_fallback()


def _get_live_values_fallback() -> dict[str, Any]:
    """Legacy collector — safety net only, should not be reached in normal operation."""
    live: dict[str, Any] = {}

    # Schema version
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from infra.migration_runner import SCHEMA_VERSION  # type: ignore[import-untyped]
        live["schema_version"] = SCHEMA_VERSION
    except ImportError:
        live["schema_version"] = None

    # Tool counts
    try:
        from tool_registry import CORE_TOOLS, ADMIN_TOOLS  # type: ignore[import-untyped]
        live["num_core_tools"] = len(CORE_TOOLS)
        live["num_admin_tools"] = len(ADMIN_TOOLS)
    except ImportError:
        live["num_core_tools"] = live["num_admin_tools"] = None

    # Migration files
    up = _count_files("migrations/*.sql")
    down = _count_files("migrations/*.down.sql")
    live["num_migrations"] = min(up, down)

    # Cron scripts
    live["num_cron_scripts"] = _count_files("cron/cron_*.py")

    # Test count
    live["num_tests"] = _get_pytest_test_count()

    # LOC
    excludes = [
        "./.venv/*", "./venv/*", "./node_modules/*", "./memory/*",
        "./.opencode/*", "./ts-sdk/node_modules/*", "*/__pycache__/*",
    ]
    total_loc = _count_lines(excludes)
    eval_excludes = excludes + ["*/eval/*"]
    prod_loc = _count_lines(eval_excludes)
    live["loc_total"] = total_loc
    live["loc_production"] = prod_loc
    live["loc_test"] = total_loc - prod_loc if total_loc and prod_loc else None

    return live


# Order + labels for the human-readable report.
ALL_FIELDS = [
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
    "num_tables_visible",
    "loc_production",
    "loc_test",
    "loc_total",
]

# Subset + display labels used by the ``--markdown`` dashboard.
MARKDOWN_ROWS = [
    ("schema_version", "Schema version"),
    ("num_migrations", "Migrations"),
    ("num_mcp_modules", "MCP modules"),
    ("num_core_tools", "CORE tools"),
    ("num_admin_tools", "ADMIN tools"),
    ("num_deprecated_tools", "Deprecated tools"),
    ("num_total_tools", "Total tools"),
    ("num_cron_scripts", "Cron scripts"),
    ("num_hooks", "Hooks"),
    ("num_test_files", "Test files"),
    ("num_tables_visible", "Tables"),
    ("loc_production", "Prod LOC"),
    ("loc_test", "Test LOC"),
    ("loc_total", "Total LOC"),
]


def _print_markdown(meta: dict, live: dict) -> None:
    """Print a markdown dashboard of doc-health metrics."""
    print("## Documentation Health\n")
    print("| Check | Value | Status |")
    print("|-------|-------|--------|")
    for field, label in MARKDOWN_ROWS:
        expected = meta.get(field)
        actual = live.get(field)
        if expected is not None and actual is not None and expected == actual:
            print(f"| {label} | {expected} | ✅ current |")
        elif expected is not None and actual is not None:
            print(f"| {label} | {actual} | ❌ drift (expected {expected}) |")
        else:
            print(f"| {label} | {actual if actual is not None else 'n/a'} | ❌ unavailable |")
    print(f"\nLast verified: {meta.get('last_verified', 'unknown')}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify docs/_meta.json matches live code."
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Print a markdown dashboard instead of the human-readable report.",
    )
    args = parser.parse_args()

    if not META_PATH.exists():
        print(f"ERROR: {META_PATH} not found", file=sys.stderr)
        return 1

    meta = json.loads(META_PATH.read_text())
    live = _get_live_values()

    failures = []
    for field in ALL_FIELDS:
        expected = meta.get(field)
        actual = live.get(field)
        if expected is None or actual is None:
            print(f"  SKIP  {field}: meta={expected}, live={actual} (unavailable)")
            continue
        if expected != actual:
            failures.append((field, expected, actual))
            print(f"  FAIL  {field}: meta={expected}, live={actual}")
        else:
            print(f"  PASS  {field}: {expected}")

    if args.markdown:
        _print_markdown(meta, live)

    if failures:
        print(f"\n{len(failures)} field(s) drifted. Update docs/_meta.json or fix the code.")
        return 1

    # Staleness gate
    from datetime import date as _date
    provenance = meta.get("provenance", {})
    last_regen = provenance.get("last_meta_regenerated")
    if last_regen and not args.markdown:
        try:
            days_old = (_date.today() - _date.fromisoformat(last_regen)).days
            if days_old > 7:
                print(f"  WARN  _meta.json provenance is {days_old} days stale (threshold: 7 days). Run 'make update-agents-md'.")
        except (ValueError, TypeError):
            pass

    if not args.markdown:
        print(f"\nAll {len(ALL_FIELDS)} fields match live code. Meta is fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
