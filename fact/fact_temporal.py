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

import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


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
    "_mark_fact_superseded",
    "invalidate_fact",
    "reconcile_fact_supersession",
    "propagate_entity_supersession",
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


def _propagate_entailment_invalidation(conn: AnyConnection, superseded_fact_id: int) -> None:
    """A2.3: invalidate derived facts whose source chain includes a
    superseded fact.

    Scans ``entailment_chains`` for rows where ``source_fact_ids`` (JSON
    array) contains ``superseded_fact_id`` and ``valid = 1``.  For each
    matching row, sets ``is_entailed = 0`` on the derived fact and
    ``valid = 0`` on the chain entry.

    Best-effort: errors are logged but never raised.
    """
    try:
        rows = conn.execute(
            "SELECT ec.id, ec.source_fact_ids, ec.derived_fact_id "
            "FROM entailment_chains ec, json_each(ec.source_fact_ids) "
            "WHERE ec.valid = 1 AND json_each.value = ?",
            (superseded_fact_id,),
        ).fetchall()
    except Exception as exc:
        logger.debug("entailment_invalidation: chain lookup failed: %s", exc)
        return
    for chain_id, source_ids_json, derived_fid in rows:
        try:
            source_ids = json.loads(source_ids_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if superseded_fact_id not in source_ids:
            continue
        try:
            conn.execute(
                "UPDATE kg_facts SET is_entailed = 0 WHERE id = ?",
                (derived_fid,),
            )
            conn.execute(
                "UPDATE entailment_chains SET valid = 0 WHERE id = ?",
                (chain_id,),
            )
        except Exception as exc:
            logger.debug(
                "entailment_invalidation: failed for chain %d fact %d: %s",
                chain_id, derived_fid, exc,
            )


def supersede_fact(
    conn: AnyConnection,
    old_id: int,
    new_id: int,
    reason: str = "contradicted",
    score: float = 1.0,
    winner_valid_at: "float | None" = None,
) -> bool:
    """Mark old_id as superseded by new_id.

    Updates:
      * old.invalid_at — when the new fact took effect (new.event_time or now)
      * old.superseded_by — new_id
      * old.invalidation_reason — e.g. 'contradicted' / 'superseded' / 'expired' / 'manual'
      * old.contradiction_score — 1.0 for deterministic auto-detection, or
        the LLM-scored confidence in [0.0, 1.0] (T11) when
        ``MEMORY_TEMPORAL_KG_LLM=1`` is set
      * new.valid_at — set to ``winner_valid_at`` if provided and new.valid_at IS NULL
      * new.supersedes — old_id

    Returns True if old_id was updated.  Returns False if:
      * old_id == new_id (a fact can't supersede itself)
      * old_id is missing, locked, or already superseded
      * new_id is missing

    After superseding, any derived facts in ``entailment_chains`` that
    depend on the old fact are marked ``is_entailed=0`` and their chain
    entries are marked ``valid=0``.
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
        "SELECT id, event_time, valid_at FROM kg_facts WHERE id = ?",
        (new_id,),
    ).fetchone()
    if not new:
        return False
    # Sprint 2: set valid_at on the winner when resolving a contradiction,
    # so subsequent supersessions have a stable "took effect" anchor.
    if new[2] is None:
        conn.execute(
            "UPDATE kg_facts SET valid_at = COALESCE(?, transaction_time) WHERE id = ?",
            (winner_valid_at, new_id),
        )
    # invalid_at = the new fact's event_time if known, else now
    invalid_at = new[1] if new[1] is not None else time.time()
    conn.execute(
        "UPDATE kg_facts SET invalid_at = ?, superseded_by = ?, "
        "invalidation_reason = ?, contradiction_score = ? WHERE id = ?",
        (invalid_at, new_id, reason, score, old_id),
    )
    try:
        _ = conn.execute("SELECT 1 FROM belief_assertions LIMIT 1")
        conn.execute(
            "UPDATE belief_assertions SET belief_status = 'deprecated' "
            "WHERE fact_id = ? AND belief_status = 'active'",
            (old_id,),
        )
    except Exception:
        pass
    conn.execute(
        "UPDATE kg_facts SET supersedes = ? WHERE id = ?",
        (old_id, new_id),
    )
    _propagate_entailment_invalidation(conn, old_id)
    return True


def _mark_fact_superseded(
    conn: AnyConnection,
    fact_id: int,
    winner_id: int,
    reason: str = "contradicted",
    score: float = 1.0,
) -> bool:
    """Mark ``fact_id`` as superseded by ``winner_id`` without touching
    the winner's ``supersedes`` column.

    This is the reverse-direction counterpart of ``supersede_fact``.
    When the existing fact has a later event_time than the newly
    inserted one, the new fact is the loser.  We mark it superseded
    here without overwriting the winner's existing ``supersedes``
    pointer (the winner may already have superseded a prior fact).
    """
    if fact_id == winner_id:
        return False
    winner = conn.execute(
        "SELECT id, event_time, locked FROM kg_facts WHERE id = ?",
        (winner_id,),
    ).fetchone()
    if not winner:
        return False
    if winner[2]:  # locked
        logger.debug(
            "_mark_fact_superseded: winner %d is locked, skipping", winner_id
        )
        return False
    fact = conn.execute(
        "SELECT id, locked, superseded_by FROM kg_facts WHERE id = ?",
        (fact_id,),
    ).fetchone()
    if not fact:
        return False
    if fact[1]:  # locked
        logger.debug(
            "_mark_fact_superseded: fact %d is locked, skipping", fact_id
        )
        return False
    if fact[2] is not None:  # already superseded
        logger.debug(
            "_mark_fact_superseded: fact %d is already superseded, skipping",
            fact_id,
        )
        return False
    # invalid_at = the winner's event_time if known, else now
    invalid_at = winner[1] if winner[1] is not None else time.time()
    conn.execute(
        "UPDATE kg_facts SET invalid_at = ?, superseded_by = ?, "
        "invalidation_reason = ?, contradiction_score = ? WHERE id = ?",
        (invalid_at, winner_id, reason, score, fact_id),
    )
    conn.execute(
        "UPDATE kg_facts SET valid_at = COALESCE(valid_at, ?, transaction_time) WHERE id = ?",
        (winner[1] or time.time(), winner_id),
    )
    try:
        _ = conn.execute("SELECT 1 FROM belief_assertions LIMIT 1")
        conn.execute(
            "UPDATE belief_assertions SET belief_status = 'deprecated' "
            "WHERE fact_id = ? AND belief_status = 'active'",
            (fact_id,),
        )
    except Exception:
        pass
    _propagate_entailment_invalidation(conn, fact_id)
    return True


def reconcile_fact_supersession(
    conn: AnyConnection, new_fact_id: int
) -> list[int]:
    """Find facts that contradict new_fact_id and supersede them.

    Looks for facts with the same ``(subject, predicate)``, a different
    ``object``, and an overlapping ``event_time`` (per granularity).
    The fact with the **later** ``event_time`` (or ``valid_at``) always
    wins — making contradiction resolution order-independent regardless
    of which fact was inserted first.

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
        "SELECT subject, predicate, object, event_time, event_time_granularity, "
        "       valid_at "
        "FROM kg_facts WHERE id = ?",
        (new_fact_id,),
    ).fetchone()
    if not new:
        return []
    new_subj, new_pred, new_obj, new_event_time, new_granularity, new_valid_at = new

    # T11: resolve LLM-scoring flag (off by default to avoid cost on every save)
    use_llm = _temporal_llm_scoring_enabled()
    threshold = _contradiction_score_threshold()

    # Find candidates: same S+P, different O, not already superseded, not self.
    candidates = conn.execute(
        "SELECT id, object, event_time, event_time_granularity, valid_at "
        "FROM kg_facts "
        "WHERE subject = ? AND predicate = ? AND object != ? "
        "AND superseded_by IS NULL AND id != ?",
        (new_subj.lower(), new_pred, new_obj.lower(), new_fact_id),
    ).fetchall()

    superseded: list[int] = []
    for (
        cand_id,
        cand_obj,
        cand_event_time,
        cand_granularity,
        cand_valid_at,
    ) in candidates:
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
        # Sprint 2: order-independent winner selection.
        # The fact with the later event_time (or valid_at fallback) always
        # wins.  If neither has a time, the new fact wins by default.
        def _fact_time(evt, vat):
            if evt is not None:
                return ("event_time", evt)
            if vat is not None:
                return ("valid_at", vat)
            return ("none", None)

        new_time_type, new_time_val = _fact_time(new_event_time, new_valid_at)
        cand_time_type, cand_time_val = _fact_time(cand_event_time, cand_valid_at)

        new_wins = True
        if new_time_val is not None and cand_time_val is not None:
            new_wins = new_time_val >= cand_time_val
        elif cand_time_val is not None and new_time_val is None:
            new_wins = False  # candidate has a time, new doesn't

        if not new_wins:
            # Existing fact is chronologically later; mark the new fact
            # as superseded without touching the winner's supersedes column.
            if _mark_fact_superseded(
                conn, new_fact_id, cand_id, "contradicted", score=1.0
            ):
                superseded.append(new_fact_id)
                logger.info(
                    "fact_temporal: new fact %d superseded by existing %d "
                    "(%s/%s/%s -> %s) via order-independent check",
                    new_fact_id,
                    cand_id,
                    new_subj,
                    new_pred,
                    new_obj,
                    cand_obj,
                )
            # New fact is now superseded; stop processing further candidates.
            break

        # Deterministic pre-filter passed with new fact winning.
        # If LLM scoring is enabled, ask the model for a confidence score
        # and gate the supersession on the threshold.  On any LLM error,
        # fall back to 1.0 (the pre-T11 deterministic behavior).
        score = 1.0
        if use_llm:
            try:
                from config import get_config

                llm_tier = get_config().temporal_kg_llm_tier
                from llm_extraction import score_fact_contradiction_via_llm

                llm_score = score_fact_contradiction_via_llm(
                    new_subj, new_pred, new_obj, new_subj, new_pred, cand_obj,
                    tier=llm_tier,
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
        winner_valid_at = new_event_time if new_event_time is not None else cand_event_time
        if supersede_fact(
            conn,
            cand_id,
            new_fact_id,
            "contradicted",
            score=score,
            winner_valid_at=winner_valid_at,
        ):
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
    # Sprint 4: cascade supersession to sibling facts on the same entity.
    # De-duplicate by tracking already-propagated fact IDs across all
    # superseded facts to avoid double-processing.
    all_propagated: list[int] = []
    _already_propagated: set[int] = set()
    for old_id in superseded:
        if old_id in _already_propagated:
            continue
        propagated = propagate_entity_supersession(conn, old_id, new_fact_id)
        all_propagated.extend(propagated)
        _already_propagated.update(propagated)
    return superseded


def propagate_entity_supersession(
    conn: AnyConnection,
    superseded_fact_id: int,
    new_fact_id: int,
) -> list[int]:
    """Sprint 4: cascade supersession to other facts sharing the same entity.

    When a fact is superseded, other active facts that share the same
    ``subject_entity_id`` or ``object_entity_id`` and have a **different**
    predicate may now be stale.  This function scans those facts and
    marks them as superseded (reason="propagated") if their event_time
    overlaps with the new fact's event_time.

    Returns a list of propagated fact IDs.  Returns [] if the
    superseded fact has no entity ids or there are no matching
    candidates.
    """
    row = conn.execute(
        "SELECT subject, predicate, object, event_time, event_time_granularity, "
        "       subject_entity_id, object_entity_id "
        "FROM kg_facts WHERE id = ?",
        (superseded_fact_id,),
    ).fetchone()
    if not row:
        return []
    (
        _subj,
        _pred,
        _obj,
        evt_time,
        evt_gran,
        subj_ent,
        obj_ent,
    ) = row

    new_row = conn.execute(
        "SELECT event_time, event_time_granularity FROM kg_facts WHERE id = ?",
        (new_fact_id,),
    ).fetchone()
    if not new_row:
        return []
    new_evt_time, new_evt_gran = new_row

    if new_evt_time is None:
        return []

    entity_ids = {e for e in (subj_ent, obj_ent) if e is not None}
    if not entity_ids:
        return []

    propagated: list[int] = []
    for ent_id in entity_ids:
        candidates = conn.execute(
            "SELECT id, predicate, subject, object, "
            "       event_time, event_time_granularity, "
            "       valid_at, invalid_at "
            "FROM kg_facts "
            "WHERE (subject_entity_id = ? OR object_entity_id = ?) "
            "AND id != ? AND id != ? "
            "AND superseded_by IS NULL AND invalid_at IS NULL "
            "AND predicate != ?",
            (ent_id, ent_id, superseded_fact_id, new_fact_id, _pred),
        ).fetchall()
        for (
            c_id,
            c_pred,
            c_subj,
            c_obj,
            c_evt,
            c_gran,
            _c_valid_at,
            _c_invalid_at,
        ) in candidates:
            # Sprint 4 propagation uses time overlap only (no S+P+O check —
            # propagation is entity-driven, not contradiction-driven).
            if not _event_times_match(c_evt, c_gran, new_evt_time, new_evt_gran):
                continue
            if supersede_fact(
                conn,
                c_id,
                new_fact_id,
                "propagated",
                score=1.0,
                winner_valid_at=new_evt_time,
            ):
                propagated.append(c_id)
                logger.info(
                    "fact_temporal: propagated supersession from fact %d "
                    "to fact %d (entity %d, pred %s -> %s)",
                    superseded_fact_id,
                    c_id,
                    ent_id,
                    _pred,
                    c_pred,
                )
    return propagated


def _temporal_llm_scoring_enabled() -> bool:
    """T11 Sprint 3: Resolve the feature_temporal_kg_llm flag.

    Returns True by default (LLM scoring ON).  Use
    ``MEMORY_TEMPORAL_KG_LLM=0`` or set ``feature_temporal_kg_llm = false``
    in memory.toml to disable and avoid the ~100-500ms LLM latency on
    every fact save.
    """
    from config import get_config

    return bool(get_config().feature_temporal_kg_llm)


def _contradiction_score_threshold() -> float:
    """T11 Sprint 3: Resolve the threshold for LLM contradiction scores.

    The threshold is tier-aware:
      * "light" → 0.5 (more permissive; fewer misses on subtle contradictions)
      * "heavy" → 0.7 (conservative; only high-confidence supersessions)
      * fallback → 0.5
    """
    from config import get_config

    cfg = get_config()
    tier = cfg.temporal_kg_llm_tier
    if tier == "heavy":
        return 0.7
    return 0.5


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
    conn: AnyConnection,
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
    conn: AnyConnection,
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
    conn: AnyConnection,
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
            "AND (f.invalid_at IS NULL OR f.invalid_at = '' OR f.invalid_at >= ?) "
            "AND f.superseded_by IS NULL ",
            [as_of, as_of],
        )
    return " AND f.invalid_at IS NULL AND f.superseded_by IS NULL", []


def query_facts_at_time(
    conn: AnyConnection,
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


def query_fact_supersession_chain(conn: AnyConnection, fact_id: int) -> list[dict]:
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
    conn: AnyConnection,
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
