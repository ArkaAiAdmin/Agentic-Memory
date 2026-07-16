"""Saga pattern for crash-consistent multi-store writes in agentic-memory.

The memory system is a triple store: SQLite (FTS5 + relational tables) +
usearch (vector index) + markdown files on disk. The save pipeline writes
to all three independently with no transactional coordination, so a
crash mid-save can leave the three stores inconsistent (e.g. DB row
written but the markdown file missing, or a vector indexed but the DB
row rolled back). ``rebuild_vec_index`` exists as a manual repair tool
but is O(N) over the corpus.

This module provides a generic saga implementation that wraps the three
writes behind a context manager. On success, all steps run in order. On
any failure, all completed steps are rolled back in reverse order via
their ``undo`` callables. If an ``undo`` itself fails, the error is
logged and the next undo is still attempted — losing data is acceptable,
crashing the tool call is not.

Gated behind ``MEMORY_SAGA_ENABLED``. Default is ON (1) to prevent database
corruption under sudden process crashes; set to 0 to opt out.

Public surface:
    * :class:`SagaStep` — dataclass describing one (do, undo) pair.
    * :class:`SagaError` — raised when a step fails and rollback is
      exhausted.
    * :class:`Saga` — context manager that runs the steps and rolls
      back on failure.
    * :func:`saga_save_memory` — saga-wrapped triple-store save.
    * :data:`SAGA_ENABLED` — the parsed value of ``MEMORY_SAGA_ENABLED``.

Example::

    from infra.saga import Saga, SagaStep, SagaError

    def add_one(state):
        state["x"] = state.get("x", 0) + 1
        return state["x"]

    def sub_one(state):
        state["x"] -= 1

    with Saga(name="arith", steps=[
        SagaStep("add",    lambda: add_one(state),  lambda: sub_one(state)),
        SagaStep("double", lambda: (state.update(x=state["x"]*2) or state["x"]),
                            lambda: (state.update(x=state["x"]//2))),
    ]) as saga:
        result = saga.result
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator, List, Literal, Optional, Union
from enum import Enum

logger = logging.getLogger(__name__)

# P0-5 fix (2026-06-24): sentinel for "do never ran" to distinguish
# from "do returned None".  _rollback skips steps whose do_result is
# this sentinel, not steps whose do_result is None (which is a valid
# return value).
_SAGA_DO_NOT_SET: Any = object()

_deferred_state = threading.local()

def _is_saga_deferred(conn: AnyConnection) -> bool:
    if not hasattr(_deferred_state, "conns"):
        _deferred_state.conns = set()
    return id(conn) in _deferred_state.conns

def _set_saga_deferred(conn: AnyConnection, deferred: bool) -> None:
    if not hasattr(_deferred_state, "conns"):
        _deferred_state.conns = set()
    if deferred:
        _deferred_state.conns.add(id(conn))
    else:
        _deferred_state.conns.discard(id(conn))

class SagaMode(str, Enum):
    PER_STEP = "per_step"
    DEFERRED = "deferred"

__all__ = [
    "Saga",
    "SagaStep",
    "SagaError",
    "SagaMode",
    "saga_save_memory",
    "SAGA_ENABLED",  # noqa: F822 — dynamically resolved via __getattr__
]



# Feature gate. Default ON so crash-consistent triple-store writes are
# protected by the saga coordinator. Reads once at import time; mutating
# the env var at runtime will not flip the value.
# SAGA_ENABLED is dynamically resolved via __getattr__


class SagaError(RuntimeError):
    """Raised when a saga step fails and rollback is exhausted.

    Carries:
        * ``saga_name`` — the saga that failed.
        * ``failed_step`` — name of the step whose ``do`` raised.
        * ``original_error`` — the exception that triggered the failure.
        * ``rollback_errors`` — list of exceptions raised by individual
          ``undo`` callables during rollback (in reverse order, so the
          first entry is the most-recent undo attempt). Best-effort
          rollback may leave the system partially inconsistent; callers
          should log ``rollback_errors`` so ``rebuild_vec_index`` can be
          run if needed.
    """

    def __init__(
        self,
        message: str,
        *,
        saga_name: str,
        failed_step: str,
        original_error: BaseException,
        rollback_errors: Optional[List[BaseException]] = None,
    ) -> None:
        super().__init__(message)
        self.saga_name = saga_name
        self.failed_step = failed_step
        self.original_error = original_error
        self.rollback_errors: List[BaseException] = list(rollback_errors or [])

    def __str__(self) -> str:  # pragma: no cover - formatting only
        parts = [
            f"SagaError(saga={self.saga_name!r}, failed_step={self.failed_step!r}): "
            f"{self.original_error!r}"
        ]
        if self.rollback_errors:
            parts.append(
                f"  rollback_errors={len(self.rollback_errors)} (most recent first): "
                + "; ".join(repr(e) for e in self.rollback_errors)
            )
        return "\n".join(parts)


@dataclass
class SagaStep:
    """One (do, undo) pair inside a :class:`Saga`.

    Attributes:
        name: Human-readable identifier for logging and the SagaError
            ``failed_step`` field. Should be unique within a saga.
        do: Forward action. Called once during ``Saga.__enter__``. May
            return anything; the value is stashed on the saga as
            ``saga.results[i]`` so callers can inspect partial output
            (useful when ``do`` returns a row id, file path, or
            embedding key the ``undo`` callable needs to reference).
        undo: Rollback action. Called once per step that already
            succeeded, in reverse order, if any later step fails. Must
            accept zero positional args. Should not raise; if it does,
            the error is logged and the next undo is still attempted.
    """

    name: str
    do: Callable[[], Any]
    undo: Callable[[], None]


@dataclass
class _StepRecord:
    """Internal bookkeeping for a step that has been executed.

    ``do_result`` captures the return value of ``do`` so callers can
    read ``saga.results[i]`` after the saga has committed (or during
    rollback, where the value often identifies what to undo).
    """

    step: SagaStep
    do_result: Any = _SAGA_DO_NOT_SET
    rolled_back: bool = False


class Saga:
    """Context manager that runs a sequence of steps and rolls back on failure.

    Usage::

        with Saga(name="save_memory", steps=[...]) as saga:
            # all steps have executed and committed
            do_results = saga.results
        # __exit__ has already run. If any step failed,
        # the saga has already rolled back and re-raised SagaError;
        # control does not reach the post-with line.

    Semantics:
        * On success: ``do`` is called for every step in order, then
          ``__exit__`` marks the saga as committed. No undos run.
        * On failure: the failed step's exception is caught, every
          previously-successful step is undone in reverse order (best
          effort; undo errors are logged and collected), and a
          :class:`SagaError` is raised carrying the original error and
          the rollback error list.
        * If the ``with`` block itself raises, the saga rolls back
          and re-raises the new exception as the cause; the
          ``SagaError`` is chained via ``raise ... from``.
        * If ``undo`` raises, the error is logged and the next undo
          is still attempted. The original failure is what the caller
          ultimately sees; rollback errors are on the SagaError.

    The context manager is single-use. Reusing the same Saga instance
    for two ``with`` blocks is undefined behavior — build a new one.
    """

    def __init__(
        self,
        name: str,
        steps: List[SagaStep],
        *,
        conn: Any = None,
        mode: SagaMode = SagaMode.DEFERRED,
        on_rollback: Optional[Callable[[SagaError], None]] = None,
    ) -> None:
        if not steps:
            raise ValueError(f"Saga({name!r}) requires at least one step")
        self.name = name
        self._steps: List[SagaStep] = list(steps)
        self.conn = conn
        self.mode = mode
        self._on_rollback = on_rollback
        self._records: List[_StepRecord] = [_StepRecord(step=s) for s in self._steps]
        # What the most recent ``do`` returned, per step. None until
        # the step runs. Public read-only handle for callers.
        self.results: List[Any] = [None] * len(self._steps)
        self.committed: bool = False
        self.rolled_back: bool = False
        self._error: Optional[BaseException] = None
        self._started_transaction: bool = False

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    @staticmethod
    def _is_proxy(conn) -> bool:
        return hasattr(conn, "_cmd_queue")

    def __enter__(self) -> "Saga":
        if self.conn is not None and self.mode == SagaMode.DEFERRED:
            if self._is_proxy(self.conn):
                pass
            elif not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
                self._started_transaction = True
            else:
                self.conn.execute("SAVEPOINT saga_sp")
                self._started_transaction = False
            _set_saga_deferred(self.conn, True)
        try:
            for idx, step in enumerate(self._steps):
                logger.info(
                    "saga[%s] step %d/%d %r: starting",
                    self.name,
                    idx + 1,
                    len(self._steps),
                    step.name,
                )
                try:
                    result = step.do()
                except Exception as exc:
                    logger.error(
                        "saga[%s] step %d/%d %r: FAILED with %r",
                        self.name,
                        idx + 1,
                        len(self._steps),
                        step.name,
                        exc,
                    )
                    self._error = exc
                    if self.conn is not None and self.mode == SagaMode.DEFERRED:
                        if not self._is_proxy(self.conn):
                            try:
                                if self._started_transaction:
                                    self.conn.rollback()
                                else:
                                    self.conn.execute("ROLLBACK TO SAVEPOINT saga_sp")
                                    self.conn.execute("RELEASE SAVEPOINT saga_sp")
                            except Exception as sp_err:
                                logger.warning("saga rollback failed: %r", sp_err)
                        else:
                            # Proxy connection: rollback the partial writes
                            # from the failed transaction, then start a new
                            # transaction for the compensating undo writes.
                            try:
                                self.conn.rollback()
                            except Exception as proxy_rb_err:
                                logger.warning("saga proxy rollback failed: %r", proxy_rb_err)
                    # Mark the failed step as "ran" so _rollback calls its
                    # undo (and any prior steps' undo). Without this, the
                    # sentinel check in _rollback skips all undo callables
                    # when a step raises, leaving compensating writes
                    # (e.g. DELETE FROM memories) unexecuted.
                    self._records[idx].do_result = True
                    rollback_errors = self._rollback(idx)
                    # For proxy connections: commit the compensating undo
                    # writes so they survive the caller's conn.rollback().
                    if self.conn is not None and self.mode == SagaMode.DEFERRED:
                        if self._is_proxy(self.conn):
                            try:
                                self.conn.commit()
                            except Exception as proxy_commit_err:
                                logger.warning("saga proxy undo commit failed: %r", proxy_commit_err)
                    raise SagaError(
                        f"Saga {self.name!r} failed at step {step.name!r}: {exc!r}",
                        saga_name=self.name,
                        failed_step=step.name,
                        original_error=exc,
                        rollback_errors=rollback_errors,
                    ) from exc
                self._records[idx].do_result = result
                self.results[idx] = result
                logger.info(
                    "saga[%s] step %d/%d %r: OK",
                    self.name,
                    idx + 1,
                    len(self._steps),
                    step.name,
                )
        except SagaError:
            if self.conn is not None and self.mode == SagaMode.DEFERRED:
                _set_saga_deferred(self.conn, False)
            raise
        except Exception as exc:
            self._error = exc
            if self.conn is not None and self.mode == SagaMode.DEFERRED:
                if not self._is_proxy(self.conn):
                    try:
                        if self._started_transaction:
                            self.conn.rollback()
                        else:
                            self.conn.execute("ROLLBACK TO SAVEPOINT saga_sp")
                            self.conn.execute("RELEASE SAVEPOINT saga_sp")
                    except Exception as sp_err:
                        logger.warning("saga rollback on external exception failed: %r", sp_err)
                else:
                    try:
                        self.conn.rollback()
                    except Exception as proxy_rb_err:
                        logger.warning("saga proxy rollback on external exception failed: %r", proxy_rb_err)
                _set_saga_deferred(self.conn, False)
            self._rollback(len(self._records) - 1)
            # Commit compensating undo writes on proxy connections.
            if self.conn is not None and self.mode == SagaMode.DEFERRED:
                if self._is_proxy(self.conn):
                    try:
                        self.conn.commit()
                    except Exception as proxy_commit_err:
                        logger.warning("saga proxy undo commit on external exception failed: %r", proxy_commit_err)
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> "Literal[False]":
        if self.conn is not None and self.mode == SagaMode.DEFERRED:
            _set_saga_deferred(self.conn, False)
        if exc_type is not None:
            # An exception escaped the ``with`` body. Run rollback if
            # any steps completed and we haven't already.
            if not self.rolled_back and any(
                r.do_result is not _SAGA_DO_NOT_SET for r in self._records
            ):
                # Only rollback steps that actually executed (do_result
                # is the marker; a step whose do raised is *not* in
                # the completed set).
                last_completed = -1
                for i, r in enumerate(self._records):
                    if r.do_result is not _SAGA_DO_NOT_SET:
                        last_completed = i
                if last_completed >= 0:
                     self._error = exc
                     if self.conn is not None and self.mode == SagaMode.DEFERRED:
                        if not self._is_proxy(self.conn):
                            try:
                                if self._started_transaction:
                                    self.conn.rollback()
                                else:
                                    self.conn.execute("ROLLBACK TO SAVEPOINT saga_sp")
                                    self.conn.execute("RELEASE SAVEPOINT saga_sp")
                            except Exception as sp_err:
                                logger.warning("saga rollback in exit failed: %r", sp_err)
                        else:
                            try:
                                self.conn.rollback()
                            except Exception as proxy_rb_err:
                                logger.warning("saga proxy rollback in exit failed: %r", proxy_rb_err)
                     self._rollback(last_completed)
                     # Commit compensating undo writes on proxy connections.
                     if self.conn is not None and self.mode == SagaMode.DEFERRED:
                        if self._is_proxy(self.conn):
                            try:
                                self.conn.commit()
                            except Exception as proxy_commit_err:
                                logger.warning("saga proxy undo commit in exit failed: %r", proxy_commit_err)
            # Do not swallow the exception.
            return False
        if self.conn is not None and self.mode == SagaMode.DEFERRED:
            if not self._is_proxy(self.conn):
                try:
                    if self._started_transaction:
                        self.conn.commit()
                    else:
                        self.conn.execute("RELEASE SAVEPOINT saga_sp")
                except Exception as sp_err:
                    logger.error("saga commit/release failed: %r", sp_err)
                    self.committed = False
                    raise SagaError(f"Saga commit failed: {sp_err}") from sp_err
        self.committed = True
        logger.info("saga[%s] committed (%d steps)", self.name, len(self._steps))
        return False

    # ------------------------------------------------------------------
    # Rollback machinery
    # ------------------------------------------------------------------

    def _rollback(self, last_completed_idx: int) -> List[BaseException]:
        """Undo every completed step from ``last_completed_idx`` down to 0.

        ``last_completed_idx`` is the *index of the failed step* (not
        the last successful one). We skip any step whose ``do`` did
        not complete — identified by ``do_result is None`` — so we
        never call ``undo`` for a step whose forward side never
        produced a side effect to revert. (Calling the failed step's
        own ``undo`` would also be wrong: the failed ``do`` either
        raised before mutating state or raised partway through, so
        we cannot guarantee a clean reversal.)

        Best effort: each ``undo`` is called even if the previous one
        raised. Errors are logged and collected; the list is returned
        so the caller can attach them to the SagaError that gets
        raised.

        Returns the list of exceptions raised by individual undo
        callables (most-recent undo attempt first). Empty if every
        undo succeeded.
        """
        rollback_errors: List[BaseException] = []
        for idx in range(last_completed_idx, -1, -1):
            record = self._records[idx]
            if record.rolled_back:
                continue
            # Only undo steps that actually completed. A step whose
            # ``do`` raised (or never ran) has ``do_result is
            # _SAGA_DO_NOT_SET``; calling its ``undo`` would be incorrect.
            if record.do_result is _SAGA_DO_NOT_SET:
                continue
            step = record.step
            try:
                step.undo()
            except Exception as undo_exc:
                logger.error(
                    "saga[%s] rollback step %d/%d %r: FAILED with %r",
                    self.name,
                    idx + 1,
                    len(self._steps),
                    step.name,
                    undo_exc,
                )
                # Most recent first — easier to read at the top of the
                # SagaError string.
                rollback_errors.append(undo_exc)
            else:
                logger.info(
                    "saga[%s] rollback step %d/%d %r: OK",
                    self.name,
                    idx + 1,
                    len(self._steps),
                    step.name,
                )
            finally:
                record.rolled_back = True
        self.rolled_back = True

        if rollback_errors and self._error is not None:
            # Attach to the original error so the caller's except
            # clause sees both. ``__exit__`` raises SagaError elsewhere
            # — this branch is for the path where the saga object is
            # inspected directly (e.g. tests).
            err = self._error
            if isinstance(err, SagaError):
                err.rollback_errors.extend(rollback_errors)
            else:
                # Wrap a fresh SagaError carrying the rollback info.
                wrapped = SagaError(
                    f"Saga {self.name!r} failed at non-step boundary: {err!r}",
                    saga_name=self.name,
                    failed_step="<with-block>",
                    original_error=err,
                    rollback_errors=rollback_errors,
                )
                wrapped.__cause__ = err
                self._error = wrapped

        if self._on_rollback is not None:
            try:
                self._on_rollback(
                    SagaError(
                        f"Saga {self.name!r} rolled back",
                        saga_name=self.name,
                        failed_step="<see caller>",
                        original_error=self._error or RuntimeError("unknown"),
                        rollback_errors=rollback_errors,
                    )
                )
            except Exception:
                # on_rollback must never re-raise — it is a hook, not
                # control flow.
                logger.exception("saga[%s] on_rollback hook raised", self.name)

        return rollback_errors

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def error(self) -> Optional[BaseException]:
        """The original exception that triggered rollback, if any."""
        return self._error


# ----------------------------------------------------------------------
# saga_save_memory — concrete saga wrapping the triple-store write
# ----------------------------------------------------------------------
#
# This is the spec'd entry point. It mirrors the existing
# save_pipeline.save_memory contract (returns the note_id) but wraps
# the three independent writes — SQLite (FTS5 + memories row + related
# triggers), usearch vector index (via the memory_vec_keys singleton),
# and the markdown file via atomic_write — in a single saga so a crash
# mid-write does not leave the stores inconsistent.
#
# Gated by MEMORY_SAGA_ENABLED. When the gate is off (set to 0), the
# function falls back to direct writes (no rollback) and simply returns
# the note_id. The fallback is deliberately identical to the pre-saga
# behavior so disabling the flag is a clean opt-out.
#
# IMPORTANT: This function does NOT replace save_pipeline.save_memory
# (per the build instructions: "Do NOT modify save_pipeline.py yet").
# It is a parallel implementation that callers can switch to once
# the saga gate is enabled. The undo callables below undo exactly
# the side effects that this function's do callables produce — they
# are NOT a general undo for save_pipeline.save_memory.


@dataclass
class _SaveMemoryParams:
    """Internal carrier so the closures below can capture by reference.

    Using a small dataclass instead of free variables means the undo
    callables have stable names (``params.note_id``) regardless of how
    the surrounding helpers reorganize the do paths.
    """

    note_id: str
    file_path: Path
    db_path: Path
    conn: AnyConnection
    wrote_file: bool = False
    wrote_vec_key: bool = False
    vec_key_value: Optional[int] = None
    initial_existed: bool = False  # was the row already present before this save?
    initial_content: Optional[str] = None
    initial_tags: Optional[str] = None
    # Scenario 4 fix (2026-06-22): snapshot of the on-disk .md
    # content at saga start.  Used by _do_file to detect concurrent
    # edits and preserve the "losing" version as a conflict file.
    initial_file_content: Optional[str] = None
    # Sprint 1.1: Snapshot of pre-existing dependent row IDs for selective rollback
    # These are populated before the saga starts for UPDATE-style saves.
    pre_existing_chunk_ids: set = None
    pre_existing_embedding_ids: set = None
    pre_existing_kg_fact_ids: set = None

    def __post_init__(self):
        if self.pre_existing_chunk_ids is None:
            self.pre_existing_chunk_ids = set()
        if self.pre_existing_embedding_ids is None:
            self.pre_existing_embedding_ids = set()
        if self.pre_existing_kg_fact_ids is None:
            self.pre_existing_kg_fact_ids = set()


def _delete_memory_row(conn: AnyConnection, note_id: str) -> None:
    """Delete a single memory row as part of saga rollback.

    Always commits the DELETE so it survives the saga's own
    conn.rollback() (which already undid the failed INSERT).
    The prior ``_is_saga_deferred`` guard prevented the commit,
    leaving the row in place after the saga raised.
    """
    try:
        conn.execute("DELETE FROM memories WHERE id = ?", (note_id,))
        try:
            conn.commit()
        except Exception as commit_exc:
            logger.debug("saga undo: DELETE commit for %s failed: %r", note_id, commit_exc)
    except Exception as exc:
        logger.warning(
            "saga undo: DELETE FROM memories for %s failed: %r", note_id, exc
        )


def _cleanup_dependent_rows(
    conn: AnyConnection,
    note_id: str,
    preserve_chunk_ids: Optional[set] = None,
    preserve_embedding_ids: Optional[set] = None,
    preserve_kg_fact_ids: Optional[set] = None,
) -> None:
    """Best-effort cleanup of kg_facts / kg_edges / backlinks rows for *note_id*.

    B-3 fix (2026-06-22 follow-up): the saga rollback path can leave
    orphan rows in kg_facts, kg_edges, and backlinks when an
    intermediate post-save hook wrote to them between the upsert
    and the failure point.  The ``memories`` row gets rolled back by
    ``_delete_memory_row``, but the dependent tables have no FK
    cascade (or, post-migration 017, the cascade only fires on
    explicit DELETE FROM memories — which we don't always issue on
    rollback for an UPDATE).  This helper ensures those dependent
    rows go away too.

    Sprint 1.1: When preserve_*_ids are provided (UPDATE rollback case),
    only delete rows NOT in the preserve set. This preserves pre-existing
    chunks/embeddings/kg_facts from before the update.

    All operations are best-effort: each is wrapped in try/except so
    a schema mismatch on a legacy DB logs and continues, matching
    the convention in ``memory_delete._purge_orphaned_kg``.
    """
    try:
        from save.cleanup import (
            cleanup_memory_relations,
            remove_chunks_and_embeddings_for_note,
            remove_kg_facts_selective,
            remove_chunks_selective,
            remove_embeddings_selective,
        )

        # For UPDATE rollback: only delete rows created during this saga
        if preserve_kg_fact_ids is not None:
            remove_kg_facts_selective(conn, note_id, preserve_kg_fact_ids)
        else:
            cleanup_memory_relations(conn, note_id)

        if preserve_chunk_ids is not None or preserve_embedding_ids is not None:
            remove_chunks_selective(conn, note_id, preserve_chunk_ids or set())
            remove_embeddings_selective(conn, note_id, preserve_embedding_ids or set())
            # Also clean vec_keys for the note
            try:
                conn.execute("DELETE FROM memory_vec_keys WHERE memory_id = ?", (note_id,))
            except Exception as exc:
                logger.warning("saga undo: remove_vec_keys for %s: %r", note_id, exc)
        else:
            remove_chunks_and_embeddings_for_note(conn, note_id)
    except Exception as exc:
        logger.warning(
            "saga undo: cleanup_memory_relations for %s failed: %r",
            note_id,
            exc,
        )


def _restore_memory_row(
    conn: AnyConnection,
    note_id: str,
    content: str,
    tags: str,
    metadata_json: Optional[str] = None,
    pinned: bool = False,
    tier: Optional[str] = None,
    importance_score: Optional[float] = None,
    fitness_score: Optional[float] = None,
) -> None:
    """Best-effort restore of a pre-existing memory row."""
    if not content:
        return
    try:
        conn.execute(
            "UPDATE memories SET content = ?, tags = ?, pinned = ?, "
            "tier = ?, importance_score = ?, fitness_score = ? WHERE id = ?",
            (
                content,
                tags,
                1 if pinned else 0,
                tier,
                importance_score,
                fitness_score,
                note_id,
            ),
        )
        if metadata_json is not None:
            try:
                conn.execute(
                    "UPDATE memories SET metadata = ? WHERE id = ?",
                    (metadata_json, note_id),
                )
            except sqlite3.OperationalError:
                # metadata column may not exist on databases that haven't
                # applied migration 005 (_migrate_columns_indexes_chunks).
                # Graceful degradation — metadata is best-effort.
                pass
        if not _is_saga_deferred(conn):
            conn.commit()
    except Exception as exc:
        logger.warning("saga undo: restore UPDATE for %s failed: %r", note_id, exc)


def _remove_vec_key(conn: AnyConnection, note_id: str) -> None:
    """Best-effort removal of the usearch key->memory_id mapping."""
    try:
        conn.execute("DELETE FROM memory_vec_keys WHERE memory_id = ?", (note_id,))
        if not _is_saga_deferred(conn):
            conn.commit()
    except Exception as exc:
        logger.warning(
            "saga undo: DELETE FROM memory_vec_keys for %s failed: %r", note_id, exc
        )


def _unlink_file(path: Path) -> None:
    """Best-effort unlink of a markdown file. Logs and swallows."""
    try:
        if path.exists():
            path.unlink()
    except Exception as exc:
        logger.warning("saga undo: unlink %s failed: %r", path, exc)


def _capture_pre_existing(conn, note_id: str):
    """Read the memories row (if any) before the save runs.

    Used by the upsert undo path: if the row pre-existed, the undo
    restores its old content/tags; if it was a fresh insert, the undo
    just deletes the row.
    """
    try:
        row = conn.execute(
            "SELECT content, tags FROM memories WHERE id = ?", (note_id,)
        ).fetchone()
        if row is not None:
            return (row[0], row[1])
    except Exception as _capture_exc:
        logger.debug("_capture_pre_existing failed for %s: %s", note_id, _capture_exc)
    return None


def _build_save_memory_steps(
    *,
    conn,
    note_id: str,
    file_path: Path,
    db_path: Path,
    do_upsert_db,
    do_write_vec_key,
    do_write_file,
) -> tuple[list[SagaStep], _SaveMemoryParams]:
    """Build the (steps, params) tuple for the save-memory saga.

    Extracted 2026-06-22 so saga_save_memory stays readable. Returns
    the three SagaStep instances plus the ``_SaveMemoryParams`` they
    need at undo time.

    Scenario 4 fix (2026-06-22): the .md file's pre-existing content
    is captured here and passed to ``safe_atomic_write`` so a
    concurrent edit (e.g. a second opencode session modifying the
    same .md between saga start and file write) is preserved as
    a conflict file instead of being silently overwritten (LWW).
    """
    pre_existing = _capture_pre_existing(conn, note_id)
    # Capture the on-disk .md content (if any) so the file-write
    # step can detect concurrent edits.  ``None`` means the file
    # did not exist — that's fine, no conflict possible.
    pre_existing_file: str | None = None
    try:
        if file_path.exists():
            pre_existing_file = file_path.read_text(encoding="utf-8")
    except Exception as _read_exc:
        logger.debug("pre-existing file read failed for %s: %s", file_path, _read_exc)
        pre_existing_file = None
    # Sprint 1.1: Snapshot pre-existing dependent row IDs for selective rollback
    pre_existing_chunk_ids = set()
    pre_existing_embedding_ids = set()
    pre_existing_kg_fact_ids = set()
    if pre_existing is not None:
        try:
            rows = conn.execute(
                "SELECT id FROM memory_chunks WHERE parent_id = ?", (note_id,)
            ).fetchall()
            pre_existing_chunk_ids = {r[0] for r in rows}
        except Exception:
            pass
        try:
            rows = conn.execute(
                "SELECT id FROM memory_embeddings WHERE memory_id = ?", (note_id,)
            ).fetchall()
            pre_existing_embedding_ids = {r[0] for r in rows}
        except Exception:
            pass
        try:
            rows = conn.execute(
                "SELECT id FROM kg_facts WHERE source_memory = ?", (note_id,)
            ).fetchall()
            pre_existing_kg_fact_ids = {r[0] for r in rows}
        except Exception:
            pass

    params = _SaveMemoryParams(
        note_id=note_id,
        file_path=file_path,
        db_path=db_path,
        conn=conn,
        initial_existed=pre_existing is not None,
        initial_content=pre_existing[0] if pre_existing else None,
        initial_tags=pre_existing[1] if pre_existing else None,
        initial_file_content=pre_existing_file,
        pre_existing_chunk_ids=pre_existing_chunk_ids,
        pre_existing_embedding_ids=pre_existing_embedding_ids,
        pre_existing_kg_fact_ids=pre_existing_kg_fact_ids,
    )

    def _do_upsert() -> str:
        do_upsert_db()
        return note_id

    def _undo_upsert() -> None:
        if params.initial_existed:
            _restore_memory_row(
                params.conn,
                params.note_id,
                params.initial_content or "",
                params.initial_tags or "[]",
            )
            # Sprint 1.1: For UPDATE rollback, preserve pre-existing rows
            # by passing the snapshot of IDs that existed before the update.
            # Only delete rows that were created during this saga, not
            # pre-existing ones from before the update.
            _cleanup_dependent_rows(
                params.conn,
                params.note_id,
                preserve_chunk_ids=params.pre_existing_chunk_ids,
                preserve_embedding_ids=params.pre_existing_embedding_ids,
                preserve_kg_fact_ids=params.pre_existing_kg_fact_ids,
            )
        else:
            _delete_memory_row(params.conn, params.note_id)
            # B-3 fix: clean up any dependent rows that were written
            # between the INSERT and the failure point.  Without this
            # the saga rollback would leave orphan kg_facts / kg_edges
            # / backlinks rows.
            _cleanup_dependent_rows(params.conn, params.note_id)

    def _do_vec_key() -> int:
        # The user-supplied callable is responsible for picking a
        # stable uint64 key. We capture whatever it returns so the
        # undo callable can confirm the right row got deleted.
        result = do_write_vec_key()
        params.wrote_vec_key = True
        params.vec_key_value = result
        return result if result is not None else 0

    def _undo_vec_key() -> None:
        if params.wrote_vec_key:
            _remove_vec_key(params.conn, params.note_id)

    def _do_file() -> Path:
        # Scenario 4 fix (2026-06-22): concurrent-edit detection.
        #
        # For existing files we must detect a concurrent modification
        # between saga-start (params.initial_file_content snapshotted
        # at _build_save_memory_steps time) and our write.  The correct
        # order is:
        #   1. Read the current on-disk content BEFORE any write.
        #   2. If current != initial_file_content, a concurrent edit
        #      is in flight — preserve the "losing" on-disk version as
        #      a conflict file before we overwrite it.
        #   3. Call do_write_file() (which is already atomic via
        #      atomic_write) to persist the new markdown.
        #
        # Prior BUG (2026-07): two separate bugs compounded:
        #   (a) _read_new_content_for_file() was called before
        #       do_write_file() ran, so the OLD content was fetched
        #       and written back by safe_atomic_write — new markdown
        #       was silently dropped for every edit of an existing file.
        #   (b) In the safe_atomic_write except-fallback, do_write_file()
        #       silently overwrote any concurrent edit without saving a
        #       conflict file.
        if params.initial_file_content is not None:
            try:
                current_on_disk = file_path.read_text(encoding="utf-8")
            except Exception:
                current_on_disk = None
            if current_on_disk is not None and current_on_disk != params.initial_file_content:
                import time as _time

                ts = int(_time.time())
                conflict_path = file_path.with_suffix(
                    f"{file_path.suffix}.conflict-{os.getpid()}-{ts}"
                )
                try:
                    conflict_path.write_text(current_on_disk, encoding="utf-8")
                    logger.warning(
                        "saga _do_file: concurrent edit on %s detected; "
                        "conflict content saved to %s",
                        file_path,
                        conflict_path,
                    )
                except Exception as _conflict_exc:
                    logger.warning(
                        "saga _do_file: failed to save conflict file %s: %s",
                        conflict_path,
                        _conflict_exc,
                    )
            try:
                do_write_file()
            except Exception as _write_exc:
                logger.warning(
                    "do_write_file failed in saga _do_file for %s: %s",
                    file_path,
                    _write_exc,
                )
                raise
        else:
            do_write_file()
        params.wrote_file = True
        return file_path

    def _undo_file() -> None:
        if params.wrote_file:
            _unlink_file(file_path)

    steps = [
        SagaStep(name="upsert_db", do=_do_upsert, undo=_undo_upsert),
        SagaStep(name="write_vec_key", do=_do_vec_key, undo=_undo_vec_key),
        SagaStep(name="write_file", do=_do_file, undo=_undo_file),
    ]
    return steps, params


def saga_save_memory(
    *,
    conn: AnyConnection,
    note_id: str,
    file_path: Union[str, Path],
    markdown_content: str,
    db_path: Union[str, Path],
    do_upsert_db,
    do_write_vec_key,
    do_write_file,
) -> str:
    """Triple-store save wrapped in a saga. Returns ``note_id`` on success.

    Args:
        conn: Open sqlite3 connection (the caller's connection is used
            so the save participates in any outer transaction; the
            saga commits per-step so each step is durable on its own).
        note_id: Canonical note id (``"category/title_slug"``).
        file_path: Destination path for the markdown file.
        markdown_content: The full markdown body to write.
        db_path: Path to the sqlite DB, used for logging only.
        do_upsert_db: Callable that performs the SQLite write.
        do_write_vec_key: Callable that inserts the memory_vec_keys row.
        do_write_file: Callable that writes the markdown file (use
            ``atomic_write`` for the same crash-safety properties the
            rest of the codebase already relies on).

    Returns:
        The ``note_id`` on success.

    Raises:
        SagaError: if any step fails. The previously-successful steps
            are rolled back in reverse order. The original exception
            is chained via ``__cause__``.

    When ``MEMORY_SAGA_ENABLED`` is set to 0, this function falls back
    to running the three callables directly with no rollback.

    Decomposed 2026-06-22: serialize lock, pre-state capture, and step
    construction are now separate helpers. The orchestrator below is
    the read-the-doc string and the dispatch.
    """
    file_path = Path(file_path)
    db_path = Path(db_path)

    if not SAGA_ENABLED:
        do_upsert_db()
        do_write_vec_key()
        do_write_file()
        return note_id

    steps, _params = _build_save_memory_steps(
        conn=conn,
        note_id=note_id,
        file_path=file_path,
        db_path=db_path,
        do_upsert_db=do_upsert_db,
        do_write_vec_key=do_write_vec_key,
        do_write_file=do_write_file,
    )

    try:
        with Saga(name="save_memory", steps=steps, conn=conn, mode=SagaMode.DEFERRED) as saga:
            result = saga.results[0]
            return result if result is not None else note_id
    except SagaError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise SagaError(
            f"saga_save_memory: unexpected error in saga {note_id!r}: {exc!r}",
            saga_name="save_memory",
            failed_step="<unknown>",
            original_error=exc,
        ) from exc


# ----------------------------------------------------------------------
# Convenience context manager for callers that just want the Saga
# without the save_memory specifics.
# ----------------------------------------------------------------------


@contextmanager
def saga(name: str, steps: List[SagaStep]) -> Generator[Saga, None, None]:
    """Sugar over :class:`Saga` for callers that prefer a function.

    Example::

        from infra.saga import saga, SagaStep
        with saga("two-step", [step1, step2]):
            ...
    """
    with Saga(name=name, steps=steps) as s:
        yield s


from infra.memory_common import make_lazy_getattr
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


__getattr__ = make_lazy_getattr({"SAGA_ENABLED": "saga_enabled"})

# Pin the SAGA_ENABLED gate as a real module attribute at import time.
# make_lazy_getattr (infra/memory_common.py) caches resolved values into its
# OWN globals, not this module's, so a bare `SAGA_ENABLED` reference inside
# saga_save_memory would never resolve. More importantly, the old code read
# it via sys.modules[__name__].SAGA_ENABLED, which raised KeyError whenever
# this module had been removed from sys.modules (test_graph_behavior.py
# deletes every infra.* entry at import time). Pinning the resolved bool
# here means the value lives in __dict__ and survives sys.modules removal
# (the module object persists in memory via references). This is consistent
# with the documented "read once at import time" semantics.
try:
    SAGA_ENABLED = bool(sys.modules[__name__].SAGA_ENABLED)
except Exception as _wp_exc:
    logger.warning("<module>: broad except swallowed: %s", _wp_exc)
    SAGA_ENABLED = True
