"""Pre-commit guard: Rule 24 — generated docs must match live code.

Runs `make update-docs` (the full autogen pipeline: update-agents-md ->
update-architecture -> update-mcp-tools -> update-readme ->
update-mcp-surface) and fails if any tracked doc file changed. This enforces
that documentation is committed in the same change as the code that drove it.

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

    # Only run the heavy regen when tracked doc files are part of the change.
    tracked = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
    )
    if tracked.returncode != 0:
        return 0
    staged = set(tracked.stdout.split())
    doc_globs = ("AGENTS.md", "docs/", "README.md", "README")
    if not any(
        any(p.startswith(g) or p == g.rstrip("/") for g in doc_globs)
        for p in staged
    ):
        # No doc file staged — don't block on unrelated changes.
        return 0

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

    # Did regeneration change any tracked file?
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=top,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return 0
    changed = [ln for ln in status.stdout.splitlines() if ln.strip()]
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
