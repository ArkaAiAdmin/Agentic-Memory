"""Versioned SQL migration runner for agentic-memory.

Replaces the inline Python migrations in memory_common.py with
numbered SQL files in the migrations/ directory.

Design:
  - Migrations are numbered 001, 002, 003, ... (lexicographic order)
  - Each .sql file contains one or more SQL statements separated by semicolons
  - Comments (-- ...) and empty lines are ignored
  - The schema_version table tracks which migrations have been applied
  - Idempotent: re-running on a fully-migrated DB is a no-op
  - Backward compatible: existing DBs with schema_version=4 are treated
    as having migrations 001-004 already applied
  - Checksums: each applied migration stores a SHA256 of its file in
    schema_version.checksums so post-apply tampering is detected on
    the next run. DBs that were upgraded before checksums were added
    get backfilled on first post-upgrade access.

Current schema version: 36.

Usage:
    from infra.migration_runner import run_migrations
    run_migrations(conn)
"""

from __future__ import annotations

import logging

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

# Directory containing the .sql migration files
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Current schema version. Bump when adding new migrations.
# Must match the highest-numbered migration file.
# 2026-06-20: bumped to 13 for memory_field_crdt (v13 per-field
# LWWES CRDT). See migrations/013_field_level_crdt.sql.
# 2026-06-22: bumped to 14 for arc_ghosts + arc_stats tables
# (P0 fix #4 — wire ARCCache into the live eviction path).
# 2026-06-22: bumped to 15 for drift_alarms (per-memory,
# severity-tiered concept-drift alarm tracking — AGENTS.md
# listed "drift alarms" as one of the 25 user-visible tables
# but it did not exist). See migrations/015_drift_alarms.sql.
# 2026-06-22: bumped to 16 for concept_drift (D1 fix — the table
# was previously created in Python via db_migrations._migrate_concept_drift
# which violated the "schema changes go in numbered .sql files" rule).
# See migrations/016_concept_drift.sql. The Python helper stays as a
# safety net.
# 2026-06-22 (follow-up): bumped to 17 for kg_cascade (B-3 fix).  Adds
# ON DELETE CASCADE to kg_edges (with ON DELETE SET NULL on the FK to
# kg_entities — entities are shared) and backlinks.  Closes the audit
# gap where the saga rollback path and any direct DELETE FROM memories
# statements left orphan rows.  See migrations/017_kg_cascade.sql.
# 2026-06-23 (follow-up): bumped to 18 for fact_temporal (T1 of the
# temporal-kg plan).  Adds fact-level bi-temporal validity columns
# (event_time, transaction_time, valid_at, invalid_at, superseded_by,
# supersedes, contradiction_score, invalidation_reason) to kg_facts —
# the missing piece for time-travel queries ("what did we know on
# date X?") and automatic supersession when a new fact contradicts
# an old one.  See migrations/018_fact_temporal.sql.
# gap where the saga rollback path left orphan rows in those tables.
# See migrations/017_kg_cascade.sql.
# 2026-06-23 (follow-up): bumped to 19 for kg_facts entity FKs.
# kg_facts.subject_entity_id and object_entity_id were FKs to
# kg_entities(id) with no ON DELETE clause. kg_dedup.merge_entities()
# deletes the merged entity, which violated the FK. The background
# worker was failing every 5 minutes with "FOREIGN KEY constraint
# failed" (24 occurrences in worker.log).  Fix: add ON DELETE SET
# NULL to both FKs.  See migrations/019_kg_facts_entity_fk.sql.
# 2026-06-23 (follow-up): bumped to 20 for kg_facts FTS5 index.
# kg_facts was the only text-searchable table without an FTS5 virtual
# table (memories, memory_chunks, kg_entities all have one).  Without
# FTS, facts_search() in fact_extraction.py uses LIKE %query% which
# is O(n) on the table.  This migration adds kg_facts_fts (FTS5,
# content='kg_facts', content_rowid='id') + 3 sync triggers (ai, ad,
# au) that keep the FTS table in lockstep with kg_facts, plus a
# backfill of existing facts.  See migrations/020_kg_facts_fts.sql.
# 2026-06-23 (follow-up): bumped to 21 for Graph CRDTs (kg_crdt).
# 2026-06-26: bumped to 22 for Session Memory System (sessions,
# decision_threads, thread_events, session_compaction_log).
# 2026-07-02: bumped to 24 for chunk-level multi-vector search (memory_chunk_embeddings,
# memory_chunk_vec_idx, memory_chunk_vec_keys).
# 2026-07-03: bumped to 26 for belief_assertions table + kg_facts.fact_type
# (Sprint 1 fact/belief separation).
# 2026-07-03: bumped to 28 for entailment_chains + memory_revision_log (Sprint 3).
# 2026-07-03: bumped to 29 for graph_snapshots table (Sprint 4 graph analytics).
# 2026-07-03: bumped to 30 for community_id + betweenness on kg_entities (Sprint 4).
# 2026-07-05: bumped to 31 for outbox memory_events table and triggers (REST/WS API).
# 2026-07-05: bumped to 32 for scoped outbox update trigger (semantic columns only).
# 2026-07-08: bumped to 35 for shared_memories target_agent_id + shared_with columns (B3.1).
# 2026-07-08: bumped to 36 for embedding model tracking in memory_vec_idx (C4.3).
# 2026-07-10: bumped to 37 for cron_runs execution tracking table.
# 2026-07-11: bumped to 38 for inception-fingerprint identity on kg_entities.
# 2026-07-11: bumped to 41 — UNIQUE(fingerprint) replaces UNIQUE(name, entity_type).
# 2026-07-11: bumped to 44 — tenant_id NOT NULL + principals + audit tenant_id.
# 2026-07-11: bumped to 46 — RBAC schema (roles, bindings, policies, acl, audit) + seed.
# 2026-07-11: bumped to 47 — SSO addressable signing keys (idem_token_key) + IdP metadata cache.
# 2026-07-11: bumped to 48 — principal_identities multi-tenant (UNIQUE includes tenant_id).
# 2026-07-11: bumped to 49 — GDPR right-to-be-forgotten request tracking.
# 2026-07-11: bumped to 50 — tenant_id on kg_entities/kg_facts.
# 2026-07-11: bumped to 52 — backfill kg tenant_id from parent memory (052).
# 2026-07-11: bumped to 53 — tenant_id column on memory_field_crdt (051).
# 2026-07-13: bumped to 57 — search-reranker foundation (057):
#   memory_search_interaction, memory_query_type_stats, memory_temporal_priors
#   (+ seed 7 rows into memory_temporal_priors).
# 2026-07-15: bumped to 62 for data_subject_sub on memories (GDPR subject-scoped erase).
# 2026-07-15: bumped to 61 for composite (query_id, id) PK on
#   memory_ctr_feedback (FIX 2 — real CTR click/dismiss correlation).
SCHEMA_VERSION = 63

