"""Field-level LWW-Element-Set (LWWES) CRDT for multi-agent memory.

This module implements a TRUE CRDT (commutative, associative, idempotent,
convergent) at the field level, replacing the note-level LWW in
``crdt_merge.py`` for v13 of the schema (2026-06-20).

Background
----------
The pre-v13 ``crdt_merge.py`` was a "LWW with version-vector
happened-before detection" over the whole note. Two agents editing
different fields of the same note would see one side's entire note
win outright. Observers correctly called this "not a real CRDT."

LWWES per field
---------------
We use the LWW-Element-Set pattern: each field is a register with its
own (value, version_vector, logical_clock, last_writer_agent) tuple.
Concurrent writes to the SAME field are resolved by LWW on
(logical_clock, agent_id). Concurrent writes to DIFFERENT fields do
not interact — both win.

The four CRDT properties hold:

  1. Commutativity: ``merge_fields(a, b) == merge_fields(b, a)``
     for the same field, because LWW is a total order and the merge
     is applied per field independently.

  2. Associativity: ``merge_fields(merge(a, b), c) == merge(a,
     merge(b, c))``, because LWW tiebreaker is a total order.

  3. Idempotence: ``merge_fields(a, a) == a``, because LWW is
     deterministic.

  4. Convergence: all replicas applying the same set of field
     updates (in any order) reach the same state. (This is a
     consequence of (1)+(2) — the standard "strong eventual
     consistency" proof.)

CRDT storage model
------------------
State lives in the ``memory_field_crdt`` table (schema v13):

    (memory_id, field_name) -> (value, version_vector, logical_clock,
                                last_writer_agent, is_deleted, updated_at)

Tombstones (``is_deleted = 1``) are part of the CRDT. A tombstone
from agent A will eventually overwrite a live value from agent B
ONLY IF A's tombstone is causally after B's write (i.e., A's VV
dominates B's VV, or they are concurrent and A's logical clock
wins the LWW tiebreaker).

Public API
----------
The module exposes:

  - ``FieldUpdate``: a dataclass for a single field write.
  - ``merge_field_updates()``: merge two sets of field updates,
    producing a new set of (winner, is_conflict) pairs.
  - ``apply_field_updates_to_db()``: apply a set of field updates
    to a SQLite DB using the LWWES rule.
  - ``read_fields()``: read all live fields for a memory_id.
  - ``ensure_field_crdt_schema()``: idempotent CREATE TABLE IF NOT
    EXISTS for the field-crdt table (mirrors the migration).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, cast

logger = logging.getLogger(__name__)

# The set of fields we replicate as CRDTs. Keep this list small and
# stable — adding a field is a schema change, removing one is
# allowed (reads just skip missing fields).
#
# Notes:
#   * ``content`` is the body of the note. Always replicated.
#   * ``tags`` is JSON-encoded list. Always replicated.
#   * ``category`` is a short string. Always replicated.
#   * ``importance`` and ``pinned`` are intentionally NOT in this
#     list — they're managed by the local retention/importance
#     subsystem and would conflict on merge. Replicate as
#     note-level scalars instead.
REPLICATED_FIELDS: tuple[str, ...] = ("content", "tags", "category")


@dataclass(frozen=True)
class FieldUpdate:
    """A single field write to be merged into the CRDT.

    Immutable. Use ``replace()`` to create a new value with one
    field changed.
    """

    memory_id: str
    field_name: str
    value: str
    version_vector: dict[str, int] = field(default_factory=dict)
    logical_clock: int = 0
    last_writer_agent: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for transport (sync protocol, JSON)."""
        return {
            "memory_id": self.memory_id,
            "field_name": self.field_name,
            "value": self.value,
            "version_vector": dict(self.version_vector),
            "logical_clock": self.logical_clock,
            "last_writer_agent": self.last_writer_agent,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FieldUpdate":
        """Deserialize from a transport dict."""
        return cls(
            memory_id=str(d["memory_id"]),
            field_name=str(d["field_name"]),
            value=str(d["value"]),
            version_vector=dict(d.get("version_vector") or {}),
            logical_clock=int(d.get("logical_clock", 0)),
            last_writer_agent=str(d.get("last_writer_agent", "")),
        )


# ---------------------------------------------------------------------------
# Vector-clock helpers (mirrors of crdt_merge.py; kept local so
# this module has no import cycle).
# ---------------------------------------------------------------------------


def _vv_dominates(a: dict[str, int], b: dict[str, int]) -> bool:
    """Return True if vector clock ``a`` causally dominates ``b``.

    a dominates b iff for every agent x, a[x] >= b[x], and there
    exists at least one agent where a[x] > b[x].
    """
    keys = set(a) | set(b)
    at_least_one_greater = False
    for k in keys:
        av = a.get(k, 0)
        bv = b.get(k, 0)
        if av < bv:
            return False
        if av > bv:
            at_least_one_greater = True
    return at_least_one_greater


def _vv_concurrent(a: dict[str, int], b: dict[str, int]) -> bool:
    """Return True if ``a`` and ``b`` are concurrent (neither dominates)."""
    return not _vv_dominates(a, b) and not _vv_dominates(b, a)


def _lww_tiebreak(
    clock_a: int,
    agent_a: str,
    clock_b: int,
    agent_b: str,
) -> str:
    """Decide the LWW winner between two writes to the same field.

    Returns "a" or "b". The tiebreaker is (clock desc, agent asc)
    so the result is fully deterministic across replicas.
    """
    if clock_a > clock_b:
        return "a"
    if clock_b > clock_a:
        return "b"
    # Same clock: agent_id ascending wins. This is the deterministic
    # total order that makes CRDT merge associative.
    return "a" if agent_a <= agent_b else "b"


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------


def merge_field_updates(
    updates: Iterable[FieldUpdate],
) -> list[FieldUpdate]:
    """Merge a set of field updates and return the winners.

    For each (memory_id, field_name), the winning update is:
      * The unique update, if only one exists.
      * The causally-dominating update, if exactly one dominates.
      * The LWW winner, if updates are concurrent.
      * If two updates are equal on (clock, agent) and value, the
        first is returned (dedup).

    The merge is a pure function of the input set. It does not
    consult the database. Callers must persist the winners
    themselves (typically via ``apply_field_updates_to_db``).
    """
    # Group by (memory_id, field_name)
    by_field: dict[tuple[str, str], list[FieldUpdate]] = {}
    for u in updates:
        by_field.setdefault((u.memory_id, u.field_name), []).append(u)

    winners: list[FieldUpdate] = []
    for _key, group in by_field.items():
        if len(group) == 1:
            winners.append(group[0])
            continue

        # Fold left: pick the winner pairwise.
        current = group[0]
        for nxt in group[1:]:
            current = _merge_two(current, nxt)
        winners.append(current)
    return winners


def _merge_two(
    a: FieldUpdate,
    b: FieldUpdate,
) -> FieldUpdate:
    """Merge two updates to the same (memory_id, field_name).

    Returns the winning FieldUpdate. Implements the LWWES rule.
    """
    assert a.memory_id == b.memory_id
    assert a.field_name == b.field_name

    # If the values are identical, return either (they're equal).
    # This is a CRDT-correct "additive" merge.
    if a.value == b.value:
        # Pick the higher VV; if equal, higher clock; if equal,
        # lexicographically smaller agent. This keeps the merged
        # state deterministic.
        if _vv_dominates(a.version_vector, b.version_vector):
            return a
        if _vv_dominates(b.version_vector, a.version_vector):
            return b
        if a.logical_clock > b.logical_clock:
            return a
        if b.logical_clock > a.logical_clock:
            return b
        return a if a.last_writer_agent <= b.last_writer_agent else b

    # Different values. Apply LWW.
    # 1. Causal order: if a dominates b, take a.
    if _vv_dominates(a.version_vector, b.version_vector):
        return a
    if _vv_dominates(b.version_vector, a.version_vector):
        return b

    # 2. Concurrent: LWW tiebreaker.
    winner = _lww_tiebreak(
        a.logical_clock,
        a.last_writer_agent,
        b.logical_clock,
        b.last_writer_agent,
    )
    chosen = a if winner == "a" else b
    return chosen


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def ensure_field_crdt_schema(conn: sqlite3.Connection) -> None:
    """Create the memory_field_crdt table if it doesn't exist.

    Idempotent. Mirrors migration 013 so callers (tests, scripts)
    can bootstrap a fresh DB without running the full migration.

    Uses individual execute() calls instead of executescript() to avoid
    implicit transaction commit (SQLite commits on DDL in executescript).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_field_crdt (
            memory_id        TEXT    NOT NULL,
            field_name       TEXT    NOT NULL,
            value            TEXT    NOT NULL,
            version_vector   TEXT    NOT NULL,
            logical_clock    INTEGER NOT NULL,
            last_writer_agent TEXT   NOT NULL,
            is_deleted       INTEGER NOT NULL DEFAULT 0,
            updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (memory_id, field_name),
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_field_crdt_memory "
        "ON memory_field_crdt(memory_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_field_crdt_agent_updated "
        "ON memory_field_crdt(last_writer_agent, updated_at)"
    )


def apply_field_updates_to_db(
    conn: sqlite3.Connection,
    updates: Iterable[FieldUpdate],
) -> list[FieldUpdate]:
    """Apply field updates to the DB using the LWWES rule.

    For each (memory_id, field_name), the DB row is either
    INSERTed (new field) or replaced (existing field). The
    replacement only happens if the incoming update is causally
    after or LWW-wins over the existing row.

    Returns the list of updates that were ACTUALLY applied (i.e.,
    not rejected as stale). Useful for sync confirmations.
    """
    ensure_field_crdt_schema(conn)
    applied: list[FieldUpdate] = []
    for upd in updates:
        try:
            row = conn.execute(
                "SELECT value, version_vector, logical_clock, last_writer_agent, is_deleted "
                "FROM memory_field_crdt WHERE memory_id = ? AND field_name = ?",
                (upd.memory_id, upd.field_name),
            ).fetchone()
        except sqlite3.Error:
            continue
        if row is None:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_field_crdt "
                    "(memory_id, field_name, value, version_vector, logical_clock, "
                    " last_writer_agent, is_deleted) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (
                        upd.memory_id,
                        upd.field_name,
                        upd.value,
                        json.dumps(upd.version_vector),
                        upd.logical_clock,
                        upd.last_writer_agent,
                    ),
                )
            except sqlite3.Error:
                continue
            applied.append(upd)
            continue

        (
            existing_value,
            existing_vv_json,
            existing_clock,
            existing_agent,
            existing_deleted,
        ) = row
        existing_vv = json.loads(existing_vv_json) if existing_vv_json else {}

        if existing_deleted and not upd.value == "__TOMBSTONE__":
            if _vv_dominates(upd.version_vector, existing_vv):
                pass
            else:
                continue

        existing = FieldUpdate(
            memory_id=upd.memory_id,
            field_name=upd.field_name,
            value=existing_value,
            version_vector=existing_vv,
            logical_clock=existing_clock,
            last_writer_agent=existing_agent,
        )
        winner = _merge_two(existing, upd)
        if winner is upd:
            try:
                conn.execute(
                    "UPDATE memory_field_crdt SET value = ?, version_vector = ?, "
                    "logical_clock = ?, last_writer_agent = ?, is_deleted = 0, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE memory_id = ? AND field_name = ?",
                    (
                        upd.value,
                        json.dumps(upd.version_vector),
                        upd.logical_clock,
                        upd.last_writer_agent,
                        upd.memory_id,
                        upd.field_name,
                    ),
                )
            except sqlite3.Error:
                continue
            applied.append(upd)
    conn.commit()
    return applied


def read_fields(
    conn: sqlite3.Connection,
    memory_id: str,
) -> dict[str, str]:
    """Read all live (non-tombstoned) fields for a memory_id.

    Returns a dict mapping field_name -> value. Missing fields
    are simply absent from the result.
    """
    rows = conn.execute(
        "SELECT field_name, value FROM memory_field_crdt "
        "WHERE memory_id = ? AND is_deleted = 0",
        (memory_id,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def project_sql_to_crdt(
    conn: sqlite3.Connection,
    memory_id: str,
    agent_id: str,
) -> None:
    """Project the SQL winning values into ``memory_field_crdt``.

    Called after a local ``save_memory`` commits — reads the current
    ``memories`` row and writes corresponding ``memory_field_crdt``
    rows via ``apply_field_updates_to_db`` (which respects LWW merge
    semantics: if a concurrent CRDT merge wrote a higher-VV value,
    the SQL value won't overwrite it).
    """
    row = conn.execute(
        "SELECT content, tags, category, version_vector, logical_clock "
        "FROM memories WHERE id=?",
        (memory_id,),
    ).fetchone()
    if not row:
        return
    content, tags, category, vv_str, clock = row
    vv = json.loads(vv_str) if vv_str else {}
    clock = clock or 0
    ensure_field_crdt_schema(conn)
    field_val = {"content": content, "tags": tags, "category": category}
    updates = [
        FieldUpdate(
            memory_id=memory_id,
            field_name=fname,
            value=str(field_val.get(fname) or ""),
            version_vector=vv,
            logical_clock=clock,
            last_writer_agent=agent_id,
        )
        for fname in REPLICATED_FIELDS
    ]
    apply_field_updates_to_db(conn, updates)


def project_crdt_to_sql(
    conn: sqlite3.Connection,
    memory_id: str,
) -> set[str]:
    """Project winning CRDT field values back to the ``memories`` row.

    Called after a CRDT merge — reads live field values from
    ``memory_field_crdt`` and updates ``memories`` columns that are
    stale.  Only fields listed in ``REPLICATED_FIELDS`` are projected.

    Returns the set of field names that were updated (caller can use
    this to decide whether to enqueue background indexing tasks).
    """
    fields = read_fields(conn, memory_id)
    if not fields:
        return set()
    allowed = set(REPLICATED_FIELDS)
    updated: set[str] = set()
    for field_name in REPLICATED_FIELDS:
        if field_name not in allowed:
            continue
        val = fields.get(field_name)
        if val is None:
            continue
        cur = conn.execute(
            f"SELECT {field_name} FROM memories WHERE id=?", (memory_id,)
        )
        row = cur.fetchone()
        if row is None:
            continue
        if str(row[0] or "") != str(val or ""):
            conn.execute(
                f"UPDATE memories SET {field_name}=? WHERE id=?", (val, memory_id)
            )
            updated.add(field_name)
    if updated:
        conn.commit()
    return updated


def backfill_from_memories(conn: sqlite3.Connection) -> int:
    """One-shot: for every memory row, write a field-crdt row per
    replicated field, seeded with the memory's content/tags/
    category and the memory's existing note-level version vector.

    Returns the number of memory rows backfilled.

    Idempotent: existing field-crdt rows are NOT overwritten (so
    a partial backfill can be resumed).
    """
    ensure_field_crdt_schema(conn)
    rows = conn.execute(
        "SELECT id, content, tags, category, version_vector, logical_clock, "
        "        COALESCE(repo_id, '') "
        "FROM memories WHERE deleted_at IS NULL"
    ).fetchall()
    count = 0
    for (
        memory_id,
        content,
        tags,
        category,
        vv_json,
        clock,
        agent_id,
    ) in rows:
        existing = conn.execute(
            "SELECT 1 FROM memory_field_crdt WHERE memory_id = ? LIMIT 1",
            (memory_id,),
        ).fetchone()
        if existing is not None:
            continue  # already backfilled
        vv = json.loads(vv_json) if vv_json else {}
        # Use the note's existing clock/agent as the seed. If absent,
        # fall back to agent_id + clock=1.
        if not vv:
            vv = {agent_id or "local": 1}
            seed_clock = 1
        else:
            seed_clock = int(clock) if clock else 1
        seed_agent = agent_id or "local"

        for field_name, value in (
            ("content", content or ""),
            ("tags", tags or ""),
            ("category", category or ""),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO memory_field_crdt "
                "(memory_id, field_name, value, version_vector, logical_clock, "
                " last_writer_agent, is_deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (memory_id, field_name, value, json.dumps(vv), seed_clock, seed_agent),
            )
        count += 1
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# High-level save (the v13 replacement for crdt_merge.crdt_save)
# ---------------------------------------------------------------------------


def _bump_vv(vv: dict[str, int], agent: str) -> dict[str, int]:
    """Return a new VV with ``agent``'s clock incremented by 1.

    Pure function: does not mutate the input.
    """
    out = dict(vv)
    out[agent] = out.get(agent, 0) + 1
    return out


def crdt_field_save(
    db_path: str | Path | sqlite3.Connection,
    note_id: str,
    content: str,
    remote_agent_id: str,
    local_agent_id: str,
    source_file: str = "",
    category: str = "",
    remote_vv_str: str = "",
    remote_logical_clock: int = 0,
    conflict_policy: str | None = None,
    tags: str | None = None,
) -> dict:
    """Save a memory note with field-level LWWES CRDT semantics.

    This is the v13 replacement for ``crdt_merge.crdt_save``. It
    applies the field-level LWWES rule to each replicated field
    independently, so concurrent edits to different fields both
    win. The whole-note ``supersede``/``replace``/``coexist``
    policies are fallbacks that trigger when the local note does
    not yet have field-level state (back-compat path for notes
    saved before v13).

    Remediation #5 (2026-06-22): every successful merge also
    writes the merged content to the .md file on disk (the system
    treats markdown as the source of truth, so a stale .md after
    a CRDT merge is a silent drift).  Concurrent local edits are
    preserved as conflict files via safe_atomic_write.

    Args:
        db_path: Path to memory.db.
        note_id: Canonical note ID.
        content: Note content.
        remote_agent_id: Sending (remote) agent identifier.
        local_agent_id: Local (receiving) agent identifier.
        source_file: Optional source file path.
        category: Optional category for the note.
        remote_vv_str: Sender's version vector (JSON).
        remote_logical_clock: Sender's logical clock value.
        conflict_policy: Override policy (supersede/replace/coexist).
            If None, reads from the existing note's ``conflict_policy``
            column or defaults to ``supersede``.
        tags: JSON-encoded tags list (string). Optional; defaults to "[]".

    Returns:
        Dict with:
        - ``applied``: True if the write was accepted.
        - ``conflict``: True if a conflict was resolved at the note
          level (i.e., the fallback policy fired). False if the
          field-level LWWES resolved everything per field.
        - ``rejected``: True if the write was stale and discarded.
        - ``policy_used``: The conflict policy that was applied
          (note-level fallback only).
        - ``fields_applied``: list of field names that were updated.
        - ``archived_id``: (replace policy only) Note ID of archived
          version.
        - ``conflict_id``: (coexist policy only) Note ID of
          coexisting version.
    """
    from datetime import datetime, timezone
    from _lazy_imports import open_db
    from contextlib import nullcontext

    if hasattr(db_path, "execute"):
        # db_path is already a connection object
        conn = cast(sqlite3.Connection, db_path)
        conn_context = nullcontext(conn)
        db_path_obj: Path | None = None
    else:
        db_path_obj = Path(cast(str | Path, db_path))
        conn_context = open_db(db_path_obj, timeout=10.0)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tags = tags or "[]"

    result: dict[str, Any] = {
        "applied": False,
        "conflict": False,
        "rejected": False,
        "policy_used": None,
        "fields_applied": [],
        "archived_id": None,
        "conflict_id": None,
    }

    # P0-3 fix (2026-06-24): pre-state capture and saga-style rollback
    # for crdt_field_save.  Capture state before any mutations, then
    # wrap the entire write path in try/except so we can restore on
    # failure regardless of which return branch is taken.
    _pre_state: _CrdtPreState | None = None
    _write_conn: sqlite3.Connection | None = None

    with conn_context as conn:
        _write_conn = conn
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_field_crdt_schema(conn)
        _pre_state = _capture_crdt_pre_state(conn, note_id, db_path_obj)

        # Backfill on first read: if the note exists in memories but
        # not in memory_field_crdt, seed the field table from the
        # note-level columns. This is the back-compat path for notes
        # saved before v13.
        _seed_note_into_field_crdt_if_needed(conn, note_id, local_agent_id)

        try:
            # Build the incoming field updates.
            incoming_vv = _parse_incoming_vv(remote_vv_str, remote_agent_id)

            field_updates: list[FieldUpdate] = []
            for fname, fvalue in (
                ("content", content),
                ("tags", tags),
                ("category", category),
            ):
                if fname not in REPLICATED_FIELDS:
                    continue
                # If the caller didn't pass tags, fall back to current value.
                if fvalue is None:
                    existing = read_fields(conn, note_id)
                    fvalue = existing.get(fname, "")
                if fvalue is None:
                    fvalue = ""
                field_updates.append(
                    FieldUpdate(
                        memory_id=note_id,
                        field_name=fname,
                        value=str(fvalue),
                        version_vector=incoming_vv,
                        logical_clock=remote_logical_clock,
                        last_writer_agent=remote_agent_id,
                    )
                )

                # Check if the note-level row exists. If not, this is a new
                # note — accept all field updates unconditionally and write
                # the note-level row.
                row = conn.execute(
                "SELECT version_vector, logical_clock, conflict_policy "
                "FROM memories WHERE id=?",
                (note_id,),
            ).fetchone()

            if row is None:
                # New note. Apply field updates + create the note row.
                from save_pipeline import upsert_row

                upsert_row(
                    conn,
                    note_id,
                    content,
                    source_file=source_file or note_id,
                    tags=[],
                    category=category,
                    pinned=False,
                    tier="warm",
                )
                # Note-level VV tracks the union of all field VVs.
                new_vv = _bump_vv(incoming_vv, remote_agent_id)
                conn.execute(
                    "UPDATE memories SET version_vector=?, logical_clock=? WHERE id=?",
                    (json.dumps(new_vv), remote_logical_clock or 1, note_id),
                )
                applied = apply_field_updates_to_db(conn, field_updates)
                result["applied"] = True
                result["fields_applied"] = [u.field_name for u in applied]
                _finalize_crdt_save(db_path_obj, note_id, content, conn)
                return result

            # Existing note. Decide if we use field-level LWWES or a
            # whole-note fallback policy.

            # Existing note. Decide if we use field-level LWWES or a
            # whole-note fallback policy.
            existing_vv = parse_existing_vv(conn, note_id)
            existing_clock = parse_existing_clock(conn, note_id)

            # If the incoming VV is empty (no remote_vv_str provided),
            # the caller probably didn't know about v13. Fall back to
            # the note-level policies.
            if not remote_vv_str:
                logger.info(
                    "crdt_field: no remote_vv_str for %s; using note-level "
                    "fallback (may produce spurious conflicts)",
                    note_id,
                )
                return _fallback_to_note_level(
                    conn=conn,
                    note_id=note_id,
                    content=content,
                    source_file=source_file,
                    category=category,
                    remote_agent_id=remote_agent_id,
                    local_agent_id=local_agent_id,
                    remote_logical_clock=remote_logical_clock,
                    conflict_policy=conflict_policy,
                    now_iso=now_iso,
                    result=result,
                )

            # Causal order: incoming dominates existing, accept all
            # field updates.
            if _vv_dominates(incoming_vv, existing_vv):
                new_vv = _bump_vv(incoming_vv, remote_agent_id)
                new_clock = max(remote_logical_clock, existing_clock + 1)
                applied = apply_field_updates_to_db(conn, field_updates)
                _write_note_level_vv(conn, note_id, new_vv, new_clock)
                result["applied"] = True
                result["fields_applied"] = [u.field_name for u in applied]
                _finalize_crdt_save(db_path_obj, note_id, content, conn)
                return result

            # Existing dominates incoming: stale write.
            if _vv_dominates(existing_vv, incoming_vv):
                result["rejected"] = True
                logger.info(
                    "crdt_field: rejected stale write for %s (local=%s, remote=%s)",
                    note_id,
                    existing_vv,
                    incoming_vv,
                )
                return result

            # Concurrent: field-level LWWES. Each field is merged
            # independently. THIS is the bug fix — concurrent edits to
            # different fields both win.
            result["conflict"] = True
            applied = apply_field_updates_to_db(conn, field_updates)
            result["applied"] = bool(applied)
            result["fields_applied"] = [u.field_name for u in applied]
            if result["applied"]:
                _finalize_crdt_save(db_path_obj, note_id, content, conn)
            return result
        except Exception:
            if _write_conn is not None and _pre_state is not None:
                _restore_crdt_pre_state(_write_conn, note_id, _pre_state, db_path_obj)
            raise


def _parse_incoming_vv(remote_vv_str: str, remote_agent_id: str) -> dict[str, int]:
    """Parse the caller's remote_vv_str, or fall back to a sensible default."""
    if remote_vv_str:
        try:
            return cast(dict[str, int], json.loads(remote_vv_str))
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "crdt_field: malformed remote_vv_str %r; using default", remote_vv_str
            )
    # Default: a single-agent VV at clock 0. The caller didn't
    # provide one, so we treat this as the first write from
    # remote_agent_id.
    return {remote_agent_id: 0}


# ---------------------------------------------------------------------------
# P0-3 fix (2026-06-24): pre-state capture and restore for saga-style
# rollback of crdt_field_save.  Captures the memories row, field CRDT
# rows, and .md file before any mutations, then restores on exception.
# ---------------------------------------------------------------------------


@dataclass
class _CrdtPreState:
    """Snapshot of pre-existing state for crdt_field_save rollback."""

    memories_row: tuple | None
    field_rows: list[tuple]
    md_content: str | None
    md_path: Path | None


def _capture_crdt_pre_state(
    conn: sqlite3.Connection, note_id: str, db_path_obj: Path | None
) -> _CrdtPreState:
    memories_row = conn.execute(
        "SELECT content, tags, category, version_vector, logical_clock, "
        "source_file FROM memories WHERE id=?",
        (note_id,),
    ).fetchone()
    field_rows: list[tuple] = []
    try:
        field_rows = conn.execute(
            "SELECT field_name, value, version_vector, logical_clock, "
            "last_writer_agent, is_deleted FROM memory_field_crdt "
            "WHERE memory_id=?",
            (note_id,),
        ).fetchall()
    except Exception:
        pass
    md_content: str | None = None
    md_path: Path | None = None
    if db_path_obj is not None:
        try:
            row = conn.execute(
                "SELECT source_file FROM memories WHERE id=?", (note_id,)
            ).fetchone()
            if row and row[0]:
                src = row[0]
                if not src.endswith(".md"):
                    src += ".md"
                resolved_path = Path(db_path_obj).parent / src
                if resolved_path.exists():
                    md_content = resolved_path.read_text(encoding="utf-8")
                    md_path = resolved_path
        except Exception:
            pass
    return _CrdtPreState(
        memories_row=memories_row,
        field_rows=field_rows,
        md_content=md_content,
        md_path=md_path,
    )


def _restore_crdt_pre_state(
    conn: sqlite3.Connection,
    note_id: str,
    pre_state: _CrdtPreState,
    db_path_obj: Path | None,
) -> None:
    if pre_state.memories_row is not None:
        content, tags, category, vv_json, clock, source_file = pre_state.memories_row
        try:
            conn.execute(
                "UPDATE memories SET content=?, tags=?, category=?, "
                "version_vector=?, logical_clock=?, source_file=? WHERE id=?",
                (content, tags, category, vv_json, clock, source_file, note_id),
            )
        except Exception as exc:
            logger.warning(
                "crdt undo: restore memories for %s failed: %r", note_id, exc
            )
    else:
        try:
            conn.execute("DELETE FROM memories WHERE id=?", (note_id,))
        except Exception as exc:
            logger.warning("crdt undo: delete memories for %s failed: %r", note_id, exc)
    try:
        conn.execute("DELETE FROM memory_field_crdt WHERE memory_id=?", (note_id,))
    except Exception as exc:
        logger.warning("crdt undo: delete fields for %s failed: %r", note_id, exc)
    for frow in pre_state.field_rows:
        fname, fvalue, fvv, fclock, flwa, fdel = frow
        try:
            conn.execute(
                "INSERT INTO memory_field_crdt "
                "(memory_id, field_name, value, version_vector, logical_clock, "
                " last_writer_agent, is_deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (note_id, fname, fvalue, fvv, fclock, flwa, fdel),
            )
        except Exception as exc:
            logger.warning(
                "crdt undo: restore field %s for %s failed: %r", fname, note_id, exc
            )
    if pre_state.md_content is not None and pre_state.md_path is not None:
        try:
            pre_state.md_path.write_text(pre_state.md_content, encoding="utf-8")
        except Exception as exc:
            logger.warning("crdt undo: restore .md for %s failed: %r", note_id, exc)
    elif pre_state.md_path is not None and pre_state.md_path.exists():
        try:
            pre_state.md_path.unlink()
        except Exception as exc:
            logger.warning("crdt undo: unlink .md for %s failed: %r", note_id, exc)


def _finalize_crdt_save(
    db_path: str | Path | None,
    note_id: str,
    content: str,
    conn: sqlite3.Connection,
) -> None:
    """Write the merged content to disk after a successful CRDT merge.

    Remediation #5 (2026-06-22): without this, the .md file
    remains the pre-merge content even though the DB has the
    merged state, so the markdown-vs-DB consistency check
    (``memory_integrity.find_orphan_files``) cannot detect the
    drift.  safe_atomic_write gives us concurrent-edit detection
    (Scenario 4 fix): if a local edit happened during the CRDT
    merge, the local edit is preserved as
    ``<path>.conflict-<pid>-<ts>``.

    Best-effort: if the write fails (e.g. disk full, permission
    error), we log and return — the DB write is the source of
    truth and the .md can be regenerated later via
    ``recover_orphan_files`` or the next save.

    If ``db_path`` is None, the .md write is skipped (the caller
    didn't have a path, e.g. when a connection was passed directly).
    """
    from pathlib import Path as _Path

    if db_path is None:
        logger.debug(
            "crdt_field: no db_path provided; skipping .md write for %s",
            note_id,
        )
        return
    try:
        row = conn.execute(
            "SELECT source_file FROM memories WHERE id=?",
            (note_id,),
        ).fetchone()
        if not row or not row[0]:
            logger.debug(
                "crdt_field: note %s has no source_file; skipping .md write",
                note_id,
            )
            return
        source_file = row[0]
        # db_path is memory/memory.db; memory_root is the parent
        # of memory.db (where the .md files live).
        memory_root = _Path(db_path).parent
        # Convention: source_file is "<category>/<slug>.md" (set
        # by save_pipeline).  When the caller didn't supply one
        # (e.g. crdt_field_save default), the row has
        # "<category>/<slug>" without the .md extension — append
        # it here so the path matches what save_pipeline would
        # have written.
        if not source_file.endswith(".md"):
            source_file = source_file + ".md"
        md_path = memory_root / source_file
        # Build the full markdown body: frontmatter + content.
        # We re-use _build_memory_file from save_pipeline to keep
        # the frontmatter format consistent with save_memory.
        body: str | None = None
        try:
            from save_pipeline import _build_memory_file

            # _build_memory_file signature: (content, category,
            # title_slug, tags_list, pinned, now_iso=None)
            # We can recover the slug from the note_id.
            # note_id format: "<category>/<slug>".
            category_str = note_id.split("/", 1)[0] if "/" in note_id else "imported"
            slug = note_id.split("/", 1)[-1]
            markdown, _fm, _now, _md = _build_memory_file(
                content,
                category_str,
                slug,
                tags_list=[],
                pinned=False,
            )
            body = markdown
        except Exception as build_exc:
            logger.debug(
                "crdt_field: _build_memory_file failed (%s); "
                "falling back to raw content",
                build_exc,
            )
            body = content
        # safe_atomic_write (Scenario 4 fix): if a local edit
        # happened during the CRDT merge, the local edit is
        # preserved as <path>.conflict-<pid>-<ts>.
        from memory_common import safe_atomic_write

        try:
            safe_atomic_write(md_path, body, encoding="utf-8")
            logger.info("crdt_field: wrote merged content to %s", md_path)
        except Exception as write_exc:
            # Best-effort: the DB has the merged state, the .md
            # can be regenerated later.
            logger.warning(
                "crdt_field: failed to write merged .md %s: %s. "
                "Run --recover-orphan-files to regenerate.",
                md_path,
                write_exc,
            )
    except Exception as outer_exc:
        logger.warning(
            "crdt_field: _finalize_crdt_save failed for %s: %s",
            note_id,
            outer_exc,
        )


def _seed_note_into_field_crdt_if_needed(
    conn: sqlite3.Connection,
    note_id: str,
    local_agent_id: str,
) -> None:
    """If the note exists in ``memories`` but not in
    ``memory_field_crdt``, backfill the field rows from the note's
    current content/tags/category.

    This is the back-compat path for notes saved before v13. It
    runs on every save (cheap — the SELECT is indexed on
    memory_id) so the system converges to field-level state as
    notes are touched.
    """
    has_field = conn.execute(
        "SELECT 1 FROM memory_field_crdt WHERE memory_id = ? LIMIT 1",
        (note_id,),
    ).fetchone()
    if has_field is not None:
        return

    row = conn.execute(
        "SELECT content, tags, category, version_vector, logical_clock "
        "FROM memories WHERE id = ?",
        (note_id,),
    ).fetchone()
    if row is None:
        return  # note doesn't exist; crdt_field_save will create it
    content, tags, category, vv_json, clock = row
    vv = json.loads(vv_json) if vv_json else {local_agent_id: clock or 1}
    seed_clock = int(clock) if clock else 1
    for fname, fvalue in (
        ("content", content or ""),
        ("tags", tags or ""),
        ("category", category or ""),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO memory_field_crdt "
            "(memory_id, field_name, value, version_vector, logical_clock, "
            " last_writer_agent, is_deleted) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (note_id, fname, fvalue, json.dumps(vv), seed_clock, local_agent_id),
        )


def parse_existing_vv(conn: sqlite3.Connection, note_id: str) -> dict[str, int]:
    """Read the note's existing version vector."""
    row = conn.execute(
        "SELECT version_vector FROM memories WHERE id=?",
        (note_id,),
    ).fetchone()
    if row is None or not row[0]:
        return {}
    try:
        v = json.loads(row[0])
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def parse_existing_clock(conn: sqlite3.Connection, note_id: str) -> int:
    """Read the note's existing logical clock."""
    row = conn.execute(
        "SELECT logical_clock FROM memories WHERE id=?",
        (note_id,),
    ).fetchone()
    return int(row[0]) if row and row[0] else 0


def _write_note_level_vv(
    conn: sqlite3.Connection,
    note_id: str,
    vv: dict[str, int],
    clock: int,
) -> None:
    """Write the note-level version vector and clock.

    The note-level VV tracks the union of all field VVs. The clock
    is the max clock across all fields. This is a denormalization
    for backward-compat with the v12 note-level LWW code path.
    """
    conn.execute(
        "UPDATE memories SET version_vector=?, logical_clock=? WHERE id=?",
        (json.dumps(vv), clock, note_id),
    )


def _fallback_to_note_level(
    conn: sqlite3.Connection,
    note_id: str,
    content: str,
    source_file: str,
    category: str,
    remote_agent_id: str,
    local_agent_id: str,
    remote_logical_clock: int,
    conflict_policy: str | None,
    now_iso: str,
    result: dict,
) -> dict:
    """Fall back to the pre-v13 note-level LWW policies.

    Triggered when the caller didn't provide a remote_vv_str (so
    the field-level CRDT can't make a deterministic decision).
    Behavior matches the legacy ``crdt_merge.crdt_save`` for
    backward compat.
    """
    from save_pipeline import upsert_row

    if conflict_policy is None:
        row = conn.execute(
            "SELECT conflict_policy FROM memories WHERE id=?",
            (note_id,),
        ).fetchone()
        policy_used = row[0] if row else "supersede"
    else:
        policy_used = conflict_policy

    if policy_used == "supersede":
        upsert_row(
            conn,
            note_id,
            content,
            source_file=source_file or note_id,
            tags=[],
            category=category,
            pinned=False,
            tier="warm",
        )
        new_vv = _bump_vv(parse_existing_vv(conn, note_id), remote_agent_id)
        conn.execute(
            "UPDATE memories SET version_vector=?, logical_clock=? WHERE id=?",
            (json.dumps(new_vv), remote_logical_clock or 1, note_id),
        )
        result["applied"] = True
        result["conflict"] = True
        result["policy_used"] = "supersede"
    elif policy_used == "coexist":
        result["conflict"] = True
        result["policy_used"] = "coexist"
        result["conflict_id"] = f"{note_id}__conflict_{remote_agent_id}"
    elif policy_used == "replace":
        result["conflict"] = True
        result["policy_used"] = "replace"
        result["archived_id"] = f"{note_id}__archived_{remote_agent_id}"
    return result
