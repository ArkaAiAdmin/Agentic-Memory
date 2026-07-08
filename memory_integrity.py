"""Memory DB integrity checker.

Detects silent corruption in the agentic-memory SQLite database:
- Missing required tables (memories, backlinks)
- FTS5 index drift (external-content table out of sync with memories)
- Orphan backlinks / chunks (references to deleted memories)
- Orphan notes (memories with no backlinks)
- Missing .md files (saga recovery scenario)
- Supersession health (T3.5: fact-level temporal KG)
- Deep mode: PRAGMA integrity_check + foreign_key_check

Returns structured findings with severity (critical / warning / info / ok).

Scenario 7 fix (2026-06-22): the ``recover_orphan_files`` function
re-creates .md files for memories whose file is missing on disk
(e.g. the saga crashed between the DB upsert and the file write).
"""

from __future__ import annotations

import logging

__all__ = [
    "check_index_integrity",
    "main",
    "recover_orphan_files",
    "find_orphan_files",
    "repair_fts_drift",
    "find_kg_orphans",
    "repair_kg_orphans",
]

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from infra.memory_common import open_db
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)


_REQUIRED_TABLES = frozenset({"memories", "backlinks"})
_OPTIONAL_TABLES = frozenset({"memories_fts", "memory_chunks"})
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2, "ok": 3}


def _get_table_names(db: AnyConnection) -> set[str]:
    return {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }


def _get_memories_count(db: AnyConnection) -> int:
    """Count active (non-deleted) memories for FTS5 comparison."""
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall()}
        if "deleted_at" in cols:
            row = db.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
            ).fetchone()
            if row is not None:
                return int(row[0])
    except sqlite3.DatabaseError:
        pass
    row = db.execute("SELECT COUNT(*) FROM memories").fetchone()
    if row is None:
        return 0
    return int(row[0])


def _get_fts_indexed_count(db: AnyConnection, tables: set[str]) -> int | None:
    """Count documents actually indexed in the FTS5 structure.

    For external-content FTS5, the virtual table's COUNT(*) and rowid
    both fall back to the content table, so they cannot reveal drift.
    The internal ``memories_fts_idx`` shadow table holds one row per
    indexed document and is the reliable signal.

    For non-external-content FTS5 (current), COUNT(*) on the virtual
    table directly returns the number of indexed documents.
    """
    if "memories_fts" not in tables:
        return None

    # Check if FTS5 uses external content
    try:
        sql_row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        if sql_row and sql_row[0]:
            create_sql = sql_row[0].lower()
            # Check for external content FTS5 (both quoted and unquoted)
            if "content=memories" in create_sql or "content='memories'" in create_sql:
                # External content FTS5 - use shadow table
                if "memories_fts_idx" in tables:
                    cnt = db.execute("SELECT COUNT(*) FROM memories_fts_idx").fetchone()
                    return int(cnt[0]) if cnt else 0
                return None
    except sqlite3.DatabaseError:
        pass

    # Non-external content FTS5 - virtual table COUNT(*) is accurate
    try:
        cnt = db.execute("SELECT COUNT(*) FROM memories_fts").fetchone()
        return int(cnt[0]) if cnt else 0
    except sqlite3.DatabaseError:
        return None


def _table_has_column(db: AnyConnection, table: str, column: str) -> bool:
    if not table or not table.replace("_", "").isalnum():
        return False
    if not column or not column.replace("_", "").isalnum():
        return False
    try:
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.DatabaseError:
        return False
    return any(row[1] == column for row in rows)


def _get_orphaned_backlinks(db: AnyConnection) -> list[dict[str, Any]]:
    """Find backlinks whose source_id no longer exists in memories.

    By design, target_id may refer to non-existent notes (wiki-style
    "red links"), so we only flag missing source_id.
    """
    try:
        rows = db.execute(
            """
            SELECT bl.source_id, bl.target_id
            FROM backlinks bl
            LEFT JOIN memories ms ON ms.id = bl.source_id AND ms.deleted_at IS NULL
            WHERE ms.id IS NULL
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        return []

    if not rows:
        return []

    orphans: list[dict[str, Any]] = []
    for src, tgt in rows:
        orphans.append(
            {
                "id": f"orphan-backlink-{src}->{tgt}",
                "check": "orphan_backlinks",
                "severity": "warning",
                "message": (
                    f"Backlink source note {src!r} no longer exists (target {tgt!r})"
                ),
            }
        )
    return orphans


def _get_orphaned_chunks(
    db: AnyConnection, tables: set[str]
) -> list[dict[str, Any]]:
    if "memory_chunks" not in tables:
        return []
    if "memories" not in tables:
        return []
    if not _table_has_column(db, "memory_chunks", "parent_id"):
        return []
    try:
        rows = db.execute(
            """
            SELECT c.parent_id, c.chunk_idx
            FROM memory_chunks c
            LEFT JOIN memories m ON m.id = c.parent_id AND m.deleted_at IS NULL
            WHERE m.id IS NULL
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        return []

    return [
        {
            "id": f"orphan-chunk-{note_id}-{chunk_idx}",
            "check": "orphan_chunks",
            "severity": "warning",
            "message": f"Chunk {chunk_idx} of note {note_id!r} has no memory",
        }
        for note_id, chunk_idx in rows
    ]


def _get_orphaned_notes(
    db: AnyConnection, tables: set[str]
) -> list[dict[str, Any]]:
    if "backlinks" not in tables:
        return []
    try:
        backlink_count_row = db.execute("SELECT COUNT(*) FROM backlinks").fetchone()
        backlink_count = int(backlink_count_row[0]) if backlink_count_row is not None else 0
    except sqlite3.DatabaseError:
        backlink_count = 0
    if backlink_count == 0:
        return []
    rows = db.execute(
        """
        SELECT m.id FROM memories m
        WHERE m.deleted_at IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM backlinks b
            WHERE b.source_id = m.id OR b.target_id = m.id
        )
        """
    ).fetchall()

    return [
        {
            "id": f"orphan-note-{r[0]}",
            "check": "orphan_notes",
            "severity": "info",
            "message": f"Memory {r[0]!r} has no backlinks (most notes are unlinked — this is normal)",
        }
        for r in rows
    ]


def _check_fts5_mismatch(
    db: AnyConnection, tables: set[str]
) -> dict[str, Any] | None:
    if "memories" not in tables:
        return None
    if "memories_fts" not in tables:
        return None
    indexed = _get_fts_indexed_count(db, tables)
    if indexed is None:
        if "memories_fts" in tables and "memories_fts_idx" not in tables:
            try:
                row = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
                ).fetchone()
                if (
                    row
                    and row[0]
                    and (
                        "content=memories" in row[0].lower()
                        or "content='memories'" in row[0].lower()
                    )
                ):
                    return {
                        "id": "fts5-shadow-missing",
                        "check": "fts5_mismatch",
                        "severity": "warning",
                        "message": "FTS5 shadow table memories_fts_idx is missing — FTS index is likely corrupted",
                    }
            except sqlite3.DatabaseError:
                pass
        return None
    mem_count = _get_memories_count(db)
    if mem_count == indexed:
        return None
    return {
        "id": "fts5-mismatch",
        "check": "fts5_mismatch",
        "severity": "warning",
        "message": (f"FTS5 index has {indexed} documents but memories has {mem_count}"),
    }


