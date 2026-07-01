"""
H21 fixture helpers — extracted from conftest.py so they can be
imported directly by test files.

The conftest.py version still exists (for pytest auto-discovery),
but tests that need to call the helper directly should import from
here.
"""

import shutil
import sqlite3
from pathlib import Path

from infra.memory_common import get_memory_paths


def bootstrap_temp_db(db_path: Path) -> None:
    """Copy the live prod schema (and data) into *db_path*.

    This is the H21-recommended bootstrap: a fully-bootstrapped temp DB
    with all 6 migrations applied (incl. 005 which adds deleted_at +
    deleted_by). Tests using this don't need the blanket xfail.

    Use as a function (e.g. in setUp()) or via the temp_db_path pytest
    fixture in conftest.py.

    NOTE: This copies prod DATA, not just schema. Tests that need a
    clean DB (no pre-existing notes) should use
    `bootstrap_temp_db_clean` instead.
    """
    _, _, global_mem = get_memory_paths()
    prod_db = global_mem / "memory.db"
    if prod_db.exists():
        shutil.copy2(prod_db, db_path)


def bootstrap_temp_db_clean(db_path: Path) -> None:
    """Create a fresh DB with the full schema (incl. all 4 FTS5 + triggers) but NO data.

    Uses run_schema_setup + ensure_facts_schema from the production
    codebase instead of copying the prod DB and truncating. This
    eliminates the prod-copy dependency and the fragile truncation-
    plus-FTS5-rebuild dance.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        from infra.db_migrations import run_schema_setup

        run_schema_setup(conn)
        from fact import ensure_facts_schema

        ensure_facts_schema(conn)
        conn.commit()
    finally:
        conn.close()
