"""Phase 13: Postprocessing passes.

Applied in a FIXED, explicit order:
  13.1 Safety demoting — strip untrusted content
  13.2 Quality gates — drop low-quality results
  13.3 User profiling — rerank per stored profile
  13.4 Strong match boost — hoist high-confidence hit
  13.5 Save hint floater — surface very-recent saves

Every pass mutates ``state.result_items``, ``state.output``, and
``state.results_to_display`` in place.  Do NOT reorder without
updating the documented order contract.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from typing import TYPE_CHECKING

from infra.error_counter import increment as _phase_inc
from search.query_parser import _escape_fts_query

if TYPE_CHECKING:
    from search.state import PipelineState

logger = logging.getLogger(__name__)


def _format_search_results(
    results_to_display: list,
    query: str,
    rerank: bool,
    result_items: list,
    backlinks_map: dict,
) -> list[str]:
    """Build human-readable output lines from ranked results.

    T10 note: the "Related facts" section is no longer built here — it
    is appended in ``_build_search_result_envelope`` AFTER all post-Phase-10
    regeneration passes, so it survives the safety demoting, quality gates,
    user profiling, strong-match boost, and save-hint floater passes which
    each call ``_format_search_results`` without T10 context.
    """
    output = [
        f"Search results for: '{query}' (Re-ranked)"
        if rerank
        else f"Search results for: '{query}'"
    ]
    for i, r in enumerate(results_to_display, 1):
        (
            note_id,
            content,
            source_file,
            tags_json,
            created,
            rank,
            final_score,
            fitness_score,
            importance_val,
            pinned,
        ) = r[:10]
        metadata_json = r[11] if len(r) > 11 else None
        try:
            tags = json.loads(tags_json) if tags_json else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        tags_str = ", ".join(tags) if tags else "none"
        backlinks = backlinks_map.get(note_id, [])
        backlinks_str = (
            ", ".join((f"[[{b}]]" for b in backlinks)) if backlinks else "none"
        )
        score_info = (
            f"(Relevance: {final_score:.2f})" if rerank else f"(Rank: {-rank:.2f})"
        )
        if fitness_score is not None:
            score_info += f" | Fitness: {fitness_score:.2f} | Importance: {importance_val} | Pinned: {('yes' if pinned else 'no')}"
        summary_line = ""
        if metadata_json:
            try:
                meta = (
                    json.loads(metadata_json)
                    if isinstance(metadata_json, str)
                    else metadata_json
                )
                if meta and meta.get("auto_summary"):
                    summary_line = f"\n    Summary: {meta['auto_summary'][:300]}"
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        output.append(
            f"[{i}] {note_id} {score_info}\n    Source: memory/{source_file}\n    Tags: {tags_str}\n    Backlinks: {backlinks_str}\n    Created: {created}{summary_line}\n    Content:\n    {content.strip()}"
        )
    return output


def apply_safety_demoting(state: PipelineState) -> None:
    """Phase 13.1: apply injection-detection safety demoting to results."""
    try:
        import memory_injection

        _rtd_by_id = {r[0]: r for r in state.results_to_display}
        _demote_input = [
            {
                "id": item["id"],
                "content": _rtd_by_id.get(item["id"], (None, ""))[1] or "",
                "score": float(item.get("final_score") or 0.0),
            }
            for item in state.result_items
        ]
        _demoted = memory_injection.demote_results_by_injection(_demote_input)
        _id_to_idx = {item["id"]: i for i, item in enumerate(state.result_items)}
        _new_order = []
        for _d in _demoted:
            if _d["id"] in _id_to_idx:
                _new_order.append(_id_to_idx[_d["id"]])
        _seen = set(_new_order)
        for _i in range(len(state.result_items)):
            if _i not in _seen:
                _new_order.append(_i)
        state.result_items = [state.result_items[_i] for _i in _new_order]
        _new_output = [state.output[0]]
        for _old_idx in _new_order:
            _new_output.append(state.output[_old_idx + 1])
        state.output = _new_output
        state.results_to_display = [state.results_to_display[_i] for _i in _new_order]
    except Exception as e:
        _phase_inc("search.safety_demoting", e)
        logger.warning("safety_demoting failed: %s", e)


def apply_quality_gates(state: PipelineState) -> None:
    """Phase 13.2: drop low-quality results via the quality_gates filter.

    Only acts when ``quality_gates.QUALITY_GATES_ENABLED`` is true
    and the result set is non-empty.  When the filter removes any
    rows, the human-readable output is regenerated to match.

    All exceptions are swallowed — quality gates are advisory; a
    failure here must never block a successful search.
    """
    try:
        import quality_gates as qg

        if getattr(qg, "QUALITY_GATES_ENABLED", False) and state.result_items:
            state.result_items, qg_stats = qg.filter_results(state.result_items)
            if qg_stats.get("filtered", 0) > 0:
                kept_ids = {ri["id"] for ri in state.result_items}
                state.results_to_display[:] = [
                    r for r in state.results_to_display if r[0] in kept_ids
                ]
                state.output = _format_search_results(
                    state.results_to_display,
                    state.query,
                    state.rerank,
                    state.result_items,
                    state.backlinks_map,
                )
    except Exception as e:
        _phase_inc("search.quality_gates", e)
        logger.warning("quality_gates failed: %s", e)


def apply_user_profiling(state: PipelineState) -> None:
    """Phase 13.3: rerank result list per the user's stored profile.

    Only acts when ``user_profile.PROFILE_ENABLED`` is true and the
    result set is non-empty.  Pulls the active profile from the DB
    and reorders results to match.

    All exceptions are swallowed — personalization is a UX
    optimization; a failure here must never block a successful search.
    """
    try:
        import user_profile as up

        if getattr(up, "PROFILE_ENABLED", False) and state.result_items:
            profile = up.get_user_profile(db_path=str(state.db_path))
            state.result_items = up.personalize_results(state.result_items, profile=profile)
            _rtd_by_id = {r[0]: r for r in state.results_to_display}
            state.results_to_display[:] = [
                _rtd_by_id[ri["id"]] for ri in state.result_items if ri["id"] in _rtd_by_id
            ]
            state.output = _format_search_results(
                state.results_to_display,
                state.query,
                state.rerank,
                state.result_items,
                state.backlinks_map,
            )
    except Exception as e:
        _phase_inc("search.user_profiling", e)
        logger.warning("user_profiling failed: %s", e)


def apply_strong_match_boost(state: PipelineState) -> None:
    """Phase 13.4: hoist a high-confidence match to position 0.

    If any row's FTS5 rank converts (via the standard ``1/(1+exp(r))``
    sigmoid) to a confidence >= 0.95, that row is moved to the top of
    both ``result_items`` and ``results_to_display``, and the
    human-readable ``output`` is regenerated to reflect the new
    order.

    No-op if no row crosses the 0.95 threshold or if the strong row
    is already at index 0.  All exceptions are swallowed because this
    pass is a UX optimization — a failure here must never break a
    successful search.
    """
    try:
        strong_id = None
        for r in state.results_to_display or []:
            try:
                rv = float(r[5]) if len(r) > 5 else 0.0
            except (TypeError, ValueError):
                rv = 0.0
            rv = max(-60.0, min(60.0, rv))
            bm = 1.0 / (1.0 + math.exp(rv))
            if bm >= 0.95:
                strong_id = r[0]
                break
        if strong_id is None or not state.result_items:
            return
        hit_idx = next(
            (i for i, ri in enumerate(state.result_items) if ri.get("id") == strong_id),
            None,
        )
        if hit_idx is None or hit_idx == 0:
            return
        strong = state.result_items.pop(hit_idx)
        state.result_items.insert(0, strong)
        disp_idx = next(
            (i for i, rr in enumerate(state.results_to_display) if rr[0] == strong_id),
            None,
        )
        if disp_idx is not None:
            disp_row = state.results_to_display.pop(disp_idx)
            state.results_to_display.insert(0, disp_row)
        try:
            state.output = _format_search_results(
                state.results_to_display,
                state.query,
                state.rerank,
                state.result_items,
                state.backlinks_map,
            )
        except Exception as _oe:
            logger.warning("_format_search_results failed: %s", _oe)
    except Exception as e:
        _phase_inc("search.strong_match_boost", e)
        logger.warning("_apply_strong_match_boost failed: %s", e)


def apply_save_hint_floater(state: PipelineState) -> None:
    """Phase 13.5: surface a very-recent save even if FTS hasn't seen it.

    Reads the ``recent_save_hint`` table (set by the post-save hook
    on a prior save) and, if the hinted note is in the FTS5 index but
    not yet in the result set, prepends it to the result list.

    The hint covers the small window between a save committing and
    the FTS5 trigger / search-cache catching up.  Without this pass,
    a save-then-immediate-search call could return a result set
    missing the just-saved note.  All exceptions are swallowed —
    a missing or stale hint is non-fatal.
    """
    try:
        from recent_save_hint import recent_save_for

        hint = recent_save_for(str(state.db_path))
    except (ImportError, AttributeError) as _e:
        _phase_inc("search.save_hint_floater", _e)
        return
    if hint is None:
        return
    hint_id, _hint_ts = hint
    try:
        escaped_match = f'"{_escape_fts_query(hint_id)}"'
        hint_in_fts = (
            state.db.execute(
                "SELECT 1 FROM memories_fts fts "
                "JOIN memories m ON m.rowid = fts.rowid "
                "WHERE memories_fts MATCH ? AND m.id = ? AND m.deleted_at IS NULL",
                (escaped_match, hint_id),
            ).fetchone()
            is not None
        )
    except sqlite3.Error as _fts_e:
        _phase_inc("search.save_hint_floater", _fts_e)
        return
    if not (hint_in_fts and state.result_items):
        return
    if any(ri.get("id") == hint_id for ri in state.result_items):
        return
    try:
        floater_row = state.db.execute(
            "SELECT id, content, source_file, tags, created_at, "
            "fitness_score, importance, pinned, last_accessed, "
            "metadata, valid_to, 0 AS rank, 0.5 AS final_score "
            "FROM tenant_memories WHERE id = ? AND deleted_at IS NULL",
            (hint_id,),
        ).fetchone()
    except sqlite3.Error as _fl_e:
        _phase_inc("search.save_hint_floater", _fl_e)
        return
    if floater_row is None:
        return
    floater_item = {
        "id": floater_row[0],
        "source_file": floater_row[2],
        "tags": json.loads(floater_row[3]) if floater_row[3] else [],
        "created": floater_row[4],
        "rank": 0,
        "final_score": 1.0,
        "fitness_score": floater_row[5] or 0.5,
        "importance": floater_row[6] or 3,
        "pinned": floater_row[7] or 0,
        "backlinks": [],
        "summary": None,
    }
    state.result_items.insert(0, floater_item)
    # Build floater tuple in the canonical results_to_display column
    # order: (id, content, source_file, tags, created, rank,
    #  final_score, fitness, importance, pinned, last_accessed,
    #  metadata, access_count, avg_dist)
    floater_tuple = (
        floater_row[0],
        floater_row[1],
        floater_row[2],
        floater_row[3],
        floater_row[4],
        0.0,
        1.0,
        floater_row[5],
        floater_row[6],
        floater_row[7],
        floater_row[8],
        floater_row[9],
        1,
        None,
    )
    state.results_to_display.insert(0, floater_tuple)
    try:
        state.output = _format_search_results(
            state.results_to_display,
            state.query, state.rerank, state.result_items, state.backlinks_map,
        )
    except Exception as _oe:
        logger.warning("_format_search_results (fallback) failed: %s", _oe)