def _check_vector_index_mismatch(
    db: AnyConnection, tables: set[str]
) -> dict[str, Any] | None:
    if "memories" not in tables:
        return None
    if "memory_vec_keys" not in tables:
        return None
    if "memory_vec_idx" not in tables:
        return None
    try:
        # If the vector index has never been built, don't flag a mismatch
        idx_exists_row = db.execute("SELECT 1 FROM memory_vec_idx WHERE id=1").fetchone()
        if not idx_exists_row:
            return None

        vec_keys_count_row = db.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()
        vec_keys_count = int(vec_keys_count_row[0]) if vec_keys_count_row is not None else 0
        mem_count_row = db.execute("SELECT COUNT(*) FROM memories").fetchone()
        mem_count = int(mem_count_row[0]) if mem_count_row is not None else 0
        if vec_keys_count != mem_count:
            return {
                "id": "vector-index-mismatch",
                "check": "vector_index_mismatch",
                "severity": "warning",
                "message": (
                    f"Vector index has {vec_keys_count} keys but memories has {mem_count}"
                ),
            }
    except sqlite3.DatabaseError:
        pass
    return None


def _check_pragma_integrity(db: AnyConnection) -> list[dict[str, Any]]:
    try:
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.DatabaseError:
        pass
    try:
        rows = db.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as e:
        return [
            {
                "id": "db-corrupt",
                "check": "db_integrity",
                "severity": "critical",
                "message": f"PRAGMA integrity_check failed: {e}",
            }
        ]

    if len(rows) == 1 and rows[0][0] == "ok":
        return []

    findings: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        if row[0] == "ok":
            continue
        findings.append(
            {
                "id": f"integrity-error-{idx}",
                "check": "db_integrity",
                "severity": "critical",
                "message": f"PRAGMA integrity_check: {row[0]}",
            }
        )
    return findings


def _check_fk_violations(db: AnyConnection) -> list[dict[str, Any]]:
    try:
        rows = db.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError:
        return []

    findings: list[dict[str, Any]] = []
    for row in rows:
        findings.append(
            {
                "id": f"fk-violation-{row[0]}-{row[1]}-{row[2]}",
                "check": "foreign_keys",
                "severity": "critical",
                "message": (
                    f"FK violation in table {row[0]} rowid {row[1]} -> {row[2]}"
                ),
            }
        )
    return findings


def _check_supersession_health(
    db_path: str | Path,
) -> list[dict[str, Any]]:
    """T3.5: surface fact-level supersession health (temporal KG).

    Reports:
      * total superseded facts (info: expected to grow over time)
      * total superseding facts (info)
      * high-confidence contradictions (contradiction_score == 1.0) in
        the last 7 days (info: visible signal that the auto-detector
        is working)
      * a sample of the most recent 5 supersession events (info)

    These are reported as "info" severity, not warning — supersession
    IS the desired behavior of the temporal KG.  A warning would be
    raised if facts with contradiction_score < 0.5 were auto-applied
    (low confidence), but the current detector always uses 1.0
    (deterministic), so that case is impossible for now.

    Opens a separate read-only URI connection so this check works even
    when the live ``auto_save.py daemon`` holds the flock on the main
    DB.  SQLite's WAL mode allows concurrent readers alongside a
    single writer.
    """
    findings: list[dict[str, Any]] = []
    db_path = Path(db_path)
    if not db_path.exists():
        return findings
    try:
        ro_uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(ro_uri, uri=True) as db:
            tables = _get_table_names(db)
            if "kg_facts" not in tables:
                return findings

            total_superseded = db.execute(
                "SELECT COUNT(*) FROM kg_facts WHERE superseded_by IS NOT NULL"
            ).fetchone()[0]
            total_superseding = db.execute(
                "SELECT COUNT(*) FROM kg_facts WHERE supersedes IS NOT NULL"
            ).fetchone()[0]
            if total_superseded == 0 and total_superseding == 0:
                # No supersession activity — emit a single info finding so
                # the check is visible in --temporal-summary output.
                findings.append(
                    {
                        "id": "supersession-none",
                        "check": "temporal_supersession",
                        "severity": "info",
                        "message": (
                            "0 fact-level supersession events recorded "
                            "(temporal KG is clean)."
                        ),
                    }
                )
                return findings

            # Recent events (last 7 days, transaction_time)
            week_ago = time.time() - 7 * 86400
            recent = db.execute(
                "SELECT COUNT(*) FROM kg_facts "
                "WHERE superseded_by IS NOT NULL AND transaction_time > ?",
                (week_ago,),
            ).fetchone()[0]

            findings.append(
                {
                    "id": "supersession-totals",
                    "check": "temporal_supersession",
                    "severity": "info",
                    "message": (
                        f"Fact-level supersession: {total_superseded} "
                        f"superseded, {total_superseding} superseding; "
                        f"{recent} in last 7 days"
                    ),
                }
            )

            # Sample of the most recent 5 supersession events.
            rows = db.execute(
                "SELECT id, subject, predicate, object, superseded_by, "
                "       invalidation_reason, contradiction_score, "
                "       transaction_time "
                "FROM kg_facts WHERE superseded_by IS NOT NULL "
                "ORDER BY transaction_time DESC LIMIT 5"
            ).fetchall()
            for r in rows:
                findings.append(
                    {
                        "id": f"supersession-event-{r[0]}",
                        "check": "temporal_supersession",
                        "severity": "info",
                        "message": (
                            f"  fact {r[0]}: {r[1]} {r[2]} {r[3]!r} → "
                            f"superseded by {r[4]} "
                            f"(reason: {r[5] or '?'}, score: {r[6]})"
                        ),
                    }
                )
    except sqlite3.OperationalError as e:
        findings.append(
            {
                "id": "supersession-error",
                "check": "temporal_supersession",
                "severity": "warning",
                "message": f"Could not read supersession data: {e}",
            }
        )
    return findings


