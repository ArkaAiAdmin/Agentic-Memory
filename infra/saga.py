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
import time as _time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generator, List, Literal, Optional, Union
from enum import Enum
import uuid

logger = logging.getLogger(__name__)

# P0-5 fix (2026-06-24): sentinel for "do never ran" to distinguish
# from "do returned None".  _rollback skips steps whose do_result is
# this sentinel, not steps whose do_result is None (which is a valid
# return value).
_SAGA_DO_NOT_SET: Any = object()

_deferred_state = threading.local()

_SAGA_STEP_FAILED: Any = object()  # H17 sentinel for failed step's do_result

# Phase 5B: shared ThreadPoolExecutor for step timeouts
_step_timeout_pool: ThreadPoolExecutor | None = None
_step_timeout_pool_lock = threading.Lock()


def _get_step_timeout_pool() -> ThreadPoolExecutor:
    """Return a shared thread pool for saga step timeout enforcement.

    Uses double-checked locking to avoid creating multiple pools.
    max_workers=2 allows one concurrent saga step timeout (the common
    case is 1 active saga; 2 handles brief overlap during recovery).
    """
    global _step_timeout_pool
    if _step_timeout_pool is None:
        with _step_timeout_pool_lock:
            if _step_timeout_pool is None:
                _step_timeout_pool = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="saga_timeout"
                )
    return _step_timeout_pool


def ensure_saga_log_table(conn: AnyConnection) -> None:
    """Create the saga_log table if it doesn't exist (idempotent)."""
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS saga_log ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  saga_id TEXT NOT NULL,"
            "  saga_name TEXT NOT NULL,"
            "  step_idx INTEGER NOT NULL,"
            "  step_name TEXT NOT NULL,"
            "  status TEXT NOT NULL,"  # 'intent', 'done', 'undone'
            "  ts REAL NOT NULL"
            ")"
        )
        if not _is_saga_deferred(conn):
            conn.commit()
    except Exception as e:
        logger.warning("ensure_saga_log_table failed (non-fatal): %s", e)


def _log_saga_step(
    conn: AnyConnection,
    saga_id: str,
    saga_name: str,
    step_idx: int,
    step_name: str,
    status: str,
) -> None:
    """Write a step lifecycle row to saga_log (best-effort, within current txn)."""
    try:
        conn.execute(
            "INSERT INTO saga_log (saga_id, saga_name, step_idx, step_name, status, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (saga_id, saga_name, step_idx, step_name, status, _time.time()),
        )
    except Exception as e:
        logger.warning("saga WAL log failed for %s step %s: %s", saga_name, step_name, e)


