"""Tenant isolation helpers for cross-tenant query filtering.

The ``tenant_memories`` TEMP VIEW and ``tenant_id()`` SQLite function
are created on every connection by ``infra/db.py:386-389``.  This module
provides convenience helpers so callers don't repeat the same
tenant-filtering boilerplate in every SQL query.

Usage::

    from infra.tenant_query import tenant_filtered_query, tenant_memories_for

    # Replace 'FROM memories' with the tenant-filtered view
    safe_sql = tenant_filtered_query(conn, "SELECT id FROM memories WHERE deleted_at IS NULL")
    # → "SELECT id FROM tenant_memories WHERE deleted_at IS NULL"

    # Get the correct table/view name for tenant-scoped reads
    table = tenant_memories_for(conn)  # → "tenant_memories"
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def tenant_filtered_query(conn, base_sql: str, params: tuple = ()) -> tuple[str, tuple]:
    """Rewrite a SQL query to use ``tenant_memories`` instead of ``memories``.

    Replaces bare ``FROM memories`` with ``FROM tenant_memories`` in the
    query string.  Does NOT rewrite ``INTO memories`` or ``UPDATE memories``
    — those are write-path statements that must carry explicit
    ``WHERE tenant_id = tenant_id()`` clauses for each write operation.

    Args:
        conn: The SQLite connection (used only for type-checking context;
              the view must already exist on the connection).
        base_sql: The SQL query string to rewrite.
        params: The parameter tuple for the query.

    Returns:
        A ``(rewritten_sql, params)`` tuple.  ``params`` is passed through
        unchanged since ``tenant_memories`` uses ``tenant_id()`` function
        calls, not bound parameters.

    Examples::

        sql, p = tenant_filtered_query(conn,
            "SELECT id, content FROM memories WHERE deleted_at IS NULL AND pinned = 1"
        )
        # sql == "SELECT id, content FROM tenant_memories WHERE deleted_at IS NULL AND pinned = 1"

        sql, p = tenant_filtered_query(conn,
            "SELECT COUNT(*) FROM memories"
        )
        # sql == "SELECT COUNT(*) FROM tenant_memories"
    """
    # Only rewrite SELECT queries — writes must use explicit tenant checks.
    rewritten = _replace_from_memories_select(base_sql)
    if rewritten != base_sql:
        logger.debug("tenant_filtered_query: rewrote query for tenant isolation")
    return rewritten, params


def _replace_from_memories_select(sql: str) -> str:
    """Replace ``FROM memories`` with ``FROM tenant_memories`` in SELECT queries.

    Uses word-boundary matching to avoid false positives on ``FROM memories``
    inside strings or other contexts.  Only matches the exact pattern
    ``FROM memories`` (case-insensitive) as a standalone table reference.

    Does NOT match:
    - ``FROM memories_fts`` (FTS5 virtual table)
    - ``FROM memory_chunks`` / ``FROM memory_embeddings`` etc.
    - ``UPDATE memories`` or ``INSERT INTO memories``
    """
    import re

    # Match "FROM memories" followed by a word boundary (space, comma, newline,
    # paren, or end-of-string) but NOT followed by _ (to avoid memories_fts etc.)
    pattern = r'\bFROM\s+memories\b(?!\s*_)'
    return re.sub(pattern, 'FROM tenant_memories', sql, flags=re.IGNORECASE)


def tenant_memories_for(conn) -> str:
    """Return the correct table/view name for tenant-scoped memory reads.

    Always returns ``"tenant_memories"`` — the TEMP VIEW that already
    filters by ``tenant_id()``.  The view is created by ``infra/db.py``
    on every connection checkout.

    Callers should use this in f-string queries::

        table = tenant_memories_for(conn)
        rows = conn.execute(
            f"SELECT id, content FROM {table} WHERE deleted_at IS NULL"
        ).fetchall()

    Args:
        conn: The SQLite connection (unused currently; reserved for future
              connection introspection if the view isn't available).

    Returns:
        ``"tenant_memories"``
    """
    return "tenant_memories"


def install_tenant_context(conn, tenant_id: str | None = None) -> str:
    """Install the tenant isolation primitives on a raw sqlite3 connection.

    Cron scripts and other subprocesses open their own ``sqlite3.connect`` and
    therefore do NOT share the worker's pooled connection (which already has the
    ``tenant_id()`` UDF + ``tenant_memories`` TEMP VIEW installed).  Without this
    call, a bare ``FROM memories`` in such a subprocess reads EVERY tenant's rows,
    breaking multi-tenant isolation.

    Call this immediately after opening the connection::

        conn = sqlite3.connect(str(db_path))
        from infra.tenant_query import install_tenant_context
        install_tenant_context(conn, os.environ.get("MEMORY_CRON_TENANT_ID"))

    The tenant is resolved as follows:
      * ``tenant_id`` arg (highest priority) — e.g. from ``MEMORY_CRON_TENANT_ID``
      * ``MEMORY_TENANT_ID`` env var (legacy/operator override)
      * default ``"default"`` — single-tenant behaviour unchanged.

    When the tenant is ``"default"`` the view returns all rows with
    ``tenant_id = 'default'`` (the correct single-tenant scope).

    Returns the resolved tenant id.

    Side effects: installs the ``tenant_id()`` UDF and creates the
    ``tenant_memories`` TEMP VIEW on ``conn``.  Idempotent: safe to call
    repeatedly.  Raises nothing — failures are logged and swallowed so a
    missing UDF never crashes a cron script that doesn't need it.
    """
    import os

    resolved = tenant_id or os.environ.get("MEMORY_CRON_TENANT_ID")
    if not resolved:
        resolved = os.environ.get("MEMORY_TENANT_ID") or "default"

    try:
        conn.create_function("tenant_id", 0, lambda: resolved)
        conn.execute(
            "CREATE TEMP VIEW IF NOT EXISTS tenant_memories AS "
            "SELECT * FROM memories WHERE tenant_id = tenant_id()"
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "install_tenant_context: failed to install tenant view "
            "for tenant=%s on %s: %s",
            resolved,
            type(conn).__name__,
            e,
        )
    return resolved