def temporal_summary(db_path: str | Path) -> dict[str, Any]:
    """T3.5: focused supersession stats for the --temporal-summary CLI.

    Returns a small dict suitable for printing; never raises on missing
    columns (pre-v18 DBs just return zero counts).

    Opens the DB in **read-only URI mode** so it never contends with the
    live ``auto_save.py daemon``'s flock.  This is safe because the
    function only reads (no writes) and SQLite's WAL mode allows
    concurrent readers alongside a single writer.
    """
    db_path = Path(db_path)
    summary: dict[str, Any] = {
        "schema_version": None,
        "facts_total": 0,
        "facts_with_event_time": 0,
        "facts_superseded": 0,
        "facts_superseding": 0,
        "supersessions_last_7d": 0,
        "supersessions_by_reason": {},
    }
    if not db_path.exists():
        return summary
    try:
        # Read-only URI bypasses the flock — WAL allows concurrent reads.
        ro_uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(ro_uri, uri=True) as db:
            summary["schema_version"] = db.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            tables = _get_table_names(db)
            if "kg_facts" in tables:
                summary["facts_total"] = db.execute(
                    "SELECT COUNT(*) FROM kg_facts"
                ).fetchone()[0]
                summary["facts_with_event_time"] = db.execute(
                    "SELECT COUNT(*) FROM kg_facts WHERE event_time IS NOT NULL"
                ).fetchone()[0]
                summary["facts_superseded"] = db.execute(
                    "SELECT COUNT(*) FROM kg_facts WHERE superseded_by IS NOT NULL"
                ).fetchone()[0]
                summary["facts_superseding"] = db.execute(
                    "SELECT COUNT(*) FROM kg_facts WHERE supersedes IS NOT NULL"
                ).fetchone()[0]
                week_ago = time.time() - 7 * 86400
                summary["supersessions_last_7d"] = db.execute(
                    "SELECT COUNT(*) FROM kg_facts "
                    "WHERE superseded_by IS NOT NULL AND transaction_time > ?",
                    (week_ago,),
                ).fetchone()[0]
                # Group by reason for distribution visibility
                for row in db.execute(
                    "SELECT COALESCE(invalidation_reason, 'unknown') AS reason, "
                    "       COUNT(*) AS n "
                    "FROM kg_facts WHERE superseded_by IS NOT NULL "
                    "GROUP BY reason ORDER BY n DESC"
                ).fetchall():
                    summary["supersessions_by_reason"][row[0]] = row[1]
    except sqlite3.OperationalError as e:
        summary["error"] = str(e)
    return summary


def _format_summary(findings: list[dict[str, Any]]) -> str:
    critical_count = sum(1 for f in findings if f["severity"] == "critical")
    warning_count = sum(1 for f in findings if f["severity"] == "warning")
    if critical_count == 0 and warning_count == 0:
        return "OK"
    parts = [f"{critical_count} critical"]
    parts.append("1 warning" if warning_count == 1 else f"{warning_count} warnings")
    return " ".join(parts)


def _add_required_table_findings(
    tables: set[str], findings: list[dict[str, Any]]
) -> None:
    for req in _REQUIRED_TABLES:
        if req not in tables:
            findings.append(
                {
                    "id": f"missing-table-{req}",
                    "check": "required_tables",
                    "severity": "critical",
                    "message": f"Required table {req!r} is missing",
                }
            )


def check_index_integrity(db_path: str | Path, deep: bool = False) -> dict[str, Any]:
    """Check the integrity of the memory DB at db_path.

    Args:
        db_path: Path to the SQLite memory database.
        deep: If True, also run PRAGMA integrity_check and
            PRAGMA foreign_key_check (slower).

    Returns:
        dict with keys:
          - ok (bool): True iff no critical or warning findings.
          - findings (list[dict]): Each finding has id, check,
            severity, message.
          - summary (str): One-line human summary.
    """
    db_path = Path(db_path)
    findings: list[dict[str, Any]] = []

    if not db_path.exists():
        findings.append(
            {
                "id": "db-missing",
                "check": "db_exists",
                "severity": "critical",
                "message": f"Database file does not exist: {db_path}",
            }
        )
        return {
            "ok": False,
            "findings": sorted(findings, key=lambda f: _SEVERITY_RANK[f["severity"]]),
            "summary": _format_summary(findings),
        }

    try:
        with open_db(db_path, write=False) as db:
            tables = _get_table_names(db)

            _add_required_table_findings(tables, findings)

            if "memories" in tables:
                fts_finding = _check_fts5_mismatch(db, tables)
                if fts_finding:
                    findings.append(fts_finding)

            if "memories" in tables and "memory_vec_keys" in tables:
                vec_finding = _check_vector_index_mismatch(db, tables)
                if vec_finding:
                    findings.append(vec_finding)

            if "memories" in tables and "backlinks" in tables:
                findings.extend(_get_orphaned_backlinks(db))

            findings.extend(_get_orphaned_chunks(db, tables))

            if "memories" in tables:
                findings.extend(_get_orphaned_notes(db, tables))

            # Scenario 7 (2026-06-22): surface backward orphans
            # (memories with no .md file on disk) as warnings.  This is
            # a silent-data-corruption signal — the DB has content
            # the user can no longer read in their editor.  The
            # ``--recover-orphan-files`` CLI option re-creates them.
            memory_root = db_path.parent
            if memory_root.exists():
                for orphan in find_orphan_files(db, memory_root):
                    findings.append(
                        {
                            "id": f"missing-md-{orphan['memory_id']}",
                            "check": "missing_md_file",
                            "severity": "warning",
                            "message": (
                                f"Memory {orphan['memory_id']!r} has no .md "
                                f"file at {orphan['md_path']} (saga crashed "
                                f"between DB upsert and file write; run "
                                f"--recover-orphan-files to fix)"
                            ),
                        }
                    )
                # Forward orphans (vec_keys without memories) — should
                # be impossible under the FK, but scan defensively.
                for orphan in find_orphan_vec_keys(db):
                    findings.append(
                        {
                            "id": f"orphan-vec-key-{orphan['key']}",
                            "check": "orphan_vec_key",
                            "severity": "warning",
                            "message": (
                                f"vec_key {orphan['key']} points to "
                                f"missing memory {orphan['memory_id']!r} "
                                f"(FK violation; should not be possible)"
                            ),
                        }
                    )

            # T3.5: temporal KG supersession health (always included,
            # not gated on --deep — it's cheap and useful for live ops).
            # Pass db_path (not db) so this check opens its own read-only
            # connection and works even when the live auto-save daemon
            # holds the flock on the main DB.
            findings.extend(_check_supersession_health(db_path))

            if deep:
                findings.extend(_check_pragma_integrity(db))
                findings.extend(_check_fk_violations(db))

    except sqlite3.DatabaseError as e:
        findings.append(
            {
                "id": "db-corrupt",
                "check": "db_open",
                "severity": "critical",
                "message": f"SQLite error opening database: {e}",
            }
        )
    except Exception as e:
        logger.warning("check_index_integrity failed: %s", e)
        findings.append(
            {
                "id": "integrity-unexpected-error",
                "check": "unexpected_error",
                "severity": "warning",
                "message": f"Unexpected error during integrity check: {type(e).__name__}: {e}",
            }
        )

    has_problems = any(f["severity"] in ("critical", "warning") for f in findings)
    if not has_problems:
        findings.append(
            {
                "id": "index-clean",
                "check": "overall",
                "severity": "ok",
                "message": "Memory index integrity check passed",
            }
        )

    findings.sort(key=lambda f: _SEVERITY_RANK[f["severity"]])
    ok = not has_problems

    return {
        "ok": ok,
        "findings": findings,
        "summary": _format_summary(findings),
    }


