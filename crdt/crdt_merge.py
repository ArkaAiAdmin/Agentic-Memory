#!/usr/bin/env python3
"""Note-level LWW merge engine with field-level CRDT delegation.

This module is now (v13, 2026-06-20) a thin wrapper around
``crdt_field.crdt_field_save``. The legacy note-level LWW path
here is retained for two reasons:

1. **Backward compat with pre-v13 peers.** When the
   ``memory_field_crdt`` table does not exist, or when a sync
   response does not include ``field_crdt``, the note-level
   LWW is the only path that works.
2. **Note-level conflict policies.** The ``supersede``,
   ``replace``, and ``coexist`` policies are whole-note operations
   that the field-level LWWES does not implement. They are
   fallbacks that callers can opt into by passing
   ``conflict_policy=...``.

The legacy implementation here is **last-writer-wins with
version-vector happened-before detection** (LWW+VV). It is
correct for its design intent (causality-preserving LWW at the
note level) but is **not a true CRDT** — two agents editing
different fields of the same note would see one side's entire
note win.

For the true CRDT semantics (per-field merge where concurrent
edits to different fields both win), see ``crdt_field.py``. That
module implements LWW-Element-Set per field and is the v13
default.

Design of the legacy path: O(1) comparison for the common case
(no conflict), O(n) for conflict detection (n = number of agents
writing to this note, typically 1-3).
"""

from __future__ import annotations

import json
import logging
from typing import Optional
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vector clock utilities
# ---------------------------------------------------------------------------


