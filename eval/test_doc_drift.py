#!/usr/bin/env python3
"""CI test: ensure docs/architecture.md matches live code.

Runs `scripts/generate_architecture_md.py` and asserts the output is
byte-equal to the checked-in `docs/architecture.md`.

Exit 0 on match; non-zero and prints a diff on mismatch.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "generate_architecture_md.py"
ARCH_MD = REPO_ROOT / "docs" / "architecture.md"


def main() -> int:
    result = subprocess.run(
        [sys.executable, str(GENERATOR)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"❌ Generator failed:\n{result.stderr}")
        return 1

    generated = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    checked_in = ARCH_MD.read_text(encoding="utf-8")

    if generated == checked_in:
        print("✅ docs/architecture.md matches live code.")
        return 0

    # Show minimal diff context
    gen_lines = generated.splitlines()
    chk_lines = checked_in.splitlines()
    diffs = 0
    for i, (g, c) in enumerate(zip(gen_lines, chk_lines), 1):
        if g != c:
            print(f"  Line {i} differs:")
            print(f"    generated: {g!r}")
            print(f"    checked-in: {c!r}")
            diffs += 1
            if diffs >= 10:
                print("  ... (truncated)")
                break
    if len(gen_lines) != len(chk_lines):
        print(f"  Line count differs: generated={len(gen_lines)}, checked-in={len(chk_lines)}")
    print("\n❌ docs/architecture.md has drifted from live code.")
    print("   Fix:  python scripts/generate_architecture_md.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())


def test_doc_drift_matches_codebase():
    """Run doc drift check as a pytest test."""
    rc = main()
    assert rc == 0, "docs/architecture.md has drifted from live code"