# ---------------------------------------------------------------------------
# Scenario 11 fix (2026-06-22): FTS5 drift auto-healing
# ---------------------------------------------------------------------------
#
# The FTS5 index can drift from the ``memories`` table when an FTS
# insert fails (disk-full, schema mismatch, interrupted
# transaction).  The existing ``_check_fts5_mismatch`` reports the
# drift as a warning, but offers no remediation.  This module
# adds ``repair_fts_drift`` which runs the standard
# ``INSERT INTO fts(fts) VALUES('rebuild')`` to trigger SQLite's
# B-tree reconstruction — the same path the daily
# ``cron/cron_rebuild_fts.py`` uses.
# ---------------------------------------------------------------------------


def repair_fts_drift(db_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Repair FTS5 drift by triggering an INSERT INTO fts(fts) VALUES('rebuild').

    Scenario 11 fix (2026-06-22): the FTS5 index can drift from the
    ``memories`` table if an FTS insert fails.  This function runs
    the standard rebuild (same as ``cron/cron_rebuild_fts.py``)
    and returns a report.

    Args:
        db_path: path to memory.db
        dry_run: if True, report what would be done without writing.

    Returns:
        dict with keys:
          - was_drifted (bool): True iff FTS5 was out of sync at the start
          - rebuild_ran (bool): True iff the rebuild command was issued
          - was_repaired (bool): True iff the post-rebuild FTS count == memories count
          - indexed_before (int): FTS count before
          - indexed_after (int): FTS count after
          - memories_count (int): memories row count
    """
    db_path = Path(db_path)
    result: dict[str, Any] = {
        "was_drifted": False,
        "rebuild_ran": False,
        "was_repaired": False,
        "indexed_before": 0,
        "indexed_after": 0,
        "memories_count": 0,
    }
    if not db_path.exists():
        return result
    with open_db(db_path, write=False) as db:
        tables = _get_table_names(db)
        if "memories" not in tables or "memories_fts" not in tables:
            return result
        mem_count = _get_memories_count(db)
        indexed = _get_fts_indexed_count(db, tables)
        result["memories_count"] = mem_count
        result["indexed_before"] = indexed or 0
        if indexed is None or mem_count == indexed:
            # No drift detected — nothing to do.
            return result
        result["was_drifted"] = True
        if dry_run:
            return result
        # The standard ``INSERT INTO fts(fts) VALUES('rebuild')`` is
        # only effective for external-content FTS5 tables — for
        # regular content FTS5 tables it just re-reads the FTS
        # table's own content, which is exactly the drifted set.
        # For content FTS5 we must wipe + re-insert from the source
        # table.  This works for both: external-content tables
        # don't have rows to wipe (so DELETE is a no-op), and
        # content tables get repopulated.
        try:
            fts_tables = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND sql LIKE '%USING fts5%'"
                ).fetchall()
            ]
            for fts_name in fts_tables:
                # Wipe + repopulate from the source table.  The
                # FTS table is named ``<source>_fts`` by convention.
                # (We don't use ``INSERT INTO fts(fts) VALUES('rebuild')``
                # because for content FTS5 tables it just re-reads
                # the FTS table's own content — exactly the drifted
                # set — so it doesn't help.)
                source = fts_name[: -len("_fts")]
                if not source:
                    continue
                fts_cols = {
                    row[1]
                    for row in db.execute(f'PRAGMA table_info("{fts_name}")').fetchall()
                }
                source_cols = {
                    row[1]
                    for row in db.execute(f'PRAGMA table_info("{source}")').fetchall()
                }
                common = sorted(fts_cols & source_cols)
                if not common:
                    logger.warning(
                        "repair_fts_drift: %s has no columns in "
                        "common with %s; skipping",
                        fts_name,
                        source,
                    )
                    continue
                db.execute(f'DELETE FROM "{fts_name}"')
                cols_sql = ", ".join(common)
                db.execute(
                    f'INSERT INTO "{fts_name}"(rowid, {cols_sql}) '
                    f"SELECT rowid, {cols_sql} FROM {source}"
                )
            db.commit()
            result["rebuild_ran"] = True
        except sqlite3.DatabaseError as e:
            logger.warning("repair_fts_drift: rebuild failed: %s", e)
            return result
        # Re-check.
        indexed_after = _get_fts_indexed_count(db, tables)
        result["indexed_after"] = indexed_after or 0
        result["was_repaired"] = (
            indexed_after is not None and indexed_after == mem_count
        )
    return result


# ---------------------------------------------------------------------------
# Scenario 7 fix (2026-06-22): orphan-file detection + recovery
# ---------------------------------------------------------------------------
#
# A "backward orphan" is a memories row whose source_file (.md path)
# is missing on disk.  This happens when the saga crashes between
# the DB upsert (step 1) and the file write (step 3) — e.g. SIGKILL,
# OOM, or power loss.  The DB has the canonical content, but the
# .md file is gone.  Without recovery, the next search hit on this
# note would surface content the user can no longer read in their
# editor.
#
# Forward orphans (vec_keys without memories) cannot exist under the
# FK constraint (memory_vec_keys.memory_id REFERENCES memories(id) ON
# DELETE CASCADE).  We still scan for them defensively, in case the
# schema was migrated without the FK or a manual DB edit broke it.
# ---------------------------------------------------------------------------


def find_orphan_files(
    db: AnyConnection, memory_root: Path
) -> list[dict[str, Any]]:
    """Return memories rows whose .md file is missing on disk.

    Each entry has: memory_id, source_file, content, tags_json,
    created_at, updated_at, observed_at, pinned, importance, md_path.
    The DB content is included so the caller can recover the file
    without a second SELECT.
    """
    if "memories" not in _get_table_names(db):
        return []
    findings: list[dict[str, Any]] = []
    try:
        rows = db.execute(
            """
            SELECT id, source_file, content, tags, created_at, updated_at,
                   observed_at, pinned, importance
            FROM memories
            WHERE deleted_at IS NULL
              AND source_file IS NOT NULL
              AND source_file != ''
            """
        ).fetchall()
    except sqlite3.DatabaseError as e:
        logger.warning("find_orphan_files: SELECT failed: %s", e)
        return findings
    for r in rows:
        (
            mid,
            source_file,
            content,
            tags_json,
            created_at,
            updated_at,
            observed_at,
            pinned,
            importance,
        ) = r
        md_path = memory_root / source_file
        if not md_path.exists():
            findings.append(
                {
                    "memory_id": mid,
                    "source_file": source_file,
                    "content": content,
                    "tags_json": tags_json,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "observed_at": observed_at,
                    "pinned": bool(pinned),
                    "importance": importance,
                    "md_path": str(md_path),
                }
            )
    return findings


def find_orphan_vec_keys(db: AnyConnection) -> list[dict[str, Any]]:
    """Defensive: return memory_vec_keys rows whose memory_id is gone.

    The FK constraint ``memory_id REFERENCES memories(id) ON DELETE
    CASCADE`` should make this impossible.  We scan defensively to
    surface schema-migration bugs or manual DB edits.
    """
    if "memory_vec_keys" not in _get_table_names(db):
        return []
    if "memories" not in _get_table_names(db):
        return []
    try:
        rows = db.execute(
            """
            SELECT v.key, v.memory_id
            FROM memory_vec_keys v
            LEFT JOIN memories m ON v.memory_id = m.id
            WHERE m.id IS NULL
            """
        ).fetchall()
    except sqlite3.DatabaseError as e:
        logger.warning("find_orphan_vec_keys: SELECT failed: %s", e)
        return []
    return [{"key": r[0], "memory_id": r[1]} for r in rows]


def _rebuild_md_for_memory(orphan: dict[str, Any]) -> str:
    """Reconstruct the .md file body from the DB row.

    Mirrors the frontmatter format that ``_build_memory_file``
    writes — keeping the structure round-trippable is more
    important than prettiness, because the next save will
    re-serialise it anyway.
    """
    import json as _json

    lines: list[str] = ["---"]
    if orphan.get("created_at"):
        lines.append(f"created: {orphan['created_at']}")
    if orphan.get("updated_at"):
        lines.append(f"updated: {orphan['updated_at']}")
    if orphan.get("observed_at"):
        lines.append(f"observed_at: {orphan['observed_at']}")
    tags_str = ""
    if orphan.get("tags_json"):
        try:
            tags = _json.loads(orphan["tags_json"])
            if isinstance(tags, list):
                tags_str = ", ".join(str(t) for t in tags)
        except (ValueError, TypeError):
            pass
    lines.append(f"tags: [{tags_str}]")
    lines.append(f"pinned: {'true' if orphan.get('pinned') else 'false'}")
    lines.append("related: []")
    valid_from = orphan.get("created_at") or ""
    lines.append(f"valid_from: {valid_from}")
    lines.append("valid_to: null")
    lines.append("superseded_by: null")
    lines.append("---")
    lines.append("")
    lines.append(orphan.get("content", ""))
    return "\n".join(lines)


def recover_orphan_files(
    db_path: Path,
    memory_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-create .md files for memories whose file is missing on disk.

    Scenario 7 fix (2026-06-22): the saga can crash between the DB
    upsert and the file write.  This function finds the resulting
    backward orphans and regenerates the .md files from the DB
    content (the DB is the canonical source of truth — the .md
    file is just a serialised view).

    Args:
        db_path: path to memory.db
        memory_root: path to the memory root (parent of category/
            subdirectories — the source_file is relative to this).
        dry_run: if True, report what would be recovered without
            writing any files.

    Returns:
        dict with keys:
          - recovered (list[str]): memory_ids whose file was re-created
          - failed (list[tuple[str, str]]): (memory_id, error) for failures
          - orphans (list[dict]): the full list of detected orphans
            (useful for callers that want to log or display)
    """
    db_path = Path(db_path)
    memory_root = Path(memory_root)
    recovered: list[str] = []
    failed: list[tuple[str, str]] = []
    if not db_path.exists():
        return {"recovered": [], "failed": [], "orphans": []}
    with open_db(db_path, write=False) as db:
        orphans = find_orphan_files(db, memory_root)
    for orphan in orphans:
        mid = orphan["memory_id"]
        md_path = Path(orphan["md_path"])
        body = _rebuild_md_for_memory(orphan)
        if dry_run:
            logger.info(
                "recover_orphan_files: would re-create %s (%d bytes)",
                md_path,
                len(body),
            )
            continue
        try:
            md_path.parent.mkdir(parents=True, exist_ok=True)
            # atomic_write lives in memory_common (re-exported at the
            # top of save_pipeline).  Importing here avoids a circular
            # import with memory_common at module-load time.
            from infra.memory_common import atomic_write

            atomic_write(md_path, body, encoding="utf-8")
            recovered.append(mid)
            logger.info(
                "recover_orphan_files: re-created %s for %s",
                md_path,
                mid,
            )
        except Exception as e:
            failed.append((mid, f"{type(e).__name__}: {e}"))
            logger.warning(
                "recover_orphan_files: failed to re-create %s: %s",
                md_path,
                e,
            )
    return {
        "recovered": recovered,
        "failed": failed,
        "orphans": orphans,
    }


def find_forward_orphan_files(
    db: AnyConnection, memory_root: Path
) -> list[dict[str, Any]]:
    """Return .md files on disk whose memories row no longer exists.

    A "forward orphan" is the mirror image of ``find_orphan_files``: a
    ``.md`` file present on disk but with no corresponding ``memories``
    row.  This is exactly the partial-state window W1 closes — a crash
    between the saga's ``.md`` write and the DB transaction commit can
    leave a dangling ``.md`` with no DB row.  These are safe to delete
    because the DB is the canonical source of truth.

    MEMORY.md (the human index) and ``.conflict-*`` / ``.flock`` files
    are never reported.
    """
    if "memories" not in _get_table_names(db):
        return []
    findings: list[dict[str, Any]] = []
    try:
        rows = db.execute(
            "SELECT id FROM memories WHERE deleted_at IS NULL"
        ).fetchall()
    except sqlite3.DatabaseError:
        return findings
    live_ids = {r[0] for r in rows}
    if not memory_root.exists():
        return findings
    for md_path in memory_root.rglob("*.md"):
        rel = md_path.relative_to(memory_root).as_posix()
        name = md_path.name
        # Skip the human index and conflict/lock sidecar files.
        if name == "MEMORY.md" or ".conflict" in name or md_path.suffix == ".lock":
            continue
        # Only consider files that look like category/title_slug.md.
        parts = rel.split("/")
        if len(parts) != 2:
            continue
        note_id = f"{parts[0]}/{md_path.stem}"
        if note_id not in live_ids:
            findings.append({"note_id": note_id, "md_path": str(md_path)})
    return findings


def reap_forward_orphan_files(
    db_path: Path,
    memory_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete .md files that have no corresponding memories row (W1).

    Safe to run at startup: a ``.md`` without a DB row is by definition
    not a real memory, so removing it cannot lose committed data.  The
    canonical DB row, if it exists, is untouched.
    """
    db_path = Path(db_path)
    memory_root = Path(memory_root)
    reaped: list[str] = []
    failed: list[tuple[str, str]] = []
    if not db_path.exists():
        return {"reaped": reaped, "failed": failed, "orphans": []}
    with open_db(db_path, write=False) as db:
        orphans = find_forward_orphan_files(db, memory_root)
    for orphan in orphans:
        p = Path(orphan["md_path"])
        if dry_run:
            logger.info("reap_forward_orphans: would delete %s", p)
            continue
        try:
            p.unlink()
            reaped.append(orphan["note_id"])
            logger.info("reap_forward_orphans: deleted %s", p)
        except Exception as e:
            failed.append((orphan["note_id"], f"{type(e).__name__}: {e}"))
            logger.warning("reap_forward_orphans: failed to delete %s: %s", p, e)
    return {"reaped": reaped, "failed": failed, "orphans": orphans}


def reconcile_orphan_files(
    db_path: Path,
    memory_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Self-heal partial save state at startup (W1).

    Combines the two directions:
      * backward orphans (DB row, missing ``.md``) are re-created from
        the DB via ``recover_orphan_files``;
      * forward orphans (``.md`` on disk, no DB row — the crash window
        between the saga's file write and DB commit) are reaped.

    After this runs, no partial memory survives a crash: either the DB
    row and ``.md`` both exist, or neither does.
    """
    backward = recover_orphan_files(db_path, memory_root, dry_run=dry_run)
    forward = reap_forward_orphan_files(db_path, memory_root, dry_run=dry_run)
    return {
        "backward_recovered": backward.get("recovered", []),
        "backward_failed": backward.get("failed", []),
        "forward_reaped": forward.get("reaped", []),
        "forward_failed": forward.get("failed", []),
    }


# ---------------------------------------------------------------------------
# B-3 fix (2026-06-22 follow-up): KG / backlinks orphan detection + repair
# ---------------------------------------------------------------------------
#
# A "KG orphan" is one of:
#   1. kg_edges rows whose source_id or target_id references an entity
#      that is no longer referenced by any kg_facts row.
#   2. kg_entities rows that are not referenced by any kg_facts row
#      AND not referenced by any kg_edges row.
#   3. backlinks rows whose source_id does not exist in the memories
#      table (target_id is allowed to be missing — wiki-style "red
#      links" are by design).
#
# These can accumulate from:
#   * Pre-migration-017 hard_delete_note() calls (before the
#     cascade FK was added).
#   * Saga rollbacks before the B-3 fix landed.
#   * Manual SQL edits or a partial migration.
#
# This module adds ``find_kg_orphans`` and ``repair_kg_orphans`` —
# mirroring the existing ``find_orphan_files`` / ``recover_orphan_files``
# pair from Scenario 7 (2026-06-22).
# ---------------------------------------------------------------------------


def find_kg_orphans(db: AnyConnection) -> dict[str, list[dict[str, Any]]]:
    """Return orphan rows in kg_edges, kg_entities, and backlinks.

    Returns a dict with three keys:
      - "kg_edges":   list of {id, source_id, target_id, relation}
      - "kg_entities": list of {id, name, entity_type}
      - "backlinks":  list of {source_id, target_id}

    An entity is an orphan when it has **no kg_facts reference** AND
    **no kg_edges reference** — both must be empty.  An edge is an
    orphan only when **both** endpoints are fact-less AND neither
    endpoint is connected (via any chain of kg_edges) to an entity
    that appears in kg_facts (bridge edges are preserved).

    Each list contains the rows that would be deleted by
    ``repair_kg_orphans``.  Pure read-only — no side effects.
    """
    tables = _get_table_names(db)
    out: dict[str, list[dict[str, Any]]] = {
        "kg_edges": [],
        "kg_entities": [],
        "backlinks": [],
    }

    if "kg_edges" in tables and "kg_entities" in tables and "kg_facts" in tables:
        try:
            rows = db.execute(
                """
                SELECT id, source_id, target_id, relation
                FROM kg_edges
                WHERE source_id IN (
                    SELECT id FROM kg_entities
                    WHERE name NOT IN (SELECT subject FROM kg_facts WHERE subject IS NOT NULL)
                       AND name NOT IN (SELECT object FROM kg_facts WHERE object IS NOT NULL)
                       AND id NOT IN (
                           SELECT DISTINCT e1.id FROM kg_entities e1
                           JOIN kg_edges e2 ON e2.source_id = e1.id OR e2.target_id = e1.id
                           JOIN kg_entities mid ON mid.id = e2.source_id OR mid.id = e2.target_id
                           WHERE mid.name IN (SELECT subject FROM kg_facts WHERE subject IS NOT NULL)
                              OR mid.name IN (SELECT object FROM kg_facts WHERE object IS NOT NULL)
                       )
                )
                AND target_id IN (
                    SELECT id FROM kg_entities
                    WHERE name NOT IN (SELECT subject FROM kg_facts WHERE subject IS NOT NULL)
                       AND name NOT IN (SELECT object FROM kg_facts WHERE object IS NOT NULL)
                       AND id NOT IN (
                           SELECT DISTINCT e1.id FROM kg_entities e1
                           JOIN kg_edges e2 ON e2.source_id = e1.id OR e2.target_id = e1.id
                           JOIN kg_entities mid ON mid.id = e2.source_id OR mid.id = e2.target_id
                           WHERE mid.name IN (SELECT subject FROM kg_facts WHERE subject IS NOT NULL)
                              OR mid.name IN (SELECT object FROM kg_facts WHERE object IS NOT NULL)
                       )
                )
                """
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            logger.warning("find_kg_orphans: kg_edges SELECT failed: %s", exc)
            rows = []
        out["kg_edges"] = [
            {"id": r[0], "source_id": r[1], "target_id": r[2], "relation": r[3]}
            for r in rows
        ]

    if "kg_entities" in tables and "kg_facts" in tables and "kg_edges" in tables:
        try:
            rows = db.execute(
                """
                SELECT e.id, e.name, e.entity_type
                FROM kg_entities e
                WHERE NOT EXISTS (
                    SELECT 1 FROM kg_facts f
                    WHERE f.subject = e.name OR f.object = e.name
                )
                AND NOT EXISTS (
                    SELECT 1 FROM kg_edges e2
                    JOIN kg_entities mid ON mid.id = e2.source_id OR mid.id = e2.target_id
                    WHERE e2.source_id = e.id OR e2.target_id = e.id
                      AND (mid.name IN (SELECT subject FROM kg_facts WHERE subject IS NOT NULL)
                           OR mid.name IN (SELECT object FROM kg_facts WHERE object IS NOT NULL))
                )
                """
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            logger.warning("find_kg_orphans: kg_entities SELECT failed: %s", exc)
            rows = []
        out["kg_entities"] = [
            {"id": r[0], "name": r[1], "entity_type": r[2]} for r in rows
        ]

    if "backlinks" in tables and "memories" in tables:
        try:
            rows = db.execute(
                """
                SELECT b.source_id, b.target_id
                FROM backlinks b
                LEFT JOIN memories m ON m.id = b.source_id
                    AND m.deleted_at IS NULL
                WHERE m.id IS NULL
                """
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            logger.warning("find_kg_orphans: backlinks SELECT failed: %s", exc)
            rows = []
        out["backlinks"] = [{"source_id": r[0], "target_id": r[1]} for r in rows]

    return out


def repair_kg_orphans(db_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Delete orphan kg_edges, kg_entities, and backlinks rows.

    B-3 fix (2026-06-22 follow-up): the saga rollback path (and pre-fix
    hard_delete_note calls) can leave orphan rows in these tables.
    ``find_kg_orphans`` reports them; this function removes them.

    Args:
        db_path: path to memory.db
        dry_run: if True, report what would be deleted without writing.

    Returns:
        dict with keys:
          - was_orphaned (bool): True iff any orphan rows were found
          - deleted_kg_edges (int)
          - deleted_kg_entities (int)
          - deleted_backlinks (int)
          - orphans (dict): the full per-table list of orphans
            (same shape as ``find_kg_orphans`` output)
    """
    db_path = Path(db_path)
    result: dict[str, Any] = {
        "was_orphaned": False,
        "deleted_kg_edges": 0,
        "deleted_kg_entities": 0,
        "deleted_backlinks": 0,
        "orphans": {"kg_edges": [], "kg_entities": [], "backlinks": []},
    }
    if not db_path.exists():
        return result
    with open_db(db_path, write=not dry_run) as db:
        orphans = find_kg_orphans(db)
        result["orphans"] = orphans
        if not any(orphans.values()):
            return result
        result["was_orphaned"] = True
        if dry_run:
            return result
        # Delete in order: kg_edges first (so the kg_entities cleanup
        # sees the post-delete state), then kg_entities, then
        # backlinks.  Each is wrapped in a try/except so a partial
        # failure still leaves us with a useful result.
        try:
            if orphans["kg_edges"]:
                ids = [r["id"] for r in orphans["kg_edges"]]
                placeholders = ",".join("?" for _ in ids)
                cur = db.execute(
                    f"DELETE FROM kg_edges WHERE id IN ({placeholders})",
                    ids,
                )
                result["deleted_kg_edges"] = int(cur.rowcount or 0)
        except sqlite3.DatabaseError as exc:
            logger.warning("repair_kg_orphans: kg_edges delete failed: %s", exc)
        try:
            if orphans["kg_entities"]:
                ids = [r["id"] for r in orphans["kg_entities"]]
                placeholders = ",".join("?" for _ in ids)
                cur = db.execute(
                    f"DELETE FROM kg_entities WHERE id IN ({placeholders})",
                    ids,
                )
                result["deleted_kg_entities"] = int(cur.rowcount or 0)
        except sqlite3.DatabaseError as exc:
            logger.warning("repair_kg_orphans: kg_entities delete failed: %s", exc)
        try:
            if orphans["backlinks"]:
                # Backlinks have a composite PK; delete by both columns.
                for row in orphans["backlinks"]:
                    cur = db.execute(
                        "DELETE FROM backlinks WHERE source_id = ? AND target_id = ?",
                        (row["source_id"], row["target_id"]),
                    )
                    result["deleted_backlinks"] += int(cur.rowcount or 0)
        except sqlite3.DatabaseError as exc:
            logger.warning("repair_kg_orphans: backlinks delete failed: %s", exc)
        db.commit()
    return result


def _parse_iso_date(s: str) -> float:
    """Parse an ISO date 'YYYY-MM-DD' (or full ISO datetime) to epoch."""
    from datetime import datetime, timezone

    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
        else:
            dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError as e:
        raise ValueError(f"could not parse date {s!r} as ISO: {e}")


def _run_temporal_query_cli(db_path: "Path", args: list[str]) -> int:
    """T4.6: dispatcher for ``--temporal-query`` CLI flag.

    The flag takes 1-3 positional args: ``OP [ARG1 [ARG2]]``:
      * ``at_time <iso_date> [text_filter]`` — facts valid at the date,
        optionally filtered by a substring of subject/predicate/object.
      * ``chain <fact_id>`` — walk the ``superseded_by`` chain.
      * ``changed_since <iso_date>`` — facts inserted or invalidated
        since the date.

    Opens a read-only URI connection so it works even when the live
    auto-save daemon holds the flock.
    """
    from fact.fact_temporal import (
        query_facts_at_time,
        query_fact_supersession_chain,
        query_facts_changed_since,
    )

    if not args:
        print(
            "ERROR: --temporal-query requires an operation: "
            "'at_time' / 'chain' / 'changed_since'",
            file=sys.stderr,
        )
        return 1
    op = args[0]
    rest = args[1:]
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"ERROR: db not found: {db_path}", file=sys.stderr)
        return 1

    try:
        ro_uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(ro_uri, uri=True) as db:
            if op == "at_time":
                if not rest:
                    print(
                        "ERROR: 'at_time' requires an ISO date (e.g. 2026-03-15)",
                        file=sys.stderr,
                    )
                    return 1
                as_of = _parse_iso_date(rest[0])
                text = rest[1] if len(rest) > 1 else None
                rows = query_facts_at_time(db, as_of, query=text, limit=100)
                print(
                    f"Facts valid at {rest[0]} (epoch {as_of:.0f})"
                    + (f" matching {text!r}" if text else "")
                    + f": {len(rows)}"
                )
                for r in rows:
                    print(
                        f"  [{r['id']:5d}] {r['subject']} {r['predicate']} "
                        f"{r['object']!r}"
                        + (
                            f" (event_time={r['event_time']:.0f}, "
                            f"granularity={r['event_time_granularity']})"
                            if r["event_time"] is not None
                            else ""
                        )
                    )
            elif op == "chain":
                if not rest:
                    print("ERROR: 'chain' requires a fact_id", file=sys.stderr)
                    return 1
                try:
                    fact_id = int(rest[0])
                except ValueError:
                    print(
                        f"ERROR: fact_id must be an integer, got {rest[0]!r}",
                        file=sys.stderr,
                    )
                    return 1
                rows = query_fact_supersession_chain(db, fact_id)
                print(f"Supersession chain for fact {fact_id}: {len(rows)} link(s)")
                for r in rows:
                    print(
                        f"  [{r['id']:5d}] {r['subject']} {r['predicate']} "
                        f"{r['object']!r}"
                        + (
                            f"  ← superseded by {r['superseded_by']} "
                            f"(reason: {r['invalidation_reason']})"
                            if r["superseded_by"] is not None
                            else "  (head of chain)"
                        )
                    )
            elif op == "changed_since":
                if not rest:
                    print(
                        "ERROR: 'changed_since' requires an ISO date",
                        file=sys.stderr,
                    )
                    return 1
                since_ts = _parse_iso_date(rest[0])
                rows = query_facts_changed_since(db, since_ts, limit=100)
                print(
                    f"Facts changed since {rest[0]} (epoch {since_ts:.0f}): {len(rows)}"
                )
                for r in rows:
                    changed_at = r["invalid_at"] or r["transaction_time"]
                    print(
                        f"  [{r['id']:5d}] {r['subject']} {r['predicate']} "
                        f"{r['object']!r}  @ {changed_at:.0f}"
                    )
            else:
                print(
                    f"ERROR: unknown operation {op!r}; "
                    "expected 'at_time' / 'chain' / 'changed_since'",
                    file=sys.stderr,
                )
                return 1
            return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check agentic-memory DB integrity")
    parser.add_argument("db_path", type=Path, help="Path to memory DB")
    parser.add_argument("--deep", action="store_true", help="Run deep integrity checks")
    parser.add_argument(
        "--recover-orphan-files",
        action="store_true",
        help=(
            "Re-create .md files for memories whose file is missing on disk. "
            "Scenario 7 fix (2026-06-22): the saga can crash between the DB "
            "upsert and the file write, leaving a 'backward orphan' (DB row "
            "with no .md file).  This recovers them by regenerating the .md "
            "from the DB content (the DB is the canonical source of truth)."
        ),
    )
    parser.add_argument(
        "--repair-fts-drift",
        action="store_true",
        help=(
            "Run the FTS5 rebuild to repair drift between memories and "
            "memory_fts.  Scenario 11 fix (2026-06-22): the FTS5 index can "
            "drift from the memories table if an FTS insert fails.  This "
            "re-runs the standard rebuild (same as cron/cron_rebuild_fts.py) "
            "and verifies the post-rebuild count matches the memories count."
        ),
    )
    parser.add_argument(
        "--repair-kg-orphans",
        action="store_true",
        help=(
            "Delete orphan rows in kg_edges, kg_entities, and backlinks.  "
            "B-3 fix (2026-06-22 follow-up): saga rollbacks and pre-fix "
            "hard_delete_note calls can leave orphan rows in these tables.  "
            "This finds and removes them.  Use --dry-run to preview."
        ),
    )
    parser.add_argument(
        "--memory-root",
        type=Path,
        default=None,
        help=(
            "Path to the memory root directory (parent of category/ "
            "subdirectories).  Defaults to <db_path.parent>.  Required "
            "if --recover-orphan-files is set and the memory root is not "
            "the DB's parent directory."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --recover-orphan-files, report what would be done without writing.",
    )
    parser.add_argument(
        "--temporal-summary",
        action="store_true",
        help=(
            "T3.5: print focused fact-level temporal KG stats "
            "(schema version, total facts, event_time coverage, "
            "supersession counts, reasons distribution). "
            "Useful for a quick health check of the temporal subsystem."
        ),
    )
    parser.add_argument(
        "--temporal-query",
        nargs="+",
        metavar=("OP", "ARG"),
        help=(
            "T4.6: time-aware query on the fact KG.  OP is one of: "
            "'at_time' / 'chain' / 'changed_since'.  ARG format depends on OP: "
            "'at_time <iso_date> [text_filter]', 'chain <fact_id>', "
            "'changed_since <iso_date>'.  Examples: "
            "--temporal-query at_time 2026-03-15, "
            "--temporal-query at_time 2026-03-15 'python', "
            "--temporal-query chain 42, "
            "--temporal-query changed_since 2026-06-20."
        ),
    )
    args = parser.parse_args(argv)

    if args.recover_orphan_files:
        memory_root = args.memory_root or args.db_path.parent
        result = recover_orphan_files(args.db_path, memory_root, dry_run=args.dry_run)
        mode = " (dry run)" if args.dry_run else ""
        print(f"Orphan files detected: {len(result['orphans'])}{mode}")
        for orphan in result["orphans"]:
            print(f"  - {orphan['memory_id']}: {orphan['md_path']}")
        if not args.dry_run:
            print(f"Recovered: {len(result['recovered'])}")
            for mid in result["recovered"]:
                print(f"  + {mid}")
            if result["failed"]:
                print(f"Failed: {len(result['failed'])}")
                for mid, err in result["failed"]:
                    print(f"  ! {mid}: {err}")
                return 1
        return 0

    if args.repair_fts_drift:
        result = repair_fts_drift(args.db_path, dry_run=args.dry_run)
        mode = " (dry run)" if args.dry_run else ""
        print(f"FTS drift check{mode}:")
        print(f"  memories count:     {result['memories_count']}")
        print(f"  FTS count (before): {result['indexed_before']}")
        if result["was_drifted"]:
            print(f"  FTS count (after):  {result['indexed_after']}")
            print(f"  rebuild_ran:        {result['rebuild_ran']}")
            print(f"  was_repaired:       {result['was_repaired']}")
            if not result["was_repaired"]:
                return 1
        else:
            print("  No drift detected — nothing to do.")
        return 0

    if args.repair_kg_orphans:
        result = repair_kg_orphans(args.db_path, dry_run=args.dry_run)
        mode = " (dry run)" if args.dry_run else ""
        print(f"KG orphan check{mode}:")
        print(f"  kg_edges orphans:    {len(result['orphans']['kg_edges'])}")
        print(f"  kg_entities orphans: {len(result['orphans']['kg_entities'])}")
        print(f"  backlinks orphans:   {len(result['orphans']['backlinks'])}")
        if result["was_orphaned"]:
            print(
                f"  deleted: kg_edges={result['deleted_kg_edges']}, "
                f"kg_entities={result['deleted_kg_entities']}, "
                f"backlinks={result['deleted_backlinks']}"
            )
        else:
            print("  No orphans detected — nothing to do.")
        return 0

    if args.temporal_summary:
        # T3.5: focused fact-level temporal KG stats
        summary = temporal_summary(args.db_path)
        print("Temporal KG summary (T3.5):")
        if summary.get("error"):
            print(f"  error: {summary['error']}")
            return 1
        print(f"  schema_version:           {summary['schema_version']}")
        print(f"  facts_total:              {summary['facts_total']}")
        print(
            f"  facts_with_event_time:    {summary['facts_with_event_time']} "
            f"({100.0 * summary['facts_with_event_time'] / max(summary['facts_total'], 1):.1f}%)"
        )
        print(f"  facts_superseded:         {summary['facts_superseded']}")
        print(f"  facts_superseding:        {summary['facts_superseding']}")
        print(f"  supersessions_last_7d:    {summary['supersessions_last_7d']}")
        if summary["supersessions_by_reason"]:
            print("  supersessions_by_reason:")
            for reason, n in summary["supersessions_by_reason"].items():
                print(f"    {reason}: {n}")
        return 0

    if args.temporal_query:
        # T4.6: time-aware query via the CLI
        return _run_temporal_query_cli(args.db_path, args.temporal_query)

    result = check_index_integrity(args.db_path, deep=args.deep)

    status = "OK" if result["ok"] else "FAIL"
    print(f"Status : {status}")
    print(f"Summary: {result['summary']}")
    print(f"Findings ({len(result['findings'])}):")
    for f in result["findings"]:
        sev = f["severity"].upper()
        print(f"  [{sev:8s}] {f['check']:20s} {f['message']}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
