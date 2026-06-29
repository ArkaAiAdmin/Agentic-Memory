#!/usr/bin/env python3
"""Verify SCHEMA_VERSION matches the number of migration files.

Usage: python scripts/schema_version_check.py
Exit: 0 if match, 1 if mismatch.
"""
from pathlib import Path
import sys


def main() -> int:
    from infra.migration_runner import SCHEMA_VERSION

    migrations_dir = Path("migrations")
    migration_files = [
        f for f in migrations_dir.glob("[0-9][0-9][0-9]_*.sql")
        if not f.name.endswith(".down.sql")
    ]
    migration_count = len(migration_files)

    print(f"SCHEMA_VERSION in migration_runner.py: {SCHEMA_VERSION}")
    print(f"Migration files (excluding .down.sql): {migration_count}")

    if SCHEMA_VERSION != migration_count:
        print(f"MISMATCH: SCHEMA_VERSION ({SCHEMA_VERSION}) != migration file count ({migration_count})")
        return 1

    print("OK: SCHEMA_VERSION matches migration file count.")
    return 0


if __name__ == "__main__":
    sys.exit(main())