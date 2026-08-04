#!/usr/bin/env python3
"""Verify SCHEMA_VERSION matches the highest migration number on disk.

The runtime invariant is `SCHEMA_VERSION == max migration number` (see
`migration_runner`'s SCHEMA_STABLE guard, which refuses to apply any
migration numbered > SCHEMA_VERSION). The base schema (000_base_schema.sql)
is the foundation and is intentionally not part of the versioned count, so we
compare against the MAX numeric prefix, not the file count.

Usage: python scripts/schema_version_check.py
Exit: 0 if match, 1 if mismatch.
"""
import re
from pathlib import Path
import sys


def main() -> int:
    # Pre-commit hooks run from the repo root, but this script is invoked by
    # path (scripts/schema_version_check.py) — bootstrap the repo root onto
    # sys.path so `infra` is importable regardless of CWD.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from infra.migration_runner import SCHEMA_VERSION

    migrations_dir = Path("migrations")
    migration_files = [
        f for f in migrations_dir.glob("[0-9][0-9][0-9]_*.sql")
        if not f.name.endswith(".down.sql")
    ]
    numbers = []
    for f in migration_files:
        m = re.match(r"^(\d+)_", f.name)
        if m:
            numbers.append(int(m.group(1)))
    max_migration = max(numbers) if numbers else -1
    file_count = len(migration_files)

    print(f"SCHEMA_VERSION in migration_runner.py: {SCHEMA_VERSION}")
    print(f"Migration files (excluding .down.sql): {file_count}")
    print(f"Max migration number on disk: {max_migration}")

    if SCHEMA_VERSION != max_migration:
        print(
            f"MISMATCH: SCHEMA_VERSION ({SCHEMA_VERSION}) != "
            f"max migration number ({max_migration})"
        )
        return 1

    print("OK: SCHEMA_VERSION matches the highest migration number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())