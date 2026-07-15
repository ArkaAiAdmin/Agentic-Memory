"""Shared database utilities for the search pipeline phases."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

_db_columns_cache: dict = {}
_db_columns_cache_lock = threading.Lock()

# Only allow safe SQL fragments in extra_filter to prevent injection.
# Safe characters: spaces, alphanumeric, SQL punctuation (AND/OR/NOT/=, etc.)
# Ban semicolons to prevent multi-statement injection.
_SQL_SAFE_FILTER_RE = re.compile(r"^[ A-Za-z0-9_.,=<>!()'\"%\-/?]+$")
_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _get_memories_columns(db: AnyConnection) -> set[str]:
    """Cache memories table columns by DB path to save repeated PRAGMA queries.

    Thread-safe: protected by ``_db_columns_cache_lock``.  Returns a
    ``set`` of column name strings for the ``memories`` table,
    populating the module-level cache on first call per DB path.

    Args:
        db: Active ``sqlite3.Connection`` (or AnyConnection wrapper)
            used to execute ``PRAGMA database_list`` and
            ``PRAGMA table_info(memories)``.

    Returns:
        A ``set[str]`` of column names in the ``memories`` table.
        Returns an empty ``set`` on any ``sqlite3.Error``.
    """
    try:
        db_path_row = db.execute("PRAGMA database_list").fetchone()
        db_path = db_path_row[2] if db_path_row is not None and len(db_path_row) > 2 else ""
    except sqlite3.Error:
        db_path = ""

    if not db_path:
        try:
            return {
                row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall() if len(row) > 1
            }
        except sqlite3.Error:
            return set()

    with _db_columns_cache_lock:
        cols = _db_columns_cache.get(db_path)

    if cols is None:
        try:
            cols = {
                row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall() if len(row) > 1
            }
            with _db_columns_cache_lock:
                _db_columns_cache[db_path] = cols
        except sqlite3.Error:
            cols = set()

    return cols


def _validate_sql_columns(columns: str) -> bool:
    """Validate that a comma-separated column list contains only safe identifiers."""
    for col in columns.split(","):
        col = col.strip().split(" AS ")[0].strip()  # strip alias
        if not _SQL_IDENT_RE.match(col):
            return False
    return True


def _fetch_rows_by_ids(
    db: AnyConnection,
    ids: list,
    table: str = "tenant_memories",
    columns: str = "id, content, source_file, tags, created_at, fitness_score, importance, pinned, last_accessed, metadata, access_count, score",
    extra_filter: str = "",
    extra_params: tuple = (),
) -> dict:
    """Batch-fetch rows by IDs to avoid N+1 queries. Returns {id: row_tuple}.

    Defaults to the ``tenant_memories`` TEMP VIEW so results are scoped to the
    current tenant (the connection's ``tenant_id()`` function). Pass
    ``table="memories"`` explicitly for administrative cross-tenant reads.

    Chunks at 500 IDs per query to stay under SQLite's ~999 variable limit.
    """
    if not ids:
        return {}
    if not _validate_sql_columns(columns):
        logger.warning("_fetch_rows_by_ids: rejecting unsafe columns=%r", columns)
        return {}
    if not _SQL_IDENT_RE.match(table.split()[0]):
        logger.warning("_fetch_rows_by_ids: rejecting unsafe table=%r", table)
        return {}
    if extra_filter and not _SQL_SAFE_FILTER_RE.match(extra_filter):
        logger.warning(
            "_fetch_rows_by_ids: rejecting unsafe extra_filter=%r", extra_filter
        )
        return {}
    result = {}
    _CHUNK_SIZE = 500
    for i in range(0, len(ids), _CHUNK_SIZE):
        chunk = ids[i : i + _CHUNK_SIZE]
        placeholders = ",".join("?" * len(chunk))
        query = f"SELECT {columns} FROM {table} m WHERE m.id IN ({placeholders}) AND m.deleted_at IS NULL{extra_filter}"
        try:
            rows = db.execute(query, [*chunk, *extra_params]).fetchall()
            result.update({row[0]: row for row in rows})
        except sqlite3.Error:
            logger.warning("_fetch_rows_by_ids: chunk of %d ids failed", len(chunk))
            continue
    return result
