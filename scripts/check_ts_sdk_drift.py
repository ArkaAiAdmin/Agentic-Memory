#!/usr/bin/env python3
"""Pre-commit / verify guard: ts-sdk/dist matches ts-sdk/src without drift.

Ensures the TypeScript SDK distribution is always built, committed, and in sync
with ts-sdk/src so consumers never suffer from build, packaging, or port drift.

Mechanism & Hardening:
1. Out-of-place compilation: Compiles ts-sdk/src to a temporary directory via
   `tsc --outDir <tmpdir>`. This eliminates side effects (such as half-emitted dist
   on compilation failure) and preserves working tree state.
2. Binary-safe recursive verification: Traverses all emitted files recursively
   (`rglob`), comparing raw bytes (`read_bytes()`) to eliminate CRLF/text-encoding
   edge cases.
3. Staged & working tree verification: Verifies byte equality against:
   - live files in ts-sdk/dist/ (working tree)
   - git staged blobs via `git show :ts-sdk/dist/<path>` (closing staged-only blind spot).
4. Compiler version verification: Verifies active `tsc` version against the pinned
   dependency in `ts-sdk/package-lock.json`.
"""

from __future__ import annotations

import difflib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TS_SDK_DIR = REPO_ROOT / "ts-sdk"
DIST_DIR = TS_SDK_DIR / "dist"
LOCAL_TSC = TS_SDK_DIR / "node_modules" / ".bin" / "tsc"
LOCK_FILE = TS_SDK_DIR / "package-lock.json"


def get_expected_tsc_version() -> str | None:
    if LOCK_FILE.is_file():
        try:
            data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            return data.get("packages", {}).get("node_modules/typescript", {}).get("version")
        except Exception:
            pass
    return None


def main() -> int:
    if not DIST_DIR.is_dir():
        print("ERROR: ts-sdk/dist directory does not exist. Run 'npm --prefix ts-sdk run build'.", file=sys.stderr)
        return 1

    expected_version = get_expected_tsc_version()
    if not expected_version:
        print("ERROR: Failed to read expected typescript version from ts-sdk/package-lock.json.", file=sys.stderr)
        return 1

    # Resolve tsc binary: prefer pinned local node_modules, fall back to version-pinned npx
    if LOCAL_TSC.is_file():
        tsc_cmd = [str(LOCAL_TSC)]
    else:
        tsc_cmd = ["npx", "--package", f"typescript@{expected_version}", "tsc"]

    # Verify tsc version against lockfile (fail-closed)
    version_res = subprocess.run(tsc_cmd + ["--version"], cwd=REPO_ROOT, capture_output=True, text=True)
    if version_res.returncode != 0:
        print(f"ERROR: Failed to run {tsc_cmd[0]} --version:\n{version_res.stderr or version_res.stdout}", file=sys.stderr)
        return 1

    actual_version = version_res.stdout.strip().replace("Version ", "")
    if actual_version != expected_version:
        print(
            f"ERROR: tsc compiler version mismatch: expected {expected_version} from package-lock.json, got {actual_version}.\n"
            f"Remediation: Run 'npm --prefix ts-sdk ci' to restore pinned dependencies.",
            file=sys.stderr,
        )
        return 1

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
        # Recursive glob to support any future subdirectories in dist
        emitted_rel_paths = sorted([p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()])
        disk_rel_paths = sorted([p.relative_to(DIST_DIR) for p in DIST_DIR.rglob("*") if p.is_file()])

        # 2. Check for missing or extra files on disk
        missing_on_disk = set(emitted_rel_paths) - set(disk_rel_paths)
        extra_on_disk = set(disk_rel_paths) - set(emitted_rel_paths)

        if missing_on_disk:
            missing_str = [p.as_posix() for p in sorted(missing_on_disk)]
            print(f"ERROR: ts-sdk/dist is missing compiled files: {missing_str}", file=sys.stderr)
            return 1
        if extra_on_disk:
            extra_str = [p.as_posix() for p in sorted(extra_on_disk)]
            print(f"ERROR: ts-sdk/dist contains unexpected extra files: {extra_str}", file=sys.stderr)
            return 1

        # 3. Check binary content equality against working tree on disk
        has_diff = False
        for rel_path in emitted_rel_paths:
            emitted_bytes = (tmp_path / rel_path).read_bytes()
            disk_bytes = (DIST_DIR / rel_path).read_bytes()

            if emitted_bytes != disk_bytes:
                has_diff = True
                print(f"ERROR: ts-sdk/dist/{rel_path.as_posix()} does not match fresh compiler output!", file=sys.stderr)
                try:
                    emitted_text = emitted_bytes.decode("utf-8")
                    disk_text = disk_bytes.decode("utf-8")
                    diff = difflib.unified_diff(
                        disk_text.splitlines(keepends=True),
                        emitted_text.splitlines(keepends=True),
                        fromfile=f"ts-sdk/dist/{rel_path.as_posix()} (disk)",
                        tofile=f"ts-sdk/dist/{rel_path.as_posix()} (fresh tsc)",
                        n=3,
                    )
                    sys.stderr.writelines(diff)
                except UnicodeDecodeError:
                    print("  [binary file difference]", file=sys.stderr)

        if has_diff:
            print("\nRun 'npm --prefix ts-sdk run build' and stage the updated ts-sdk/dist files.", file=sys.stderr)
            return 1

        # 4. Check git index / staged blob equality (closing staged-only blind spot)
        for rel_path in emitted_rel_paths:
            posix_path = rel_path.as_posix()
            show = subprocess.run(
                ["git", "show", f":ts-sdk/dist/{posix_path}"],
                cwd=REPO_ROOT,
                capture_output=True,
            )
            # If the file is tracked/staged in git index, check its staged content bytes
            if show.returncode == 0 and show.stdout != (tmp_path / rel_path).read_bytes():
                print(f"ERROR: Staged index blob for ts-sdk/dist/{posix_path} differs from compiler output!", file=sys.stderr)
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

    print(f"ts-sdk/dist is clean, fully built, and synchronized (disk, index, and tsc {actual_version} verified).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
