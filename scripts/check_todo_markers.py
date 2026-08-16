"""Pre-commit guard: reject TODO/FIXME/HACK markers on added lines (Rule 17).

Rule 17: leaving known-broken code is not acceptable — mark it fixed, not
TODO'd. Scans only lines added in the staged diff (git diff --cached),
so pre-existing markers in unchanged code do not block commits.

Scope: source files (.py/.ts/.tsx/.js/.jsx/.rs/.go/.sql/.sh) only, and
never this script itself — docs, configs and Makefiles legitimately name
the markers when describing the rule, and scanning them would make the
guard fail on its own documentation.

Exit 0 = OK, 1 = violation (lists file:line of each offending added line).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

MARKER = re.compile(r"\b(TODO|FIXME|HACK)\b")
_SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".sql", ".sh"}
_SELF = Path(__file__).resolve()


def _added_lines() -> list[tuple[str, int, str]]:
    """Return (path, line_no, content) for every line added in the staged diff."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--diff-filter=ACM", "--unified=0"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []

    hits: list[tuple[str, int, str]] = []
    path: str | None = None
    new_line: int | None = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
        elif line.startswith("@@ "):
            m = re.search(r"\+(\d+)(?:,\d+)? @@", line)
            new_line = int(m.group(1)) if m else None
        elif line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            if path is not None and new_line is not None:
                hits.append((path, new_line, content))
                if new_line is not None:
                    new_line += 1
    return hits


def main() -> int:
    violations = [
        (path, lineno, content)
        for path, lineno, content in _added_lines()
        if Path(path).suffix in _SOURCE_EXTS
        and Path(path).resolve() != _SELF
        and MARKER.search(content)
    ]
    if violations:
        print(
            "ERROR: TODO/FIXME/HACK markers on added lines (Rule 17 — fix bugs, don't TODO them):",
            file=sys.stderr,
        )
        for path, lineno, content in violations:
            print(f"  {path}:{lineno}: {content.strip()}", file=sys.stderr)
        return 1

    print("OK: no TODO/FIXME/HACK markers on added lines.")
    return 0


if __name__ == "__main__":
    sys.exit(main())