def parse_version_vector(raw: Optional[str]) -> dict[str, int]:
    """Parse a version_vector JSON string into a dict.

    Returns an empty dict if the value is None, empty, or unparseable.
    """
    if not raw:
        return {}
    try:
        vv = json.loads(raw)
        if isinstance(vv, dict):
            return {k: int(v) for k, v in vv.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def dominates(v1: dict[str, int], v2: dict[str, int]) -> bool:
    """Return True if v1 dominates v2 (all v1 counters ≥ all v2 counters).

    Two empty version vectors are never concurrent — neither dominates.
    """
    all_keys = set(v1) | set(v2)
    if not all_keys:
        return False
    for k in all_keys:
        if v1.get(k, 0) < v2.get(k, 0):
            return False
    return True


def concurrent(v1: dict[str, int], v2: dict[str, int]) -> bool:
    """Return True if v1 and v2 are concurrent (neither dominates)."""
    return not dominates(v1, v2) and not dominates(v2, v1)


def merge_vectors(
    agent_id: str, local: dict[str, int], remote: dict[str, int]
) -> dict[str, int]:
    """Merge two version vectors by taking the max per entry.

    Pure pointwise-max — idempotent and commutative.
    The caller is responsible for bumping the local clock BEFORE or AFTER
    calling merge_vectors, so that merge_vectors(x, x) == x.
    """
    merged = {}
    all_keys = set(local) | set(remote)
    for k in all_keys:
        merged[k] = max(local.get(k, 0), remote.get(k, 0))
    return merged


# ---------------------------------------------------------------------------
# CRDT-aware save and sync
# ---------------------------------------------------------------------------


def _capture_pre_state_main(conn: AnyConnection, note_id: str) -> Optional[dict]:
    """Snapshot a memory row for saga undo. Returns None if the row
    doesn't exist; otherwise returns a dict with the columns the undo
    closure will need to restore.
    """
    try:
        row = conn.execute(
            """SELECT content, source_file, version_vector, logical_clock
               FROM memories WHERE id=?""",
            (note_id,),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return {
        "content": row[0],
        "source_file": row[1],
        "version_vector": row[2],
        "logical_clock": row[3],
    }


def _write_merged_markdown(
    db_path: "str | Path",
    note_id: str,
    content: str,
    conn: AnyConnection,
) -> None:
    """Write the merged CRDT content to the note's markdown file.

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
    """
    import logging as _logging
    from pathlib import Path

    logger = _logging.getLogger(__name__)
    try:
        # Look up the source_file path (set by save_memory and
        # stored relative to memory_root).
        row = conn.execute(
            "SELECT source_file FROM memories WHERE id=?",
            (note_id,),
        ).fetchone()
        if not row or not row[0]:
            logger.debug(
                "crdt: note %s has no source_file; skipping .md write",
                note_id,
            )
            return
        source_file = row[0]
        # db_path is memory/memory.db; memory_root is the parent
        # of memory.db (where the .md files live).
        memory_root = Path(db_path).parent
        # Convention: source_file is "<category>/<slug>.md" (set
        # by save_pipeline).  When the caller didn't supply one
        # (e.g. crdt_save default), the row has "<category>/<slug>"
        # without the .md extension — append it here so the path
        # matches what save_pipeline would have written.
        if not source_file.endswith(".md"):
            source_file = source_file + ".md"
        md_path = memory_root / source_file
        # Build the full markdown body: frontmatter + content.
        # We re-use _build_memory_file from save_pipeline to keep
        # the frontmatter format consistent with save_memory.
        try:
            from save_pipeline import _build_memory_file
        except Exception:
            _build_memory_file = None  # type: ignore[assignment]
        if _build_memory_file is not None:
            try:
                # _build_memory_file signature: (content, category,
                # title_slug, tags_list, pinned, now_iso=None)
                # We can recover the slug from the note_id.
                # note_id format: "<category>/<slug>".
                category_str = (
                    note_id.split("/", 1)[0] if "/" in note_id else "imported"
                )
                slug = note_id.split("/", 1)[-1]
                markdown, _fm, now_iso, _md = _build_memory_file(
                    content,
                    category_str,
                    slug,
                    tags_list=[],
                    pinned=False,
                )
                body = markdown
            except Exception as build_exc:
                logger.debug(
                    "crdt: _build_memory_file failed (%s); falling back to raw content",
                    build_exc,
                )
                body = content
        else:
            body = content
        # safe_atomic_write (Scenario 4 fix): if a local edit
        # happened during the CRDT merge, the local edit is
        # preserved as <path>.conflict-<pid>-<ts>.
        from infra.memory_common import safe_atomic_write

        try:
            safe_atomic_write(md_path, body, encoding="utf-8")
            logger.info("crdt: wrote merged content to %s", md_path)
        except Exception as write_exc:
            # Best-effort: the DB has the merged state, the .md
            # can be regenerated later.
            logger.warning(
                "crdt: failed to write merged .md %s: %s. "
                "Run --recover-orphan-files to regenerate.",
                md_path,
                write_exc,
            )
    except Exception as outer_exc:
        logger.warning(
            "crdt: _write_merged_markdown failed for %s: %s",
            note_id,
            outer_exc,
        )


def crdt_save(
    db_path: str | Path,
    note_id: str,
    content: str,
    remote_agent_id: str,
    local_agent_id: str,
    source_file: str = "",
    category: str = "",
    remote_vv_str: str = "",
    remote_logical_clock: int = 0,
    conflict_policy: Optional[str] = None,
) -> dict:
    """Save a memory note with CRDT conflict resolution.

    If the note already exists, the version vectors are compared:
    - Local dominates remote: write proceeds (optimistic update).
    - Remote dominates local: write is rejected (stale data).
    - Concurrent: depends on ``conflict_policy``:
        - ``supersede`` (default): LWW via highest (logical_clock, agent_id).
          **Note:** the default ``supersede``/LWW policy loses one side of a
          same-field concurrent edit. To preserve both versions (keeping
          a ``__conflict_<remote_agent_id>`` copy of the loser), use the
          ``coexist`` policy.
        - ``replace``: Winning version replaces the note; old content is
          archived with ``valid_to`` and the new note's ``supersedes``
          column links to the archived version.
        - ``coexist``: Both versions kept as separate notes. Remote
          content gets note_id with ``__conflict_<remote_agent_id>`` suffix.

    H19 fix: the function now takes ``remote_vv_str`` and
    ``remote_logical_clock`` as explicit parameters so the conflict
    resolution is computed against the actual remote state, not a
    re-parse of the local row (which always biases toward "local
    dominates" and silently swallows conflicts).

    S4 note (resolved 2026-06-18): this function is now saga-wrapped.
    The pre-state is captured before the BEGIN IMMEDIATE block, and
    the Saga's undo closure restores the row(s) to that pre-state if
    any step raises. The local transaction is still atomic via
    BEGIN IMMEDIATE — the saga is a defense-in-depth layer that
    catches the rare case of a mid-execution raise (e.g. disk full
    between the archive INSERT and the main UPDATE in the ``replace``
    policy). Network roundtrips with the remote agent remain
    idempotent per the LWW tiebreaker; saga doesn't address that
    boundary.

    B4 fix: ``local_agent_id`` and ``remote_agent_id`` are now
    separate parameters so the LWW tiebreaker compares distinct
    agent IDs for deterministic cross-agent ordering.

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

    Returns:
        Dict with:
        - ``applied``: True if the write was accepted.
        - ``conflict``: True if a conflict was resolved (LWW used).
        - ``rejected``: True if the write was stale and discarded.
        - ``policy_used``: The conflict policy that was applied.
        - ``archived_id``: (replace only) Note ID of the archived version.
        - ``conflict_id``: (coexist only) Note ID of the coexisting version.
    """
    from datetime import datetime, timezone
    from infra._lazy_imports import open_db

    db_path = Path(db_path)

    # P0-1 fix (2026-07-03): scan remote content for prompt injection
    # before any DB mutation. This closes the CRDT injection bypass where
    # pull_from_peer feeds unvalidated remote content directly into
    # crdt_save, bypassing the 8-layer injection-defense in save_memory.
    try:
        from save_pipeline import _scan_for_injection_or_skip, SaveValidationError

        _scan_for_injection_or_skip(content, category or "", note_id)
    except SaveValidationError as e:
        logger.warning(
            "crdt_save: rejected injection-suspicious content from %s for %s: %s",
            remote_agent_id,
            note_id,
            e,
        )
        return {
            "applied": False,
            "rejected": True,
            "conflict": False,
            "policy_used": None,
            "archived_id": None,
            "conflict_id": None,
        }
    except Exception as _inject_exc:
        logger.debug("crdt_save: injection scan failed (benign): %s", _inject_exc)

    # 2026-06-20 (v13): if the memory_field_crdt table exists, use
    # the field-level LWWES path. This is a CRDT-correct merge:
    # concurrent edits to different fields both win. The legacy
    # note-level LWW below is the fallback for pre-v13 callers and
    # for note-level conflict policies (supersede/replace/coexist)
    # that the field-level path does not implement.
    #
    # 2026-06-22 (D6 fix): the previous ``SCHEMA_VERSION_HAVE_FIELD_CRDT
    # = 13`` constant in this block was dead — the sqlite_master probe
    # is what actually decides which path to take, and the constant
    # was never referenced after definition.  Removed; if a future
    # caller needs to query the migration version directly, they
    # should import ``migration_runner.SCHEMA_VERSION`` rather than
    # hardcoding the field-CRDT introduction version.
    # Check if field-level CRDT table exists, but close the probe
    # before delegating to crdt_field_save (which also uses open_db).
    # Holding the probe's open_db context while calling crdt_field_save
    # would deadlock the write queue (both need a session).
    _has_table = False
    try:
        with open_db(db_path, timeout=10.0) as _probe:
            _probe.execute("PRAGMA foreign_keys=ON")
            _has_table = (
                _probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='memory_field_crdt' LIMIT 1"
                ).fetchone()
                is not None
            )
    except Exception:
        _has_table = False

    if _has_table:
        # Field-level path is available. Delegate.
        from crdt.crdt_field import crdt_field_save, project_crdt_to_sql

        try:
            _result = crdt_field_save(
                db_path=db_path,
                note_id=note_id,
                content=content,
                remote_agent_id=remote_agent_id,
                local_agent_id=local_agent_id,
                source_file=source_file,
                category=category,
                remote_vv_str=remote_vv_str,
                remote_logical_clock=remote_logical_clock,
                conflict_policy=conflict_policy,
            )

            if _result.get("applied"):
                try:
                    from infra._lazy_imports import open_db

                    with open_db(db_path, timeout=10.0) as _proj_conn:
                        _updated = project_crdt_to_sql(_proj_conn, note_id)
                        if _updated:
                            from background.background_queue import (
                                init_task_queue,
                                enqueue_task,
                            )

                            init_task_queue(_proj_conn)
                            enqueue_task(
                                _proj_conn,
                                "embedding_index",
                                {"note_id": note_id},
                            )
                            enqueue_task(
                                _proj_conn,
                                "kg_and_fact_index",
                                {"note_id": note_id},
                            )
                            enqueue_task(
                                _proj_conn,
                                "semantic_backlinks",
                                {"note_id": note_id},
                            )
                except Exception as _pe:
                    logger.warning(
                        "crdt_save: post-merge projection failed for %s: %s",
                        note_id, _pe,
                    )

            # Map fields to the legacy shape.
            return {
                "applied": _result["applied"],
                "conflict": _result["conflict"],
                "rejected": _result["rejected"],
                "policy_used": _result["policy_used"],
                "archived_id": _result["archived_id"],
                "conflict_id": _result["conflict_id"],
            }
        except Exception as _e:
            logger.warning(
                "crdt_save: field-level path failed for %s; falling back to note-level: %s",
                note_id,
                _e,
            )

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with open_db(db_path, timeout=10.0) as conn:
        conn.execute("PRAGMA foreign_keys=ON")

        # S4 fix (2026-06-18): capture pre-state for the saga's undo.
        # The saga runs the BEGIN IMMEDIATE work; if any step raises
        # (e.g. disk full between the replace-policy archive INSERT
        # and the main UPDATE), the undo restores the row(s).
        pre_existing_main = _capture_pre_state_main(conn, note_id)
        pre_existing_conflict = _capture_pre_state_main(
            conn, f"{note_id}__conflict_{remote_agent_id}"
        )

        from infra.saga import Saga, SagaStep, SagaError

        def _do_resolve() -> dict:
            conn.execute("BEGIN IMMEDIATE")

            # Read existing version vector
            row = conn.execute(
                "SELECT version_vector, logical_clock FROM memories WHERE id=?",
                (note_id,),
            ).fetchone()

            applied = False
            conflict = False
            rejected = False
            policy_used = None
            archived_id = None
            conflict_id = None

            if row is None:
                # New note — accept unconditionally.
                # P0-5 fix: route the row write through
                # save_pipeline.upsert_row instead of a raw
                # INSERT OR REPLACE.  Keeps fitness_score,
                # importance, repo_id, valid_from, metadata,
                # and file_mtimes consistent with the canonical
                # save_memory path.  The version_vector +
                # logical_clock are set explicitly below so the
                # CRDT bookkeeping is preserved.
                our_vv = json.dumps({remote_agent_id: 1})
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
                conn.execute(
                    "UPDATE memories SET version_vector=?, logical_clock=? WHERE id=?",
                    (our_vv, 1, note_id),
                )
                applied = True

            else:
                existing_vv_str = row[0]
                existing_clock = row[1]
                existing_vv = parse_version_vector(existing_vv_str)
                if remote_vv_str:
                    incoming_vv = parse_version_vector(remote_vv_str)
                else:
                    logger.warning(
                        "crdt: no remote_vv_str provided for %s; falling back to "
                        "local+1 (may produce spurious conflicts in multi-agent sync)",
                        note_id,
                    )
                    incoming_vv = dict(existing_vv)
                    incoming_vv[remote_agent_id] = (
                        incoming_vv.get(remote_agent_id, 0) + 1
                    )

                if dominates(incoming_vv, existing_vv):
                    new_vv = merge_vectors(remote_agent_id, existing_vv, incoming_vv)
                    new_clock = new_vv.get(remote_agent_id, existing_clock + 1)
                    # P0-5 fix: route the row write through
                    # save_pipeline.upsert_row instead of a raw
                    # UPDATE.  The existing row's content is
                    # overwritten via INSERT ... ON CONFLICT
                    # inside the private helper, then we set the
                    # CRDT version_vector + logical_clock
                    # explicitly.
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
                    conn.execute(
                        "UPDATE memories SET version_vector=?, logical_clock=? WHERE id=?",
                        (json.dumps(new_vv), new_clock, note_id),
                    )
                    applied = True

                elif dominates(existing_vv, incoming_vv):
                    rejected = True
                    logger.info(
                        "crdt: rejected stale write for %s (local=%s, remote=%s)",
                        note_id,
                        existing_vv,
                        incoming_vv,
                    )

                else:
                    conflict = True
                    if conflict_policy is None:
                        _cp_row = conn.execute(
                            "SELECT conflict_policy FROM memories WHERE id=?",
                            (note_id,),
                        ).fetchone()
                        policy_used = _cp_row[0] if _cp_row else "supersede"
                    else:
                        policy_used = conflict_policy

                    our_tie = (existing_clock, local_agent_id)
                    their_tie = (
                        remote_logical_clock or existing_clock + 1,
                        remote_agent_id,
                    )
                    remote_wins = their_tie > our_tie

                    if policy_used == "coexist":
                        conflict_id = f"{note_id}__conflict_{remote_agent_id}"
                        _existing = conn.execute(
                            "SELECT id FROM memories WHERE id=?", (conflict_id,)
                        ).fetchone()
                        if _existing is None:
                            _rv = json.dumps(incoming_vv)
                            conn.execute(
                                """INSERT INTO memories
                                   (id, content, source_file, tags, created_at, updated_at, observed_at,
                                    fitness_score, importance, pinned, version_vector, logical_clock,
                                    conflict_policy, supersedes)
                                   VALUES (?, ?, ?, '[]', ?, ?, ?, 0.5, 3, 0, ?, ?, ?, ?)""",
                                (
                                    conflict_id,
                                    content,
                                    source_file or note_id,
                                    now_iso,
                                    now_iso,
                                    now_iso,
                                    _rv,
                                    remote_logical_clock or 1,
                                    policy_used,
                                    note_id,
                                ),
                            )
                            applied = True
                        else:
                            rejected = True

                    elif policy_used == "replace" and remote_wins:
                        archived_id = f"{note_id}__v_{existing_clock}"
                        _existing_archived = conn.execute(
                            "SELECT id FROM memories WHERE id=?", (archived_id,)
                        ).fetchone()
                        if _existing_archived is None:
                            _old_content = conn.execute(
                                "SELECT content, source_file FROM memories WHERE id=?",
                                (note_id,),
                            ).fetchone()
                            if _old_content:
                                conn.execute(
                                    """INSERT INTO memories
                                       (id, content, source_file, tags, created_at, updated_at,
                                        observed_at, fitness_score, importance, pinned,
                                        version_vector, logical_clock, valid_to, conflict_policy)
                                       VALUES (?, ?, ?, '[]', ?, ?, ?, 0.5, 3, 0, ?, ?, ?, ?)""",
                                    (
                                        archived_id,
                                        _old_content[0],
                                        _old_content[1],
                                        now_iso,
                                        now_iso,
                                        now_iso,
                                        existing_vv_str or "{}",
                                        existing_clock,
                                        now_iso,
                                        policy_used,
                                    ),
                                )

                        new_vv = merge_vectors(
                            remote_agent_id, existing_vv, incoming_vv
                        )
                        new_clock = new_vv.get(remote_agent_id, their_tie[0])
                        conn.execute(
                            """UPDATE memories SET content=?, updated_at=?, version_vector=?,
                               logical_clock=?, supersedes=?, valid_to=NULL WHERE id=?""",
                            (
                                content,
                                now_iso,
                                json.dumps(new_vv),
                                new_clock,
                                archived_id,
                                note_id,
                            ),
                        )
                        applied = True

                    elif policy_used == "replace":
                        rejected = True

                    else:
                        if remote_wins:
                            new_vv = merge_vectors(
                                remote_agent_id, existing_vv, incoming_vv
                            )
                            new_clock = new_vv.get(remote_agent_id, their_tie[0])
                            conn.execute(
                                """UPDATE memories SET content=?, updated_at=?, version_vector=?,
                                   logical_clock=? WHERE id=?""",
                                (
                                    content,
                                    now_iso,
                                    json.dumps(new_vv),
                                    new_clock,
                                    note_id,
                                ),
                            )
                            applied = True
                        else:
                            rejected = True

            conn.commit()
            # Remediation #5 (2026-06-22): write the merged content to
            # disk so the markdown files don't drift from the DB.
            # Without this, the .md file remains the pre-merge
            # content even though the DB has the merged state, and
            # the markdown-vs-DB check (memory_integrity.find_orphan_files)
            # would not detect it (no missing file, just stale content).
            # safe_atomic_write gives us concurrent-edit detection
            # (Scenario 4 fix): if a local edit happened during the
            # CRDT merge, the local edit is preserved as
            # ``<path>.conflict-<pid>-<ts>``.
            if applied and not rejected:
                _write_merged_markdown(
                    db_path=db_path,
                    note_id=note_id,
                    content=content,
                    conn=conn,
                )
                if conflict_id:
                    # Coexist branch: also write the conflict .md.
                    _row = conn.execute(
                        "SELECT content FROM memories WHERE id=?",
                        (conflict_id,),
                    ).fetchone()
                    if _row:
                        _write_merged_markdown(
                            db_path=db_path,
                            note_id=conflict_id,
                            content=_row[0],
                            conn=conn,
                        )
            return {
                "applied": applied,
                "conflict": conflict,
                "rejected": rejected,
                "policy_used": policy_used,
                "archived_id": archived_id,
                "conflict_id": conflict_id,
            }

        def _undo_resolve() -> None:
            # Restore the main note to its pre-state. If the row didn't
            # exist before, delete whatever was inserted. If it did,
            # restore the captured (content, source_file, version_vector,
            # logical_clock).
            if pre_existing_main is None:
                try:
                    conn.execute("DELETE FROM memories WHERE id=?", (note_id,))
                    conn.commit()
                except Exception as undo_exc:
                    logger.error("crdt saga undo: delete main row failed: %r", undo_exc)
            else:
                try:
                    conn.execute(
                        """UPDATE memories SET content=?, source_file=?, version_vector=?,
                           logical_clock=? WHERE id=?""",
                        (
                            pre_existing_main["content"],
                            pre_existing_main["source_file"],
                            pre_existing_main["version_vector"],
                            pre_existing_main["logical_clock"],
                            note_id,
                        ),
                    )
                    conn.commit()
                except Exception as undo_exc:
                    logger.error(
                        "crdt saga undo: restore main row failed: %r", undo_exc
                    )

            # Coexist branch may have inserted a conflict row. Remove
            # it if it didn't exist before, restore if it did.
            if pre_existing_conflict is None:
                try:
                    conn.execute(
                        "DELETE FROM memories WHERE id=?",
                        (f"{note_id}__conflict_{remote_agent_id}",),
                    )
                    conn.commit()
                except Exception as undo_exc:
                    logger.error(
                        "crdt saga undo: delete conflict row failed: %r", undo_exc
                    )
            else:
                try:
                    conn.execute(
                        """UPDATE memories SET content=?, source_file=?, version_vector=?,
                           logical_clock=? WHERE id=?""",
                        (
                            pre_existing_conflict["content"],
                            pre_existing_conflict["source_file"],
                            pre_existing_conflict["version_vector"],
                            pre_existing_conflict["logical_clock"],
                            f"{note_id}__conflict_{remote_agent_id}",
                        ),
                    )
                    conn.commit()
                except Exception as undo_exc:
                    logger.error(
                        "crdt saga undo: restore conflict row failed: %r", undo_exc
                    )

            # Replace branch may have inserted an archive row.
            # Archives are always fresh (they have a clock-stamped id)
            # so we always delete on undo, never restore.
            try:
                conn.execute(
                    "DELETE FROM memories WHERE id LIKE ? AND id != ?",
                    (f"{note_id}__v_%", note_id),
                )
                conn.commit()
            except Exception as undo_exc:
                logger.error("crdt saga undo: delete archive rows failed: %r", undo_exc)

        result: dict = {}
        try:
            with Saga(
                name="crdt_save",
                steps=[SagaStep("resolve", _do_resolve, _undo_resolve)],
            ) as saga:
                result = saga.results[0]
        except SagaError:
            # The undo has already run. Re-raise to surface the original
            # error to the caller. The crdt_sync_all caller counts this
            # as a rejected write.
            raise
        except Exception as exc:
            # Saga wraps SagaError around step failures. If something
            # outside the Saga raised (e.g. open_db), re-raise so the
            # caller sees the real error.
            raise RuntimeError(f"crdt_save failed for {note_id}: {exc!r}") from exc
        return result


def crdt_sync_all(
    db_path: str | Path,
    remote_agent_id: str,
    local_agent_id: str,
    remote_notes: dict[str, tuple[str, str, int, str, int]],
) -> dict:
    """Bulk-sync multiple notes from a remote agent.

    H19 fix: the tuple now includes the remote version vector and
    logical clock so the per-note ``crdt_save`` can do real conflict
    resolution instead of always treating the local row as authoritative.

    ``remote_notes``: {note_id: (content, source_file, logical_clock, version_vector, remote_clock)}

    Returns summary dict with counts of applied, conflicted, and rejected.
    """
    results = {"applied": 0, "conflict": 0, "rejected": 0, "total": len(remote_notes)}
    for note_id, (
        content,
        source_file,
        remote_clock,
        remote_vv,
        _sender_clock,
    ) in remote_notes.items():
        r = crdt_save(
            db_path,
            note_id,
            content,
            remote_agent_id,
            local_agent_id,
            source_file,
            remote_vv_str=remote_vv,
            remote_logical_clock=remote_clock,
        )
        if r["applied"]:
            results["applied"] += 1
        if r["conflict"]:
            results["conflict"] += 1
        if r["rejected"]:
            results["rejected"] += 1
    return results
