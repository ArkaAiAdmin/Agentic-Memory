"""Pre-commit guard: AGENTS.md stays a stable contract.

Rule 22/23 workflow contract + W4 hardening (2026-08-17):
- Line budget: AGENTS.md must stay within `MAX_LINES` (set to the
  post-hardening size; prevents regrowth of volatile prose).
- No volatile counts in prose: lines outside AUTO-GEN markers must
  not quote live counts (test files/functions, cron jobs, LOC, tool
  counts, schema tables). Live counts live in docs/_meta.json.
- Local file:// links must resolve to existing files.

Exit 0 = OK, 1 = violation (prints report to stderr).
Run from repo root: venv/bin/python scripts/check_agents_md.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTS_MD = REPO / "AGENTS.md"

MAX_LINES = 210

# Patterns that quote volatile live counts in prose. Lines inside
# AUTO-GEN markers are exempt (hard_rule_4/6 spans are machine-managed).
VOLATILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\d+\+?\s+(test files|test functions|tests|cron jobs|scheduled jobs|lifecycle hooks|MCP modules|CORE verbs|CORE tools)"),
    re.compile(r"~\d+k\+\?\s*(LOC|test LOC)"),
    re.compile(r"Schema v\d+.*\d+\+? tables"),
    re.compile(r"\b(370|5578|5112|148497|138785)\b"),
]

LINK_PATTERN = re.compile(r"file://([^\s\)]+)")


def main() -> int:
    errors: list[str] = []

    if not AGENTS_MD.exists():
        print("ERROR: AGENTS.md not found", file=sys.stderr)
        return 1

    lines = AGENTS_MD.read_text(encoding="utf-8").splitlines()

    if len(lines) > MAX_LINES:
        errors.append(
            f"AGENTS.md is {len(lines)} lines (budget {MAX_LINES}). "
            f"Trim volatile prose; live counts belong in docs/_meta.json."
        )

    in_marker = False
    for lineno, line in enumerate(lines, start=1):
        if re.search(r"AUTO-GEN:START", line):
            in_marker = True
            continue
        if re.search(r"AUTO-GEN:END", line):
            in_marker = False
            continue
        if in_marker:
            continue
        for pat in VOLATILE_PATTERNS:
            if pat.search(line):
                errors.append(f"AGENTS.md:{lineno} quotes a volatile live count: {line.strip()}")
                break

    text = AGENTS_MD.read_text(encoding="utf-8")
    for m in LINK_PATTERN.finditer(text):
        path = m.group(1)
        if not (REPO / path).exists():
            errors.append(f"AGENTS.md link target missing: {path}")

    if errors:
        print("ERROR: AGENTS.md contract violations:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("OK: AGENTS.md within contract (lines, no volatile counts, links resolve).")
    return 0


if __name__ == "__main__":
    sys.exit(main())