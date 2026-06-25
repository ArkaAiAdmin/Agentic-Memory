"""Fact-level temporal logic: supersession + contradiction detection.

T3 of the temporal-kg plan (schema v18).  When a new fact is inserted,
we check whether it contradicts any existing fact (same subject+predicate,
different object, overlapping event_time).  If so, the old fact is
marked as superseded by the new one — preserving history while keeping
the current state of the knowledge graph accurate.

The 4 main functions are:
  * ``_event_times_match`` — granularity-aware time equality
  * ``detect_fact_contradiction`` — pure check (no side effects)
  * ``supersede_fact`` — mark old_id as superseded by new_id
  * ``reconcile_fact_supersession`` — find + supersede candidates

``reconcile_fact_supersession`` is called from
``fact_extraction.index_facts_for_memory`` after each fact INSERT.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Standard column list returned by every fact query below.  Used by
# _row_to_dict to convert a plain tuple row into a dict regardless of
# the connection's row_factory (the caller's connection might not have
# sqlite3.Row set).
_FACT_COLUMNS: tuple[str, ...] = (
    "id",
    "subject",
    "predicate",
    "object",
    "event_time",
    "event_time_granularity",
    "valid_at",
    "invalid_at",
    "superseded_by",
    "supersedes",
    "invalidation_reason",
    "contradiction_score",
    "transaction_time",
)


def _row_to_dict(row, columns=None) -> dict:
    """Convert a sqlite row (Row or tuple) to a dict by column name.

    Avoids the caller needing to set ``row_factory = sqlite3.Row`` on
    their connection.  Works with both Row and tuple inputs.
    """
    if columns is None:
        columns = _FACT_COLUMNS
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(zip(columns, row))


__all__ = [
    "detect_fact_contradiction",
    "supersede_fact",
    "invalidate_fact",
    "reconcile_fact_supersession",
    "invalidate_stale_facts",
    "query_facts_at_time",
    "query_fact_supersession_chain",
    "query_facts_changed_since",
    "audit_fact_temporal_event",
]


# Granularity ordering for time comparison.  Higher = more precise.
_GRANULARITY_ORDER = {"year": 1, "month": 2, "day": 3}


def _datetime(event_time: float) -> datetime:
    """Convert epoch to UTC datetime."""
    return datetime.fromtimestamp(event_time, tz=timezone.utc)


def _event_times_match(
    t1: "float | None",
    g1: "str | None",
    t2: "float | None",
    g2: "str | None",
) -> bool:
    """Return True if two event times match within their granularities.

    Rules:
      * If either time is ``None`` or granularity is ``"unknown"``,
        always match — an unknown event time is treated as "always true"
        and overlaps everything.
      * Otherwise, compare at the LESS precise of the two granularities
        (a year-precision fact matches a day-precision fact only if the
        years match).

    Examples:
      * (2024-03-15, day) vs (2024-03-20, day) → False (different days)
      * (2024-03-15, day) vs (2024-03-01, month) → True (less precise
        is month; both are March 2024)
      * (2024-03-15, day) vs (2024-01-01, year) → True (less precise
        is year; both are 2024)
      * (2024-03-15, day) vs (2025-03-15, day) → False (different years)
    """
    if t1 is None or t2 is None:
        return True
    if g1 == "unknown" or g2 == "unknown" or g1 is None or g2 is None:
        return True
    d1 = _datetime(t1)
    d2 = _datetime(t2)
    precision = min(_GRANULARITY_ORDER.get(g1, 0), _GRANULARITY_ORDER.get(g2, 0))
    if precision <= 1:  # year or unknown
        return d1.year == d2.year
    if precision == 2:  # month
        return d1.year == d2.year and d1.month == d2.month
    # day
    return d1.year == d2.year and d1.month == d2.month and d1.day == d2.day


def detect_fact_contradiction(
    subj_a: str,
    pred_a: str,
    obj_a: str,
    event_time_a: "float | None",
    granularity_a: "str | None",
    subj_b: str,
    pred_b: str,
    obj_b: str,
    event_time_b: "float | None",
    granularity_b: "str | None",
) -> bool:
    """Return True if two facts contradict.

    Two facts contradict iff all of:
      1. Same subject (case-insensitive)
      2. Same predicate
      3. Different object (case-insensitive)
      4. Their event times match within granularity (or either is unknown)

    This is a pure check (no side effects).  Use ``supersede_fact`` to
    actually mark a contradiction in the DB.
    """
    if subj_a.lower() != subj_b.lower():
        return False
    if pred_a != pred_b:
        return False
    if obj_a.lower() == obj_b.lower():
        return False
    return _event_times_match(event_time_a, granularity_a, event_time_b, granularity_b)


def supersede_fact(
    conn: sqlite3.Connection,
    old_id: int,
    new_id: int,
    reason: str = "contradicted",
    score: float = 1.0,
) -> bool:
    """Mark old_id as superseded by new_id.

    Updates:
      * old.invalid_at — when the new fact took effect (new.event_time or now)
      * old.superseded_by — new_id
      * old.invalidation_reason — e.g. 'contradicted' / 'superseded' / 'expired' / 'manual'
      * old.contradiction_score — 1.0 for deterministic auto-detection, or
        the LLM-scored confidence in [0.0, 1.0] (T11) when
        ``MEMORY_TEMPORAL_KG_LLM=1`` is set
      * new.supersedes — old_id

    Returns True if old_id was updated.  Returns False if:
      * old_id == new_id (a fact can't supersede itself)
      * old_id is missing, locked, or already superseded
      * new_id is missing

    The ``invalid_at`` defaults to the new fact's event_time if set,
    otherwise to now.  This represents "the moment the new fact took
    effect" — which is the natural interpretation of "the old fact was
    no longer true at this time."

    The ``score`` parameter (T11) is the contradiction confidence.  The
    default 1.0 preserves the pre-T11 deterministic behavior.  Callers
    using LLM scoring pass the score from
    ``score_fact_contradiction_via_llm``.  The column stores the score
    verbatim so the audit trail shows whether the supersession was
    deterministic or LLM-scored (and at what confidence).
    """
    if old_id == new_id:
        return False
    old = conn.execute(
        "SELECT id, event_time, locked, superseded_by FROM kg_facts WHERE id = ?",
        (old_id,),
    ).fetchone()
    if not old:
        return False
    if old[2]:  # locked
        logger.debug("supersede_fact: fact %d is locked, skipping", old_id)
        return False
    if old[3] is not None:  # already superseded
        logger.debug(
            "supersede_fact: fact %d is already superseded, skipping",
            old_id,
        )
        return False
    new = conn.execute(
        "SELECT id, event_time FROM kg_facts WHERE id = ?",
        (new_id,),
    ).fetchone()
    if not new:
        return False
    # invalid_at = the new fact's event_time if known, else now
    invalid_at = new[1] if new[1] is not None else time.time()
    conn.execute(
        "UPDATE kg_facts SET invalid_at = ?, superseded_by = ?, "
        "invalidation_reason = ?, contradiction_score = ? WHERE id = ?",
        (invalid_at, new_id, reason, score, old_id),
    )
    conn.execute(
        "UPDATE kg_facts SET supersedes = ? WHERE id = ?",
        (old_id, new_id),
    )
    return True


def reconcile_fact_supersession(
    conn: sqlite3.Connection, new_fact_id: int
) -> list[int]:
    """Find facts that contradict new_fact_id and supersede them.

    Looks for facts with the same ``(subject, predicate)``, a different
    ``object``, and an overlapping ``event_time`` (per granularity).
    Marks each match as superseded by ``new_fact_id``.

    Returns a list of superseded fact IDs.  Idempotent: re-running on
    an already-reconciled fact is a no-op (candidates are already
    superseded or are the new fact itself).

    T11: when ``MEMORY_TEMPORAL_KG_LLM=1`` is set, the deterministic
    check is augmented with an LLM-scored confidence in [0.0, 1.0].
    The supersession is gated by ``contradiction_score_threshold`` (default
    0.7) so that low-confidence "contradictions" (e.g. a refinement
    like "is_a engineer" vs "is_a senior engineer") are NOT auto-superseded.
    The threshold is also a safety net for LLM errors: when the LLM
    fails to produce a parseable score, the deterministic 1.0 is used
    (preserves the pre-T11 behavior).

    Cost: O(candidates) deterministic + N LLM calls where N is the number
    of candidate pairs that pass the deterministic pre-filter.  In
    practice this is small (a few rows) for most S+P pairs.  The LLM
    calls are synchronous and add ~100-500ms each — set
    ``MEMORY_TEMPORAL_KG_LLM=0`` (the default) to opt out and avoid the
    LLM cost on every save.

    Called from ``index_facts_for_memory`` after each fact INSERT.
    """
    new = conn.execute(
        "SELECT subject, predicate, object, event_time, event_time_granularity "
        "FROM kg_facts WHERE id = ?",
        (new_fact_id,),
    ).fetchone()
    if not new:
        return []
    new_subj, new_pred, new_obj, new_event_time, new_granularity = new

    # T11: resolve LLM-scoring flag (off by default to avoid cost on every save)
    use_llm = _temporal_llm_scoring_enabled()
    threshold = _contradiction_score_threshold()

    # Find candidates: same S+P, different O, not already superseded, not self.
    candidates = conn.execute(
        "SELECT id, object, event_time, event_time_granularity "
        "FROM kg_facts "
        "WHERE subject = ? AND predicate = ? AND object != ? "
        "AND superseded_by IS NULL AND id != ?",
        (new_subj.lower(), new_pred, new_obj.lower(), new_fact_id),
    ).fetchall()

    superseded: list[int] = []
    for cand_id, cand_obj, cand_event_time, cand_granularity in candidates:
        if not detect_fact_contradiction(
            new_subj,
            new_pred,
            new_obj,
            new_event_time,
            new_granularity,
            new_subj,  # same subject (we filtered)
            new_pred,  # same predicate (we filtered)
            cand_obj,
            cand_event_time,
            cand_granularity,
        ):
            continue
        # Deterministic pre-filter passed.  If LLM scoring is enabled,
        # ask the model for a confidence score and gate the supersession
        # on the threshold.  On any LLM error, fall back to 1.0 (the
        # pre-T11 deterministic behavior).
        score = 1.0
        if use_llm:
            try:
                from llm_extraction import score_fact_contradiction_via_llm

                llm_score = score_fact_contradiction_via_llm(
                    new_subj, new_pred, new_obj, new_subj, new_pred, cand_obj
                )
                if llm_score is not None:
                    score = llm_score
                    # Threshold gate: low confidence = treat as refinement, not contradiction
                    if score < threshold:
                        logger.info(
                            "fact_temporal: LLM scored %s/%s/%s vs %s at %.2f "
                            "(below threshold %.2f); NOT superseding",
                            new_subj,
                            new_pred,
                            new_obj,
                            cand_obj,
                            score,
                            threshold,
                        )
                        continue
            except Exception as exc:
                # LLM unavailable or threw — keep deterministic 1.0 score
                logger.warning(
                    "fact_temporal: LLM scoring failed for %d->%d, "
                    "falling back to deterministic 1.0: %s",
                    new_fact_id,
                    cand_id,
                    exc,
                )
                score = 1.0
        if supersede_fact(conn, cand_id, new_fact_id, "contradicted", score=score):
            superseded.append(cand_id)
            logger.info(
                "fact_temporal: fact %d superseded by %d (%s/%s/%s -> %s) score=%.2f",
                cand_id,
                new_fact_id,
                new_subj,
                new_pred,
                cand_obj,
                new_obj,
                score,
            )
    return superseded


def _temporal_llm_scoring_enabled() -> bool:
    """T11: Resolve the ``MEMORY_TEMPORAL_KG_LLM`` kill switch.

    Returns True only when the flag is explicitly set to ``1``.  Off
    by default because the LLM call is synchronous and adds ~100-500ms
    per contradiction-candidate pair.
    """
    return os.environ.get("MEMORY_TEMPORAL_KG_LLM") == "1"


def _contradiction_score_threshold() -> float:
    """T11: Resolve the ``MEMORY_TEMPORAL_KG_LLM_THRESHOLD`` setting.

    Facts with an LLM contradiction score below this threshold are
    treated as refinements, not contradictions, and are NOT auto-superseded.
    Default 0.7.
    """
    try:
        v = os.environ.get("MEMORY_TEMPORAL_KG_LLM_THRESHOLD")
        if v:
            return float(v)
    except ValueError:
        pass
    return 0.7


# ---------------------------------------------------------------------------
# T5: Memory-update handling (invalidate stale facts on edit)
# ---------------------------------------------------------------------------
#
# When a memory is edited, the new content may no longer contain facts
# that were extracted from the previous version.  These stale facts
# should be marked as no-longer-valid (invalid_at set, no replacement)
# so they don't pollute "current state" queries.
#
# Two functions:
#   * invalidate_fact(fact_id, reason)  — mark a single fact stale
#   * invalidate_stale_facts(conn, memory_id, new_fact_keys)
#                                          — diff old vs new, invalidate
#                                            all old facts not in new
# ---------------------------------------------------------------------------


def invalidate_fact(
    conn: sqlite3.Connection,
    fact_id: int,
    reason: str = "manual",
    invalid_at: "float | None" = None,
) -> bool:
    """Mark a fact as no-longer-valid (no replacement, just expired).

    Sets ``invalid_at`` and ``invalidation_reason`` on the fact.  Does
    NOT set ``superseded_by`` because there is no replacement fact —
    this is for facts the user removed from a memory via edit.

    Use this for facts that were true when they were written but are
    no longer asserted in the latest version of the source memory.
    Compare with ``supersede_fact`` which is for contradictions (a
    NEW fact replaced the OLD one).

    Returns True if the fact was updated.  Returns False if:
      * the fact doesn't exist
      * the fact is locked
      * the fact is already invalidated (invalid_at IS NOT NULL)
    """
    if invalid_at is None:
        invalid_at = time.time()
    fact = conn.execute(
        "SELECT id, locked, invalid_at FROM kg_facts WHERE id = ?",
        (fact_id,),
    ).fetchone()
    if not fact:
        return False
    if fact[1]:  # locked
        logger.debug("invalidate_fact: fact %d is locked, skipping", fact_id)
        return False
    if fact[2] is not None:  # already invalidated
        logger.debug(
            "invalidate_fact: fact %d is already invalidated, skipping",
            fact_id,
        )
        return False
    conn.execute(
        "UPDATE kg_facts SET invalid_at = ?, invalidation_reason = ? WHERE id = ?",
        (invalid_at, reason, fact_id),
    )
    return True


def invalidate_stale_facts(
    conn: sqlite3.Connection,
    memory_id: str,
    new_fact_keys: "set[tuple[str, str, str]]",
) -> list[int]:
    """T5.1 + T5.2 + T5.3: find facts from `memory_id` that are no longer
    in the new content and mark them invalidated.

    Called from ``index_facts_for_memory`` AFTER all new facts have
    been extracted and upserted.  The diff is:
      * OLD = facts where source_memory == memory_id (i.e., facts
        currently attributed to this memory)
      * NEW = new_fact_keys (the set of S+P+O that the new content
        contains — typically the result of ``extract_facts(content)``)

    For each fact in OLD that's NOT in NEW, call ``invalidate_fact``
    with reason='manual' (the user removed the content via edit).

    Note: this is implicit-update detection.  There's no need to
    distinguish INSERT vs UPDATE — for an INSERT, OLD is empty, so
    no invalidation happens.  For an UPDATE where the user added
    facts, NEW includes the new S+P+O, so the old (now re-asserted)
    facts are preserved.  For an UPDATE where the user removed facts,
    the missing S+P+O are invalidated.

    Returns a list of invalidated fact IDs.
    """
    rows = conn.execute(
        "SELECT id, subject, predicate, object "
        "FROM kg_facts WHERE source_memory = ? AND invalid_at IS NULL",
        (memory_id,),
    ).fetchall()
    invalidated: list[int] = []
    for row in rows:
        fid, subj, pred, obj = row
        key = (subj, pred, obj)
        if key not in new_fact_keys:
            if invalidate_fact(conn, fid, reason="manual"):
                invalidated.append(fid)
                logger.info(
                    "fact_temporal: fact %d invalidated (memory %s no "
                    "longer contains %s/%s/%s)",
                    fid,
                    memory_id,
                    subj,
                    pred,
                    obj,
                )
    return invalidated


# T5.4: audit log helper
#
# We reuse the existing memory_audit_log table (no new migration needed)
# with tool='kg_fact_temporal' and event details in the args column as
# JSON.  This is queryable via the existing memory_audit_query tool:
#
#   memory_audit_query(tool_name='kg_fact_temporal', since_ts=...,
#                      until_ts=..., limit=...)
#
# Schema of args JSON: {
#   "event": "invalidate" | "supersede",
#   "fact_id": <int>,
#   "reason": "manual" | "contradicted" | ...,
#   "memory_id": "..." (for invalidate events),
#   "new_fact_id": <int> (for supersede events),
#   "subject": "...", "predicate": "...", "object": "..."
# }


def audit_fact_temporal_event(
    conn: sqlite3.Connection,
    event: str,
    fact_id: int,
    reason: str,
    subject: str,
    predicate: str,
    obj: str,
    memory_id: "str | None" = None,
    new_fact_id: "int | None" = None,
) -> None:
    """T5.4: write a fact-level temporal event to the audit log.

    Reuses ``memory_audit_log`` with ``tool='kg_fact_temporal'``.  The
    event details are serialized as JSON in the ``args`` column.

    Best-effort: errors are logged but never raised (the call site is
    in the save hot path).
    """
    import json

    payload: dict = {
        "event": event,
        "fact_id": fact_id,
        "reason": reason,
        "subject": subject,
        "predicate": predicate,
        "object": obj,
    }
    if memory_id is not None:
        payload["memory_id"] = memory_id
    if new_fact_id is not None:
        payload["new_fact_id"] = new_fact_id
    try:
        conn.execute(
            "INSERT INTO memory_audit_log "
            "(ts, tool, args, results_count, top1_id, latency_ms, error, request_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                "kg_fact_temporal",
                json.dumps(payload),
                1,
                None,
                0.0,
                None,
                None,
            ),
        )
    except Exception as e:
        logger.warning("audit_fact_temporal_event: failed to write audit log: %s", e)


# ---------------------------------------------------------------------------
# T4.1-T4.4: Time-aware query layer
# ---------------------------------------------------------------------------
#
# Three query functions for the temporal KG:
#   * query_facts_at_time(t)        — facts valid at time t
#   * query_fact_supersession_chain(id) — walk the superseded_by chain
#   * query_facts_changed_since(t)  — facts added or invalidated since t
#
# Plus _temporal_fact_clause(as_of_epoch) — the SQL fragment the queries
# build on (mirrors _temporal_edge_clause in knowledge_graph.py but for
# kg_facts and using REAL/epoch instead of TEXT/datetime).
# ---------------------------------------------------------------------------


def _temporal_fact_clause(as_of: "float | None") -> "tuple[str, list]":
    """T4.1: Build the temporal filter SQL fragment + params for kg_facts
    queries. Returns ``(" AND ...", params)`` so callers can splat the
    clause into a larger query string.

    Mirrors :func:`knowledge_graph._temporal_edge_clause` but for
    ``kg_facts`` (which stores ``valid_at`` / ``invalid_at`` as REAL
    epoch seconds, not TEXT datetimes like ``kg_edges``).

    The as_of parameter is an epoch (float).  ``None`` means "current
    state" — only facts that are still valid (invalid_at IS NULL).

    SQL semantics:
      * ``valid_at IS NULL`` is treated as "always valid" (the fact
        has no known start time, so it was always true).
      * ``invalid_at IS NULL`` is treated as "still valid".
      * Otherwise, the fact is valid iff
        ``valid_at <= as_of AND invalid_at >= as_of``.
    """
    if as_of is not None:
        return (
            " AND (f.valid_at IS NULL OR f.valid_at <= ?) "
            "AND (f.invalid_at IS NULL OR f.invalid_at >= ?)",
            [as_of, as_of],
        )
    return " AND f.invalid_at IS NULL", []


def query_facts_at_time(
    conn: sqlite3.Connection,
    as_of: float,
    *,
    query: "str | None" = None,
    limit: int = 100,
) -> list[dict]:
    """T4.2: return facts valid at the given epoch.

    A fact is "valid at as_of" iff:
      * ``valid_at`` is NULL or <= as_of
      * ``invalid_at`` is NULL or >= as_of

    Args:
      conn: SQLite connection.
      as_of: epoch seconds (the time to query).
      query: optional case-insensitive substring filter against
        subject / predicate / object.  Default: no filter.
      limit: max rows to return.  Default: 100.

    Returns:
      list of dicts with the full fact row (id, S/P/O, event_time,
      valid_at, invalid_at, superseded_by, etc.).
    """
    clause, params = _temporal_fact_clause(as_of)
    where = f"WHERE 1=1{clause}"
    if query:
        where += " AND (f.subject LIKE ? OR f.predicate LIKE ? OR f.object LIKE ?)"
        like = f"%{query.lower()}%"
        params.extend([like, like, like])
    rows = conn.execute(
        f"""
        SELECT f.id, f.subject, f.predicate, f.object,
               f.event_time, f.event_time_granularity,
               f.valid_at, f.invalid_at,
               f.superseded_by, f.supersedes,
               f.invalidation_reason, f.contradiction_score,
               f.transaction_time
        FROM kg_facts AS f
        {where}
        ORDER BY f.transaction_time DESC
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    return [_row_to_dict(r, _FACT_COLUMNS) for r in rows]


def query_fact_supersession_chain(conn: sqlite3.Connection, fact_id: int) -> list[dict]:
    """T4.3: walk the ``superseded_by`` chain starting from ``fact_id``.

    Returns facts in chronological order (oldest first), so the result
    tells the full history: ``[original, ...replacements..., latest]``.
    The starting ``fact_id`` is included even if it's the head of the
    chain (no superseded_by).

    Bounded by ``max_chain_depth`` to prevent infinite loops on
    pathological data (e.g., a fact that points to itself).

    Args:
      conn: SQLite connection.
      fact_id: starting fact id (the head, or any link in the chain).

    Returns:
      list of dicts (the full chain), oldest first.  Empty if fact_id
      doesn't exist.
    """
    max_chain_depth = 100
    chain: list[dict] = []
    current_id: "int | None" = fact_id
    visited: set[int] = set()
    while current_id is not None and current_id not in visited:
        if len(chain) >= max_chain_depth:
            logger.warning(
                "fact_temporal: chain from fact %d exceeded max depth %d",
                fact_id,
                max_chain_depth,
            )
            break
        visited.add(current_id)
        row = conn.execute(
            "SELECT id, subject, predicate, object, "
            "       event_time, event_time_granularity, "
            "       valid_at, invalid_at, "
            "       superseded_by, supersedes, "
            "       invalidation_reason, contradiction_score, "
            "       transaction_time "
            "FROM kg_facts WHERE id = ?",
            (current_id,),
        ).fetchone()
        if not row:
            break
        fact = _row_to_dict(row)
        chain.append(fact)
        current_id = fact.get("superseded_by")
        if current_id is not None and not isinstance(current_id, int):
            current_id = None  # Defensive: should be int or None
    # Walker already visits oldest first (it follows superseded_by from
    # the starting fact to its replacement, then to the next replacement,
    # etc.).  No reversal needed.
    return chain


def query_facts_changed_since(
    conn: sqlite3.Connection,
    since_ts: float,
    *,
    limit: int = 100,
) -> list[dict]:
    """T4.4: return facts that changed (inserted or invalidated) since
    ``since_ts``.

    "Changed" includes:
      * newly inserted (transaction_time > since_ts)
      * newly invalidated (invalid_at > since_ts, set by a supersession)

    Useful for "what changed since yesterday?" or "show me the last
    week's knowledge-graph mutations".

    Args:
      conn: SQLite connection.
      since_ts: epoch seconds (the lower bound).
      limit: max rows to return.  Default: 100.

    Returns:
      list of dicts, ordered by most-recent change first
      (COALESCE(invalid_at, transaction_time) DESC).
    """
    rows = conn.execute(
        """
        SELECT id, subject, predicate, object,
               event_time, event_time_granularity,
               valid_at, invalid_at,
               superseded_by, supersedes,
               invalidation_reason, contradiction_score,
               transaction_time
        FROM kg_facts
        WHERE transaction_time > ? OR (invalid_at IS NOT NULL AND invalid_at > ?)
        ORDER BY COALESCE(invalid_at, transaction_time) DESC
        LIMIT ?
        """,
        (since_ts, since_ts, limit),
    ).fetchall()
    return [_row_to_dict(r, _FACT_COLUMNS) for r in rows]
