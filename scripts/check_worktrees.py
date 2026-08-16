"""Pre-commit guard: at most one registered git worktree (Rule 16).

Rule 16: one persistent worktree for active development. More than one
registered worktree means parallel working copies are accumulating;
reuse the existing one or remove it after merging.

Exit 0 = OK, 1 = violation.
Run from repo root: venv/bin/python scripts/check_worktrees.py
"""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    # Prune stale metadata first: a removed worktree leaves a prunable
    # registration behind (harmless), so only real parallel worktrees
    # should fail the hook.
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0

    if result.returncode != 0:
        return 0

    # Each worktree block starts with a `worktree <path>` line.
    count = sum(1 for line in result.stdout.splitlines() if line.startswith("worktree "))
    if count > 1:
        print(
            f"ERROR: {count} git worktrees registered (Rule 16 allows at most 1).\n"
            f"Reuse the persistent worktree; remove extras with `git worktree remove <path>`.\n"
            f"Registered worktrees:\n{result.stdout}",
            file=sys.stderr,
        )
        return 1

    print("OK: single git worktree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())