# Schema is locked at the version above. Set to False when a new
# migration is intentionally added, then back to True once the
# new migration is committed.
SCHEMA_STABLE = True



def _parse_sql_file(path: Path) -> list[str]:
    """Parse a .sql file into a list of executable statements.

    Strips single-line comments and empty lines, then splits on
    semicolons that are *not* inside a CREATE TRIGGER ... BEGIN ... END
    block.  The previous implementation naively split on every ";"
    character, which broke ``CREATE TRIGGER`` statements whose body
    legitimately contains semicolons (e.g. embedded INSERT statements)
    — the half-baked body would then be run as malformed SQL and
    silently fail.
    """
    content = path.read_text(encoding="utf-8")
    # Strip single-line comments first; SQL doesn't allow block comments
    # inline with multi-line tables outside of triggers, but we'll be
    # defensive and cope with them too.
    raw_lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if "--" in stripped:
            stripped = stripped[: stripped.index("--")].strip()
        if stripped:
            raw_lines.append(stripped)
    full_text = " ".join(raw_lines)

    # Tokenize: walk character-by-character, but only treat a ";" as a
    # statement terminator if we're not currently inside a BEGIN ... END
    # block (trigger bodies).
    statements: list[str] = []
    buf: list[str] = []
    depth = 0  # BEGIN ... END nesting depth
    i = 0
    n = len(full_text)
    in_single = (
        False  # SQLite doesn't really use single-quoted identifiers; track for safety
    )
    while i < n:
        ch = full_text[i]
        # Track BEGIN/END keyword boundaries (whitespace-delimited).
        if not in_single and ch.isalpha():
            j = i
            while j < n and (full_text[j].isalpha() or full_text[j] == "_"):
                j += 1
            word = full_text[i:j].upper()
            if word == "BEGIN":
                depth += 1
            elif word == "END":
                depth = max(0, depth - 1)
            buf.append(full_text[i:j])
            i = j
            continue
        if not in_single and ch == ";" and depth == 0:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        if ch == "'":
            in_single = not in_single
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _get_applied_migrations(conn: AnyConnection) -> set[int]:
    """Read the set of already-applied migration numbers.

    Uses the schema_version table. For backward compatibility with
    DBs that have schema_version <= 4 (the old inline system),
    returns {1, 2, 3, 4} as baseline.

    2026-06-22 (D7 fix): the previous implementation trusted
    ``schema_version.version`` to be the literal set of applied
    migrations, which meant a fresh DB that ran ``run_migrations``
    would claim versions 1..N were all applied even if the .sql
    files for some of them did not exist on disk.  We now intersect
    the recorded version with the actual files present, so:
      - a DB that says ``version=15`` but is missing
        ``migrations/009_kg_facts_entity_fks.sql`` will correctly
        re-run 009 the next time the runner starts.
      - a fresh DB that has no ``schema_version`` row is still
        treated as "nothing applied" (so the runner applies 1..N).
    """
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        if row is None:
            return set()
        version = row[0]
        # A version of 0 means "no migrations applied" (fresh DB or
        # fully rolled back via migrate_down).  Return empty so all
        # available migrations are re-applied.
        if version <= 0:
            return set()
        # Backward compat: old schema_version=4 means migrations 1-4
        # are already applied (they correspond to the old inline helpers).
        if version <= 4:
            return {1, 2, 3, 4}
        # For version >= 5, the version itself IS the highest applied
        # migration number, but only the migrations that actually
        # exist on disk are considered applied.  This protects
        # against drift between the recorded version and the file
        # set (e.g., a partial checkout, a manual file move, or a
        # downgrade where a new migration was rolled back via
        # ``migrate_down`` but the version row was bumped past it
        # by accident).
        available_nums = {num for num, _ in _get_available_migrations()}
        return {n for n in range(1, version + 1) if n in available_nums}
    except sqlite3.OperationalError:
        return set()


