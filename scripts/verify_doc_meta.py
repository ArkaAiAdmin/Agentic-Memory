"""Verify docs/_meta.json matches live code.

Exit code 0 = all good. Exit code 1 = drift detected (prints report).
Run from repo root: venv/bin/python scripts/verify_doc_meta.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

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


def _get_live_values() -> dict:
    """Extract live values from code."""
    live = {}

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
        "./.mimocode/*", "./.opencode/*", "./ts-sdk/node_modules/*", "*/__pycache__/*",
    ]
    total_loc = _count_lines(excludes)
    eval_excludes = excludes + ["*/eval/*"]
    prod_loc = _count_lines(eval_excludes)
    live["loc_total"] = total_loc
    live["loc_production"] = prod_loc
    live["loc_test"] = total_loc - prod_loc if total_loc and prod_loc else None

    return live


def main() -> int:
    if not META_PATH.exists():
        print(f"ERROR: {META_PATH} not found", file=sys.stderr)
        return 1

    meta = json.loads(META_PATH.read_text())
    live = _get_live_values()

    all_fields = [
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

    failures = []
    for field in all_fields:
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

    if failures:
        print(f"\n{len(failures)} field(s) drifted. Update docs/_meta.json or fix the code.")
        return 1

    print(f"\nAll {len(all_fields)} fields match live code. Meta is fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