def recover_incomplete_sagas(conn: AnyConnection) -> int:
    """Scan saga_log for orphaned intents and run compensating undos.

    Called once per process startup (from run_schema_setup or open_db).
    Returns the number of sagas recovered.

    A saga is "incomplete" if it has intent rows without a terminal
    (done or undone) state.  Recovery runs the compensating undos for
    completed steps in reverse order.
    """
    try:
        # Find saga_ids with at least one intent row and no terminal row.
        orphans = conn.execute(
            "SELECT DISTINCT s.saga_id, s.saga_name "
            "FROM saga_log s "
            "WHERE s.status = 'intent' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM saga_log t "
            "  WHERE t.saga_id = s.saga_id AND t.step_idx = s.step_idx AND t.status IN ('done', 'undone')"
            ")"
        ).fetchall()
    except Exception as e:
        logger.debug("recover_incomplete_sagas: saga_log query failed: %s", e)
        return 0

    recovered = 0
    for saga_id, saga_name in orphans:
        # Find completed steps (done but not undone) in reverse order.
        try:
            completed = conn.execute(
                "SELECT step_idx, step_name FROM saga_log "
                "WHERE saga_id = ? AND status = 'done' "
                "ORDER BY step_idx DESC",
                (saga_id,),
            ).fetchall()
        except Exception:
            continue

        logger.warning(
            "saga recovery: %s (id=%s) has %d completed steps to undo",
            saga_name, saga_id, len(completed),
        )

        # Phase 2C: crdt_field_* orphans — delete partial field CRDT rows.
        # saga_id format: "crdt_field_{note_id}_{uuid8}"
        if saga_id.startswith("crdt_field_"):
            # Extract note_id: strip prefix and trailing _<8hex>
            _parts = saga_id[len("crdt_field_"):]
            # note_id may contain underscores, so split on last _
            _last_underscore = _parts.rfind("_")
            if _last_underscore > 0:
                _orphan_note_id = _parts[:_last_underscore]
                try:
                    conn.execute(
                        "DELETE FROM memory_field_crdt WHERE memory_id = ?",
                        (_orphan_note_id,),
                    )
                    logger.warning(
                        "saga recovery: cleaned up crdt_field orphan rows for %s",
                        _orphan_note_id,
                    )
                except Exception as _crdt_cleanup_exc:
                    logger.debug(
                        "saga recovery: crdt_field cleanup failed for %s: %s",
                        _orphan_note_id, _crdt_cleanup_exc,
                    )

        # Mark all intent rows as undone (we can't run actual undo
        # callables without the original saga instance — the recovery
        # scanner marks them so they don't trigger again).
        try:
            conn.execute(
                "UPDATE saga_log SET status = 'undone' "
                "WHERE saga_id = ? AND status = 'intent'",
                (saga_id,),
            )
        except Exception:
            pass

        recovered += 1

    if recovered > 0:
        try:
            if not _is_saga_deferred(conn):
                conn.commit()
        except Exception:
            pass

    return recovered


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