def _get_available_migrations() -> list[tuple[int, Path]]:
    """Discover numbered .sql files in the migrations/ directory.

    Returns sorted list of (number, path) tuples. Excludes `.down.sql`
    rollback files — those are only discovered by `_get_down_migrations`.
    """
    if not MIGRATIONS_DIR.exists():
        return []
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name.endswith(".down.sql"):
            continue
        try:
            num = int(path.stem.split("_")[0])
            migrations.append((num, path))
        except (ValueError, IndexError):
            logger.warning("Skipping non-numbered migration file: %s", path.name)
    return sorted(migrations)


def _get_down_migrations() -> list[tuple[int, Path]]:
    """Discover numbered .down.sql files in the migrations/ directory.

    Returns sorted list of (number, path) tuples.
    """
    if not MIGRATIONS_DIR.exists():
        return []
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("*.down.sql")):
        try:
            num = int(path.stem.split("_")[0])
            migrations.append((num, path))
        except (ValueError, IndexError):
            logger.warning("Skipping non-numbered down migration file: %s", path.name)
    return sorted(migrations)


def _ensure_checksums_column(conn: AnyConnection) -> None:
    """Add checksums column to schema_version if it doesn't exist."""
    try:
        conn.execute(
            "ALTER TABLE schema_version ADD COLUMN checksums TEXT DEFAULT '{}'"
        )
    except sqlite3.OperationalError:
        pass


def _get_checksums(conn: AnyConnection) -> dict[str, str]:
    """Read stored migration file checksums from schema_version.

    Returns {migration_number_str: sha256_hex}. Returns empty dict if
    the column or row is missing.
    """
    try:
        row = conn.execute(
            "SELECT checksums FROM schema_version WHERE id=1"
        ).fetchone()
        if row and row[0]:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                return parsed
    except (sqlite3.OperationalError, json.JSONDecodeError, TypeError):
        pass
    return {}


