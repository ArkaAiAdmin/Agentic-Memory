"""Pre-commit guard: Rule 24 — generated docs must match live code.

Runs `make update-docs` (the full autogen pipeline: update-agents-md ->
update-architecture -> update-mcp-tools -> update-readme ->
update-mcp-surface -> update-schema -> update-config -> update-repowiki)
and fails if any tracked file changed. This enforces that documentation is
committed in the same change as the code that drove it — including code-only
changes, because every code change can affect schema/config/tool/LOC counts.

If docs are stale, it regenerates them and reports the diff so the operator
can `git add` the refreshed files and re-stage.
"""

import shutil
import subprocess
import sys


def main() -> int:
    repo = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if repo.returncode != 0:
        # Not in a git repo — nothing to guard.
        return 0
    top = repo.stdout.strip()

    make = shutil.which("make") or "make"
    regen = subprocess.run(
        [make, "update-docs"],
        cwd=top,
        capture_output=True,
        text=True,
    )
    if regen.returncode != 0:
        print(
            "ERROR: `make update-docs` failed to run.\n" + regen.stderr,
            file=sys.stderr,
        )
        return 1

    # Did regeneration change any tracked file? Untracked (`??`) and ignored
    # (`!!`) files are noise — only tracked modifications count as drift.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=top,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return 0
    changed = [
        ln for ln in status.stdout.splitlines()
        if ln.strip() and not ln.startswith(("??", "!!"))
    ]
    if not changed:
        return 0

    print(
        "ERROR: Generated docs drifted from code (Rule 24).\n"
        "  Run `make update-docs`, then `git add` the refreshed doc files "
        "and re-stage your commit.\n"
        "  Changed files:\n"
        + "\n".join(f"    {ln}" for ln in changed),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
