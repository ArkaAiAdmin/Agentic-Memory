#!/usr/bin/env python3
"""Pre-commit / verify guard: ts-sdk/dist matches ts-sdk/src without drift.

Ensures the TypeScript SDK distribution is always built, committed, and in sync
with ts-sdk/src so consumers never suffer from build, packaging, or port drift.

Mechanism & Hardening:
1. Out-of-place compilation: Compiles ts-sdk/src to a temporary directory via
   `tsc --outDir <tmpdir>`. This eliminates side effects (such as half-emitted dist
   on compilation failure) and preserves working tree state.
2. Staged & working tree verification: Verifies byte-for-byte equality between:
   - compiler output in tempdir and live files in ts-sdk/dist/ (working tree)
   - compiler output in tempdir and git staged blobs via `git show :ts-sdk/dist/<file>`
     (closing the staged-only blind spot where index drift bypasses working tree diffs).
3. Dependency pinning: Uses ts-sdk's local tsc compiler binary (`ts-sdk/node_modules/.bin/tsc`)
   for deterministic compilation matching package.json devDependencies.
"""

from __future__ import annotations

import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TS_SDK_DIR = REPO_ROOT / "ts-sdk"
DIST_DIR = TS_SDK_DIR / "dist"
LOCAL_TSC = TS_SDK_DIR / "node_modules" / ".bin" / "tsc"


def main() -> int:
    if not DIST_DIR.is_dir():
        print("ERROR: ts-sdk/dist directory does not exist. Run 'npm --prefix ts-sdk run build'.", file=sys.stderr)
        return 1

    # Resolve tsc binary: prefer pinned local node_modules, fall back to PATH
    tsc_cmd = [str(LOCAL_TSC)] if LOCAL_TSC.is_file() else ["npx", "tsc"]

    # 1. Compile out-of-place to a temporary directory to avoid dirtying working tree on failure
    with tempfile.TemporaryDirectory(prefix="ts_sdk_drift_") as tmpdir:
        build = subprocess.run(
            tsc_cmd + ["--project", "ts-sdk", "--outDir", tmpdir],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            print("ERROR: Failed to compile ts-sdk TypeScript sources:", file=sys.stderr)
            print(build.stderr or build.stdout, file=sys.stderr)
            return build.returncode

        tmp_path = Path(tmpdir)
        emitted_files = sorted([p.name for p in tmp_path.glob("*") if p.is_file()])
        disk_files = sorted([p.name for p in DIST_DIR.glob("*") if p.is_file()])

        # 2. Check for missing or extra files on disk
        missing_on_disk = set(emitted_files) - set(disk_files)
        extra_on_disk = set(disk_files) - set(emitted_files)

        if missing_on_disk:
            print(f"ERROR: ts-sdk/dist is missing compiled files: {sorted(missing_on_disk)}", file=sys.stderr)
            return 1
        if extra_on_disk:
            print(f"ERROR: ts-sdk/dist contains unexpected extra files: {sorted(extra_on_disk)}", file=sys.stderr)
            return 1

        # 3. Check content equality against working tree on disk
        has_diff = False
        for filename in emitted_files:
            emitted_content = (tmp_path / filename).read_text(encoding="utf-8")
            disk_content = (DIST_DIR / filename).read_text(encoding="utf-8")

            if emitted_content != disk_content:
                has_diff = True
                print(f"ERROR: ts-sdk/dist/{filename} does not match fresh compiler output!", file=sys.stderr)
                diff = difflib.unified_diff(
                    disk_content.splitlines(keepends=True),
                    emitted_content.splitlines(keepends=True),
                    fromfile=f"ts-sdk/dist/{filename} (disk)",
                    tofile=f"ts-sdk/dist/{filename} (fresh tsc)",
                    n=3,
                )
                sys.stderr.writelines(diff)

        if has_diff:
            print("\nRun 'npm --prefix ts-sdk run build' and stage the updated ts-sdk/dist files.", file=sys.stderr)
            return 1

        # 4. Check git index / staged blob equality (closing staged-only blind spot)
        for filename in emitted_files:
            rel_git_path = f":ts-sdk/dist/{filename}"
            show = subprocess.run(
                ["git", "show", rel_git_path],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            # If the file is tracked/staged in git index, check its staged content
            if show.returncode == 0 and show.stdout != (tmp_path / filename).read_text(encoding="utf-8"):
                print(f"ERROR: Staged index blob for ts-sdk/dist/{filename} differs from compiler output!", file=sys.stderr)
                print("Run 'npm --prefix ts-sdk run build' and re-stage ts-sdk/dist.", file=sys.stderr)
                return 1

    # 5. Check git status for untracked / uncommitted debris in ts-sdk/dist
    status = subprocess.run(
        ["git", "status", "--porcelain", "ts-sdk/dist"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    untracked_or_drifted = []
    for raw_line in status.stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("??") or (len(line) > 1 and line[1] != " "):
            untracked_or_drifted.append(line)

    if untracked_or_drifted:
        print("ERROR: ts-sdk/dist contains unstaged modifications or untracked files:", file=sys.stderr)
        for line in untracked_or_drifted:
            print(f"  {line}", file=sys.stderr)
        return 1

    print("ts-sdk/dist is clean, fully built, and synchronized (disk and index verified).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