def _backfill_empty_checksums(conn: AnyConnection) -> None:
    """Backfill empty checksums on upgraded DBs (M5).

    DBs that were migrated before the checksums column existed will
    have schema_version.version >= current applied set but an empty
    checksums dict (the column's DEFAULT '{}' kicked in). We
    backfill only when the column already exists AND the stored
    dict is empty/None — once a DB has any checksum written, the
    fail-closed integrity gate kicks in for all future runs.
    """
    try:
        col_check = conn.execute(
            "PRAGMA table_info(schema_version)"
        ).fetchall()
        has_checksums_col = any(r[1] == "checksums" for r in col_check)
    except sqlite3.OperationalError:
        return
    if not has_checksums_col:
        return
    existing = _get_checksums(conn)
    if existing:
        return  # fail-closed: once checksums are present, never overwrite
    available = {num: path for num, path in _get_available_migrations()}
    new_checksums: dict[str, str] = {}
    for num, path in available.items():
        new_checksums[str(num)] = hashlib.sha256(path.read_bytes()).hexdigest()
    if new_checksums:
        conn.execute(
            "UPDATE schema_version SET checksums = ? WHERE id = 1",
            (json.dumps(new_checksums),),
        )


def verify_checksums(conn: AnyConnection) -> list[tuple[int, str, str, str]]:
    """Verify on-disk migration file checksums against stored hashes.

    Returns a list of (migration_number, filename, stored_hash, actual_hash)
    tuples for every file whose hash does not match. An empty list means
    all applied migrations pass integrity check. Unapplied migrations and
    migration files without a stored checksum are not reported.
    """
    stored = _get_checksums(conn)
    if not stored:
        return []
    available = {num: path for num, path in _get_available_migrations()}
    mismatches: list[tuple[int, str, str, str]] = []
    for num_str, stored_hash in stored.items():
        num = int(num_str)
        path = available.get(num)
        if path is None:
            continue
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != stored_hash:
            mismatches.append((num, path.name, stored_hash, actual_hash))
    return mismatches


def _enforce_checksum_integrity(conn: AnyConnection) -> None:
    """Verify stored checksums of applied migrations against current files.

    On subsequent runs, every already-applied migration file is hashed and
    compared to the SHA256 recorded when it was applied. If any file has
    been modified (tampered, edited, or rolled back incompletely), this
    raises a ``RuntimeError`` and refuses to apply further migrations until
    the schema integrity is restored. This is the OWASP A08-002 control.
    """
    mismatches = verify_checksums(conn)
    if mismatches:
        lines = [
            f"  {num:03d} {fname}: stored={stored} current={actual}"
            for num, fname, stored, actual in mismatches
        ]
        raise RuntimeError(
            "Migration checksum verification FAILED. The following applied "
            "migration file(s) no longer match the SHA256 recorded when they "
            "were applied:\n"
            + "\n".join(lines)
            + "\nRefusing to apply further migrations. Restore the original "
            "migration file(s), or roll back and re-apply them cleanly."
        )


