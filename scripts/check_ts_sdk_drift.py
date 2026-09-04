#!/usr/bin/env python3
"""Pre-commit / verify guard: ts-sdk/dist matches ts-sdk/src without drift.

Ensures the TypeScript SDK distribution is always built, committed, and in sync
with ts-sdk/src so consumers never suffer from build or port drift.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    # 1. Build ts-sdk
    build = subprocess.run(
        ["npm", "--prefix", "ts-sdk", "run", "build"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        print("ERROR: Failed to build ts-sdk:", file=sys.stderr)
        print(build.stderr or build.stdout, file=sys.stderr)
        return build.returncode

    # 2. Check diff on ts-sdk/dist (working tree vs index)
    diff = subprocess.run(
        ["git", "diff", "--exit-code", "ts-sdk/dist"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if diff.returncode != 0:
        print("ERROR: ts-sdk/dist has drifted from ts-sdk/src (unstaged modifications detected)!", file=sys.stderr)
        print("Run 'npm --prefix ts-sdk run build' and stage the updated ts-sdk/dist files.", file=sys.stderr)
        print(diff.stdout, file=sys.stderr)
        return diff.returncode

    # 3. Check for any untracked files or unstaged changes under ts-sdk/dist
    status = subprocess.run(
        ["git", "status", "--porcelain", "ts-sdk/dist"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # Porcelain line format: XY PATH
    # If Y != ' ' or line starts with '??', there is an unstaged modification or untracked file.
    drifted_lines = []
    for raw_line in status.stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("??") or (len(line) > 1 and line[1] != " "):
            drifted_lines.append(line)

    if drifted_lines:
        print("ERROR: ts-sdk/dist contains unstaged modifications or untracked files:", file=sys.stderr)
        for line in drifted_lines:
            print(f"  {line}", file=sys.stderr)
        print("Stage and commit all files in ts-sdk/dist.", file=sys.stderr)
        return 1

    print("ts-sdk/dist is clean, fully built, and synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
