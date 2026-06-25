#!/usr/bin/env python3
"""
Session recap — called from ecc-hooks.ts session.created handler.

Replaces the old inline ``python -c '...'`` that was fragile (escaped
quotes in TypeScript) and hard to debug. This script lives in the
proper Python module path so imports resolve cleanly.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap_path  # noqa: E402, F401

from recall import session_recap  # noqa: E402


def main():
    result = session_recap()
    if result:
        print(result)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"recall failed: {e}", file=sys.stderr)
        sys.exit(0)