def run_migrations(conn: AnyConnection, dry_run: bool = False) -> None:
    """Apply all pending migrations.

    This is the forward-only entry point for schema evolution. It
    replaces the inline Python migrations in memory_common.py.

    For reverse / rollback migrations, use migrate_down().

    Steps:
       1. Ensure schema_version table exists
       2. Ensure base schema (memories table + KG tables) via migration 000
       3. Read applied migrations
       4. Discover available .sql files
       5. Apply pending ones in order
       6. Update schema_version to current SCHEMA_VERSION
    """
    # Step 1: Ensure schema_version table exists
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  id      INTEGER PRIMARY KEY CHECK (id = 1),"
        "  version INTEGER NOT NULL"
        "  )"
    )
    # Step 2 is now handled by migrations/000_base_schema.sql (base tables
    # and KG tables). The numbered migrations below it assume those tables
    # are already present.
    _ensure_checksums_column(conn)
    _backfill_empty_checksums(conn)

    # Step 2: Read applied migrations
    applied = _get_applied_migrations(conn)

    # Step 3: Integrity gate — refuse to apply further migrations if any
    # already-applied migration file's SHA256 no longer matches the stored
    # checksum (OWASP A08-002).
    _enforce_checksum_integrity(conn)

    # Step 4: Discover available migrations
    available = _get_available_migrations()

    # Step 5: Filter to pending
    pending = [(num, path) for num, path in available if num not in applied]

    if not pending:
        return

    if SCHEMA_STABLE and any(num > SCHEMA_VERSION for num, _ in pending):
        offending = sorted({num for num, _ in pending if num > SCHEMA_VERSION})
        raise RuntimeError(
            f"Schema is locked at v{SCHEMA_VERSION} (SCHEMA_STABLE=True). "
            f"Cannot apply migration(s) > {SCHEMA_VERSION}: {offending}. "
            "Set SCHEMA_STABLE = False in migration_runner.py to add new migrations."
        )

    if dry_run:
        logger.info("[DRY RUN] Would apply %d migration(s):", len(pending))
        for num, path in pending:
            stmts = _parse_sql_file(path)
            logger.info("  %03d: %s (%d statement(s))", num, path.name, len(stmts))
            for stmt in stmts:
                for line in stmt.split("\n"):
                    logger.info("    %s", line)
        logger.info("  schema_version would advance to %d", SCHEMA_VERSION)
        return

    # Step 6: Apply pending migrations in a single transaction
    try:
        # Disable foreign keys before DDL transaction
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
        except Exception:
            pass
        with conn:
            for num, path in pending:
                logger.info("Applying migration %03d: %s", num, path.name)
                statements = _parse_sql_file(path)
                for stmt in statements:
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        # Ignore errors that are expected for idempotent
                        # migrations:
                        #  * "already exists" / "duplicate X" — the object
                        #    was created by a previous run
                        #  * "no such table" / "no such column" — the
                        #    referenced object will be created by a later
                        #    migration (migrations have some forward
                        #    references that are resolved later in the
                        #    migration sequence)
                        # All other errors (syntax errors, constraint
                        # violations, type mismatches) are re-raised to
                        # avoid silent corruption.
                        msg = str(e).lower()
                        if any(
                            re.search(rf"\b{re.escape(kw)}\b", msg)
                            for kw in (
                                "already exists",
                                "duplicate column",
                                "duplicate index",
                                "table already exists",
                                "index already exists",
                                "trigger already exists",
                            )
                        ):
                            logger.warning(
                                "Migration %03d statement failed (idempotent, "
                                "object already exists): %s",
                                num,
                                e,
                            )
                        elif any(
                            re.search(rf"\b{re.escape(kw)}\b", msg)
                            for kw in ("no such table",)
                        ):
                            logger.warning(
                                "Migration %03d statement references object "
                                "created by a later migration (%s); "
                                "this is expected.",
                                num,
                                e,
                            )
                        # NOTE: the "no such column" guard below is
                        # UNREACHABLE in practice — SQLite's
                        # "duplicate column" and "no such column"
                        # errors are mutually exclusive for the same
                        # statement. The first branch (line ~498)
                        # already catches "duplicate column" via the
                        # "duplicate column name" substring match.
                        # Kept as no-op to document original intent.
                        elif False:  # pragma: no cover
                            pass
                        else:
                            logger.error(
                                "Migration %03d statement failed (non-idempotent): %s",
                                num,
                                e,
                            )
                            raise

            # Build checksums for newly applied migrations, then write
            # version + checksums to schema_version.
            checksums = _get_checksums(conn)
            for num, path in pending:
                checksums[str(num)] = hashlib.sha256(path.read_bytes()).hexdigest()

            highest_applied = max(num for num, _ in pending)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (id, version, checksums) VALUES (1, ?, ?)",
                (highest_applied, json.dumps(checksums)),
            )
            # If the cap SCHEMA_VERSION is higher than the highest
            # applied migration in this run (e.g., a migration was
            # skipped because of the file-presence filter in
            # ``_get_applied_migrations``), bump once more to the
            # cap.  This is idempotent.
            if SCHEMA_VERSION > highest_applied:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_version (id, version, checksums) VALUES (1, ?, ?)",
                    (SCHEMA_VERSION, json.dumps(checksums)),
                )

        # Post-migration hooks. Run AFTER the transaction commits so
        # the backfill can be retried independently if it fails.
        # Each hook is keyed on the migration it accompanies so a
        # partial migration + hook run can be resumed.
        if any(num == 13 for num, _ in pending):
            try:
                from crdt.crdt_field import backfill_from_memories

                count = backfill_from_memories(conn)
                logger.info(
                    "Migration 013: backfilled %d memory rows into memory_field_crdt",
                    count,
                )
            except Exception as e:
                logger.warning(
                    "Migration 013: backfill failed (non-fatal; will "
                    "retry on next save via _seed_note_into_field_crdt): %s",
                    e,
                )
    except Exception as e:
        logger.error("Migration failed: %s", e)
        raise
    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception as e:
            logger.warning("run_migrations failed: %s", e)


