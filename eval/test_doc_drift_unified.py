#!/usr/bin/env python3
"""CI test: unified documentation drift check.

Runs scripts/doc_drift_check.py and asserts all three docs are in sync:
  1. docs/architecture.md (via generate script)
  2. AGENTS.md AUTO-GEN sections
  3. docs/MCP_SURFACE.md tool counts

Exit 0 on match; non-zero and prints details on mismatch.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIFT_SCRIPT = REPO_ROOT / "scripts" / "doc_drift_check.py"


def main() -> int:
    result = subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def test_unified_doc_drift():
    """Run unified doc drift check as a pytest test."""
    rc = main()
    assert rc == 0, "Documentation has drifted from live code. Run: python scripts/doc_drift_check.py"


if __name__ == "__main__":
    sys.exit(main())