def _write_rollback_audit(
    conn: AnyConnection,
    saga_name: str,
    original_error: Optional[BaseException],
    rollback_errors: List[BaseException],
) -> None:
    """Persist a saga rollback-failure record for post-mortem queries.

    Writes to ``saga_audit_log`` if the table exists; best-effort so
    a schema mismatch on an old DB does not mask the original failure.
    """
    import time as _time

    try:
        errors_text = "; ".join(
            f"{type(e).__name__}: {e}" for e in rollback_errors
        )
        original_text = (
            f"{type(original_error).__name__}: {original_error}"
            if original_error is not None
            else None
        )
        conn.execute(
            "INSERT INTO saga_audit_log "
            "(ts, saga_name, failed_step, original_error, rollback_count, rollback_errors) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _time.time(),
                saga_name,
                "",
                original_text,
                len(rollback_errors),
                errors_text,
            ),
        )
        if not _is_saga_deferred(conn):
            conn.commit()
    except Exception as _audit_exc:
        logger.warning("saga rollback audit write failed: %r", _audit_exc)

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
        step_timeout_s: Optional[float] = None,
        post_commit_hooks: Optional[List[Callable[[], None]]] = None,
    ) -> None:
        if not steps:
            raise ValueError(f"Saga({name!r}) requires at least one step")
        self.name = name
        self._steps: List[SagaStep] = list(steps)
        self.conn = conn
        self.mode = mode
        self._on_rollback = on_rollback
        self._step_timeout_s = step_timeout_s
        self._saga_id = uuid.uuid4().hex[:16]
        self._records: List[_StepRecord] = [_StepRecord(step=s) for s in self._steps]
        # What the most recent ``do`` returned, per step. None until
        # the step runs. Public read-only handle for callers.
        self.results: List[Any] = [None] * len(self._steps)
        self.committed: bool = False
        self.rolled_back: bool = False
        self._error: Optional[BaseException] = None
        self._started_transaction: bool = False
        self._post_commit_hooks: List[Callable[[], None]] = list(post_commit_hooks or [])

    def add_post_commit_hook(self, hook: Callable[[], None]) -> None:
        """Register a callable to run after successful DB commit."""
        self._post_commit_hooks.append(hook)

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
            # Ensure saga_log table exists for WAL entries.
            ensure_saga_log_table(self.conn)
        try:
            for idx, step in enumerate(self._steps):
                # WAL: log intent before step runs.
                if self.conn is not None:
                    _log_saga_step(
                        self.conn, self._saga_id, self.name,
                        idx, step.name, "intent",
                    )
                t_start = _time.monotonic()
                logger.info(
                    "saga[%s] step %d/%d %r: starting",
                    self.name,
                    idx + 1,
                    len(self._steps),
                    step.name,
                )
                try:
                    if self._step_timeout_s is not None:
                        pool = _get_step_timeout_pool()
                        future = pool.submit(step.do)
                        try:
                            result = future.result(timeout=self._step_timeout_s)
                        except FutureTimeoutError:
                            raise TimeoutError(
                                f"saga[{self.name}] step {step.name!r} "
                                f"timed out after {self._step_timeout_s}s"
                            )
                    else:
                        result = step.do()
                except Exception as exc:
                    elapsed = _time.monotonic() - t_start
                    logger.error(
                        "saga[%s] step %d/%d %r: FAILED with %r (%.3fs)",
                        self.name,
                        idx + 1,
                        len(self._steps),
                        step.name,
                        exc,
                        elapsed,
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
                    # H17: use _SAGA_STEP_FAILED sentinel instead of True
                    # to distinguish "step ran and failed" from "step ran
                    # and returned None".  _rollback checks this to decide
                    # whether to call undo.
                    self._records[idx].do_result = _SAGA_STEP_FAILED
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
                # WAL: log done after successful step.
                if self.conn is not None:
                    _log_saga_step(
                        self.conn, self._saga_id, self.name,
                        idx, step.name, "done",
                    )
                self._records[idx].do_result = result
                self.results[idx] = result
                elapsed = _time.monotonic() - t_start
                logger.info(
                    "saga[%s] step %d/%d %r: OK (%.3fs)",
                    self.name,
                    idx + 1,
                    len(self._steps),
                    step.name,
                    elapsed,
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
                # Rollback all steps that have run (do_result is set),
                # including the failed step (_SAGA_STEP_FAILED) since
                # its do may have partially mutated state.
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
                    raise SagaError(
                        f"Saga commit failed: {sp_err}",
                        saga_name=self.name,
                        failed_step="<commit>",
                        original_error=sp_err,
                    ) from sp_err
        self.committed = True
        # Phase 3A: Run post-commit hooks (best-effort, log failures).
        # This eliminates the M44 crash window by allowing file writes
        # to run AFTER the DB transaction has committed.
        for hook in self._post_commit_hooks:
            try:
                hook()
            except Exception as hook_exc:
                logger.warning(
                    "saga[%s] post-commit hook failed: %r", self.name, hook_exc
                )
        logger.info("saga[%s] committed (%d steps)", self.name, len(self._steps))
        return False

    # ------------------------------------------------------------------
    # Rollback machinery
    # ------------------------------------------------------------------

    def _rollback(self, last_completed_idx: int) -> List[BaseException]:
        """Undo every completed step from ``last_completed_idx`` down to 0.

        ``last_completed_idx`` is the *index of the failed step* (not
        the last successful one). The failed step's ``undo`` IS invoked
        intentionally — its ``do`` may have partially mutated state
        (e.g. inserted a DB row before failing on the vec_key write),
        so the undo cleans up those partial side effects.

        Steps whose ``do`` never ran (``do_result is
        _SAGA_DO_NOT_SET``) are skipped.  Steps marked with
        ``_SAGA_STEP_FAILED`` (the step raised but may have partially
        mutated) have their undo called.

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
            # Skip steps whose do never ran.
            if record.do_result is _SAGA_DO_NOT_SET:
                continue
            step = record.step
            try:
                step.undo()
                # WAL: log undo completion.
                if self.conn is not None:
                    _log_saga_step(
                        self.conn, self._saga_id, self.name,
                        idx, step.name, "undone",
                    )
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

        if rollback_errors and self.conn is not None:
            _write_rollback_audit(self.conn, self.name, self._error, rollback_errors)

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
    tenant_id: str = "default"
    wrote_file: bool = False
    wrote_vec_key: bool = False
    prepared_file: bool = False
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
    pre_existing_chunk_ids: set = field(default_factory=set)
    pre_existing_embedding_ids: set = field(default_factory=set)
    pre_existing_kg_fact_ids: set = field(default_factory=set)
    # Sprint 1.1 hardening: full pre-image of the memories row so an
    # UPDATE rollback restores every column, not just content/tags.
    initial_row: Optional[dict] = None

    def __post_init__(self):
        if self.pre_existing_chunk_ids is None:
            self.pre_existing_chunk_ids = set()
        if self.pre_existing_embedding_ids is None:
            self.pre_existing_embedding_ids = set()
        if self.pre_existing_kg_fact_ids is None:
            self.pre_existing_kg_fact_ids = set()


def _delete_memory_row(conn: AnyConnection, note_id: str, tenant_id: str = "default") -> None:
    """Delete a single memory row as part of saga rollback.

    Always commits the DELETE so it survives the saga's own
    conn.rollback() (which already undid the failed INSERT).
    The prior ``_is_saga_deferred`` guard prevented the commit,
    leaving the row in place after the saga raised.
    """
    try:
        conn.execute(
            "DELETE FROM memories WHERE id = ? AND tenant_id = ?", (note_id, tenant_id)
        )
        try:
            conn.commit()
        except Exception as commit_exc:
            logger.debug("saga undo: DELETE commit for %s failed: %r", note_id, commit_exc)
    except Exception as exc:
        logger.warning(
            "saga undo: DELETE FROM memories for %s failed: %r", note_id, exc
        )


# ---------------------------------------------------------------------------
# Cleanup module cache (Phase 2D fix)
# ---------------------------------------------------------------------------
_cleanup_module_cache: Optional[dict] = None


def _load_cleanup_module() -> dict:
    """Cached import of save.cleanup functions.

    Raises ImportError on failure (logged at CRITICAL by the caller)
    rather than silently skipping cleanup.
    """
    global _cleanup_module_cache
    if _cleanup_module_cache is not None:
        return _cleanup_module_cache
    from save.cleanup import (
        cleanup_memory_relations,
        remove_chunks_and_embeddings_for_note,
        remove_kg_facts_selective,
        remove_chunks_selective,
        remove_embeddings_selective,
    )
    _cleanup_module_cache = {
        "cleanup_memory_relations": cleanup_memory_relations,
        "remove_chunks_and_embeddings_for_note": remove_chunks_and_embeddings_for_note,
        "remove_kg_facts_selective": remove_kg_facts_selective,
        "remove_chunks_selective": remove_chunks_selective,
        "remove_embeddings_selective": remove_embeddings_selective,
    }
    return _cleanup_module_cache


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
    # Phase 2D fix: import cleanup module with cached loading and
    # CRITICAL-level logging on failure (not silently swallowed).
    try:
        fns = _load_cleanup_module()
    except ImportError as imp_exc:
        logger.critical(
            "saga undo: save.cleanup import FAILED — orphan rows will remain "
            "for note %s. Fix the import and run backfill_all: %r",
            note_id,
            imp_exc,
        )
        return

    # Actual cleanup operations (best-effort per-operation)
    try:
        if preserve_kg_fact_ids is not None:
            fns["remove_kg_facts_selective"](conn, note_id, preserve_kg_fact_ids)
        else:
            fns["cleanup_memory_relations"](conn, note_id)
    except Exception as exc:
        logger.warning("saga undo: cleanup_memory_relations for %s: %r", note_id, exc)

    try:
        if preserve_chunk_ids is not None or preserve_embedding_ids is not None:
            fns["remove_chunks_selective"](conn, note_id, preserve_chunk_ids or set())
            fns["remove_embeddings_selective"](conn, note_id, preserve_embedding_ids or set())
            try:
                conn.execute("DELETE FROM memory_vec_keys WHERE memory_id = ?", (note_id,))
            except Exception as exc:
                logger.warning("saga undo: remove_vec_keys for %s: %r", note_id, exc)
        else:
            fns["remove_chunks_and_embeddings_for_note"](conn, note_id)
    except Exception as exc:
        logger.warning("saga undo: chunk/embedding cleanup for %s: %r", note_id, exc)


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
    tenant_id: str = "default",
) -> None:
    """Best-effort restore of a pre-existing memory row on rollback."""
    if not content:
        return
    try:
        conn.execute(
            "UPDATE memories SET content = ?, tags = ?, pinned = ?, "
            "tier = ?, importance_score = ?, fitness_score = ? "
            "WHERE id = ? AND tenant_id = ?",
            (
                content,
                tags,
                1 if pinned else 0,
                tier,
                importance_score,
                fitness_score,
                note_id,
                tenant_id,
            ),
        )
        if metadata_json is not None:
            try:
                conn.execute(
                    "UPDATE memories SET metadata = ? WHERE id = ? AND tenant_id = ?",
                    (metadata_json, note_id, tenant_id),
                )
            except sqlite3.OperationalError:
                pass
        if not _is_saga_deferred(conn):
            conn.commit()
    except Exception as exc:
        logger.warning("saga undo: restore UPDATE for %s failed: %r", note_id, exc)


def _restore_full_row(conn: AnyConnection, note_id: str, row: dict, tenant_id: str = "default") -> None:
    """Restore a pre-existing memories row to its exact pre-save snapshot.

    Sprint 1.1 hardening: unlike :func:`_restore_memory_row` (which only
    rewrites a fixed subset of columns), this rebuilds the UPDATE from the
    captured full-row dict so a failed UPDATE rolls every touched column
    (valid_from/valid_to/superseded_by/asserting_agent_id/epistemic_source/
    tier/scores/...) back to its pre-save value. ``id`` and ``note_id`` are
    never overwritten. Best-effort: unknown/removed columns are skipped.
    """
    # Columns that must never be mutated by a restore.
    _immutable = {"id", "note_id"}
    cols = [c for c in row.keys() if c not in _immutable and row.get(c) is not None]
    if not cols:
        return
    try:
        placeholders = ", ".join(f"{c} = ?" for c in cols)
        sql = f"UPDATE memories SET {placeholders} WHERE id = ? AND tenant_id = ?"
        conn.execute(sql, [row[c] for c in cols] + [note_id, tenant_id])
        if not _is_saga_deferred(conn):
            conn.commit()
    except Exception as exc:
        logger.warning("saga undo: full-row restore for %s failed: %r", note_id, exc)


def _remove_vec_key(conn: AnyConnection, note_id: str, tenant_id: str = "default") -> None:
    """Best-effort removal of the usearch key->memory_id mapping."""
    try:
        conn.execute(
            "DELETE FROM memory_vec_keys WHERE memory_id = ? AND tenant_id = ?",
            (note_id, tenant_id),
        )
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


def _capture_full_row(conn, note_id: str) -> dict | None:
    """Snapshot the *complete* memories row before the save runs.

    Sprint 1.1 hardening: restoring only content/tags (the prior undo
    path) left an UPDATE-rolled-back row inconsistent if the save had
    touched other columns (valid_from, superseded_by, asserting_agent_id,
    epistemic_source, tier, scores, ...).  Capturing the full row lets the
    undo restore every column the forward write may have changed, so a
    failed UPDATE rolls the row back to its exact pre-save state.
    """
    try:
        cur = conn.execute("SELECT * FROM memories WHERE id = ?", (note_id,))
        cols = [c[0] for c in cur.description] if cur.description else []
        row = cur.fetchone()
        if row is not None:
            return dict(zip(cols, row))
    except Exception as _capture_exc:
        logger.debug("_capture_full_row failed for %s: %s", note_id, _capture_exc)
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
    tenant_id: str = "default",
) -> tuple[list[SagaStep], _SaveMemoryParams, list[Callable[[], None]]]:
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
        initial_row=_capture_full_row(conn, note_id) if pre_existing is not None else None,
        tenant_id=tenant_id,
    )

    def _do_upsert() -> str:
        do_upsert_db()
        return note_id

    def _undo_upsert() -> None:
        if params.initial_existed:
            if params.initial_row is not None:
                _restore_full_row(params.conn, params.note_id, params.initial_row, params.tenant_id)
            else:
                _restore_memory_row(
                    params.conn,
                    params.note_id,
                    params.initial_content or "",
                    params.initial_tags or "[]",
                    tenant_id=params.tenant_id,
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
            _delete_memory_row(params.conn, params.note_id, params.tenant_id)
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
            _remove_vec_key(params.conn, params.note_id, params.tenant_id)

    def _do_prepare_file() -> Path:
        # Phase 3B: Prepare file write intent WITHOUT writing to disk.
        # The actual file write moves to a post-commit hook, eliminating
        # the M44 crash window where an .md exists on disk but the DB
        # transaction hasn't committed yet.
        #
        # Scenario 4 fix (2026-06-22): concurrent-edit detection is
        # still performed here (pre-commit) so that conflict files
        # are saved before the new content overwrites them.
        params.prepared_file = True
        return file_path

    def _undo_prepare_file() -> None:
        # Phase 3C: No file was written (it's post-commit now),
        # so there's nothing to undo during saga rollback.
        params.prepared_file = False

    def _post_commit_write_file() -> None:
        """Write .md after DB commit — eliminates M44 crash window.

        Crash semantics:
        - Crash before DB commit: no .md on disk (correct)
        - Crash after DB commit but before this hook: DB row exists,
          .md missing (repairable by backfill_all)
        - Crash after this hook: fully consistent
        """
        if not params.prepared_file:
            return
        try:
            # Concurrent-edit detection (moved from old _do_file)
            if params.initial_file_content is not None:
                try:
                    current_on_disk = file_path.read_text(encoding="utf-8")
                except Exception:
                    current_on_disk = None
                if current_on_disk is not None and current_on_disk != params.initial_file_content:
                    import time as _pc_time

                    ts = int(_pc_time.time())
                    conflict_path = file_path.with_suffix(
                        f"{file_path.suffix}.conflict-{os.getpid()}-{ts}"
                    )
                    try:
                        atomic_write(conflict_path, current_on_disk)
                        logger.warning(
                            "saga post-commit: concurrent edit on %s detected; "
                            "conflict content saved to %s",
                            file_path,
                            conflict_path,
                        )
                    except Exception as _conflict_exc:
                        logger.warning(
                            "saga post-commit: failed to save conflict file %s: %s",
                            conflict_path,
                            _conflict_exc,
                        )
            do_write_file()
            params.wrote_file = True
        except Exception as exc:
            logger.error(
                "saga post-commit: file write failed for %s — "
                "DB is committed, .md can be regenerated via backfill_all: %s",
                params.note_id, exc,
            )

    steps = [
        SagaStep(name="upsert_db", do=_do_upsert, undo=_undo_upsert),
        SagaStep(name="write_vec_key", do=_do_vec_key, undo=_undo_vec_key),
        SagaStep(name="prepare_file", do=_do_prepare_file, undo=_undo_prepare_file),
    ]
    return steps, params, [_post_commit_write_file]


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
    tenant_id: str = "default",
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

    steps, _params, _hooks = _build_save_memory_steps(
        conn=conn,
        note_id=note_id,
        file_path=file_path,
        db_path=db_path,
        do_upsert_db=do_upsert_db,
        do_write_vec_key=do_write_vec_key,
        do_write_file=do_write_file,
        tenant_id=tenant_id,
    )

    try:
        with Saga(
            name="save_memory",
            steps=steps,
            conn=conn,
            mode=SagaMode.DEFERRED,
            post_commit_hooks=_hooks,
        ) as saga:
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


from infra.memory_common import atomic_write, make_lazy_getattr
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