def migrate_down(conn: AnyConnection, target_version: int, dry_run: bool = False) -> None:
    """Rollback migrations to target_version.

    Applies down-migrations in reverse order (highest to lowest) for
    all migrations that are > target_version and have a corresponding
    .down.sql file.  Updates schema_version to target_version on
    success.  Wraps the whole sequence in a transaction.
    """
    # Ensure schema_version table exists
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  id      INTEGER PRIMARY KEY CHECK (id = 1),"
        "  version INTEGER NOT NULL"
        "  )"
    )
    _ensure_checksums_column(conn)

    applied = _get_applied_migrations(conn)
    down_migrations = _get_down_migrations()
    # Build a map for quick lookup
    down_map = {num: path for num, path in down_migrations}

    # Determine which migrations to roll back
    to_rollback = sorted(
        [num for num in applied if num > target_version and num in down_map],
        reverse=True,
    )

    if not to_rollback:
        logger.info(
            "No down-migrations needed to reach target version %d", target_version
        )
        return

    if dry_run:
        logger.info("[DRY RUN] Would roll back %d migration(s):", len(to_rollback))
        for num in to_rollback:
            path = down_map[num]
            stmts = _parse_sql_file(path)
            logger.info("  %03d: %s (%d statement(s))", num, path.name, len(stmts))
            for stmt in stmts:
                for line in stmt.split("\n"):
                    logger.info("    %s", line)
        logger.info("  schema_version would regress to %d", target_version)
        return

    try:
        # Disable foreign keys before DDL transaction
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
        except Exception:
            pass
        with conn:
            for num in to_rollback:
                path = down_map[num]
                logger.info("Applying down-migration %03d: %s", num, path.name)
                statements = _parse_sql_file(path)
                for stmt in statements:
                    conn.execute(stmt)

            # Remove checksums for rolled-back migrations, then write
            # version + checksums to schema_version.  Recreate the table
            # if a down-migration (e.g. 001) dropped it.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "  id INTEGER PRIMARY KEY CHECK (id = 1),"
                "  version INTEGER NOT NULL"
                ")"
            )
            _ensure_checksums_column(conn)
            checksums = _get_checksums(conn)
            for num in to_rollback:
                checksums.pop(str(num), None)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (id, version, checksums) VALUES (1, ?, ?)",
                (target_version, json.dumps(checksums)),
            )
    except Exception as e:
        logger.error("Down-migration failed: %s", e)
        raise
    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception as e:
            logger.warning("migrate_down failed: %s", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run agentic-memory migrations")
    parser.add_argument(
        "--target-version",
        type=int,
        default=None,
        help="Target schema version to roll back to (triggers down-migrations)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to the SQLite database file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending migrations and SQL without executing",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify migration file checksums against stored hashes",
    )
    args = parser.parse_args()

    if args.db is None:
        print("Usage: python migration_runner.py --db <path> [--target-version <N>] [--dry-run] [--verify]")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        if args.verify:
            mismatches = verify_checksums(conn)
            if not mismatches:
                print("All migration checksums match.")
            else:
                print(f"Checksum mismatch(es) detected ({len(mismatches)}):")
                for num, fname, stored, actual in mismatches:
                    print(f"  {num:03d} {fname}")
                    print(f"    stored: {stored}")
                    print(f"    actual: {actual}")
                sys.exit(1)
        elif args.target_version is not None:
            migrate_down(conn, args.target_version, dry_run=args.dry_run)
        else:
            run_migrations(conn, dry_run=args.dry_run)
    finally:
        conn.close()
