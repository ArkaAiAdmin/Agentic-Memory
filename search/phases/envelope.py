"""Phase 12 + 14 Envelope and finalization functions."""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Optional

from infra.error_counter import increment as _phase_inc
from search.query_parser import _build_zero_result_suggestions
from search.phases.postprocess import _format_search_results

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)


def _get_agent_scope() -> str:
    try:
        from agent_context import get_agent
        return get_agent().namespace or "default"
    except (ImportError, AttributeError):
        return "default"


def _build_result_items(
    *, db: AnyConnection, results_to_display: list, query: str, rerank: bool,
    db_path: Any = None,
    as_of: Optional[float] = None,
) -> tuple[list, list[str], dict]:
    """Phase 10 of search_memories: build the public result list and output.

    Two responsibilities:

    1. Build ``result_items`` (a list of dicts) for the public API
       and gather backlinks (forward + reverse) for each result row.
    2. Call ``_format_search_results`` to produce the human-readable
       ``output`` string.

    T10: when ``related_facts`` is provided, appends a "Related facts"
    section to the output and includes the facts in the result envelope
    (caller decides whether to surface it on the wire).

    Backlinks lookups are wrapped in try/except because the
    ``backlinks`` table is optional in some deployments — the
    function must still produce a valid result set if the lookup
    fails.
    """
    from search.enrichment import _apply_post_rank_metadata

    result_items = []
    result_ids = [r[0] for r in results_to_display]
    backlinks_map: dict = {}
    category_map: dict = {}
    if result_ids:
        ph = ",".join("?" * len(result_ids))
        try:
            for row in db.execute(
                f"""
                SELECT target_id, source_id FROM backlinks WHERE target_id IN ({ph})
                UNION ALL
                SELECT source_id, target_id FROM backlinks WHERE source_id IN ({ph})
                """,
                result_ids + result_ids,
            ).fetchall():
                backlinks_map.setdefault(row[0], []).append(row[1])
        except Exception as _oe:
            logger.warning("backlinks fetch failed: %s", _oe)
        try:
            for row in db.execute(
                f"SELECT id, category FROM tenant_memories WHERE id IN ({ph})",
                result_ids,
            ).fetchall():
                category_map[row[0]] = row[1]
        except Exception as _ce:
            logger.warning("category fetch failed: %s", _ce)
    for r in results_to_display:
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
        last_accessed = r[10] if len(r) > 10 else None
        metadata_json = r[11] if len(r) > 11 else None
        supersedes = r[12] if len(r) > 12 else None
        try:
            tags = json.loads(tags_json) if tags_json else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        backlinks = backlinks_map.get(note_id, [])
        auto_summary = None
        meta = None
        if metadata_json:
            try:
                meta = (
                    json.loads(metadata_json)
                    if isinstance(metadata_json, str)
                    else metadata_json
                )
                if meta and meta.get("auto_summary"):
                    auto_summary = meta["auto_summary"]
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        result_items.append(
            {
                "id": note_id,
                "content": content,
                "source_file": source_file,
                "tags": tags,
                "created": created,
                "rank": rank,
                "final_score": final_score,
                "fitness_score": fitness_score,
                "importance": importance_val,
                "pinned": pinned,
                "backlinks": backlinks,
                "last_accessed": last_accessed,
                "metadata": meta if meta is not None else {},
                "summary": auto_summary,
                "category": category_map.get(note_id),
                "supersedes": supersedes,
            }
        )
    output = _format_search_results(
        results_to_display,
        query,
        rerank,
        result_items,
        backlinks_map,
    )
    # RANK-FIRST LOCK (PR1.1): attach enrichment as order-invariant
    # envelope fields. This is the ONLY post-CE enrichment site; it never
    # re-sorts or mutates the ranking final_score.
    if db_path is not None:
        result_items = _apply_post_rank_metadata(
            result_items, query, db_path, as_of=as_of
        )
    return result_items, output, backlinks_map


def _build_empty_result_with_hint(
    *, cache_key: str | None = None, query: str, db_path: Any, hint: str | None, related_facts: list[dict] | None = None
) -> dict:
    """Build the standard "no results" envelope used by the early-return paths.

    Used when FTS5 + embedding fallback return nothing, and when the
    temporal filter eliminates everything.  Caches and returns the
    result via ``_cache_store_result``.

    T10: when ``related_facts`` is non-empty, append the standard
    "Related facts (KG)" section to the output and attach the facts
    to the envelope.  This is how the empty-DB path can still surface
    matching KG facts (the common case when the user has facts but
    no memories yet).
    """
    if hint:
        output = f"No memories matched the query: '{query}'. {hint}"
    else:
        output = f"No memories matched the query: '{query}'"
    facts_section: list[str] = []
    if related_facts:
        facts_section.append("")
        facts_section.append("--- Related facts (KG) ---")
        for j, fact in enumerate(related_facts, 1):
            et = fact.get("event_time")
            et_str = ""
            if et is not None:
                from datetime import datetime as _dt, timezone as _tz

                try:
                    et_str = f" [event_time={_dt.fromtimestamp(et, tz=_tz.utc).strftime('%Y-%m-%d')}]"
                except (ValueError, OSError):
                    pass
            conf = fact.get("confidence", 0.0) or 0.0
            facts_section.append(
                f"  [F{j}] {fact['subject']} --[{fact['predicate']}]--> "
                f"{fact['object']} (conf={conf:.2f}, mentions={fact.get('mention_count', 1)})"
                f"{et_str}"
            )
        output = output + "\n" + "\n".join(facts_section)
    result = {
        "results": [],
        "count": 0,
        "output": output,
        "suggestions": _build_zero_result_suggestions(db_path, query),
        "agent_scope": _get_agent_scope(),
        "query_id": uuid.uuid4().hex,
    }
    if related_facts:
        result["related_facts"] = related_facts
    if cache_key:
        try:
            from search.orchestrator import _cache_store_result
            _cache_store_result(cache_key, result)
        except (ImportError, AttributeError):
            pass
    return result


def _record_last_accessed(db: AnyConnection | None, result_items: list) -> None:
    """Phase 12 of search_memories: stamp last_accessed on every result row.

    Bumps ``last_accessed`` to the current ISO timestamp in a single
    batched UPDATE for every result note.  No-op if the result set is
    empty.  All exceptions are swallowed — adaptive retention depends
    on this column but a failure to record is non-fatal.
    """
    if not result_items:
        return
    if db is None:
        return
    try:
        import datetime as _dt

        now_iso = _dt.datetime.now().isoformat(timespec="seconds")
        ids = [r["id"] for r in result_items]
        _CHUNK_SIZE = 998  # 1 slot reserved for the timestamp param
        for i in range(0, len(ids), _CHUNK_SIZE):
            chunk = ids[i : i + _CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            db.execute(
                f"UPDATE memories SET last_accessed = ? WHERE id IN ({placeholders}) AND tenant_id = tenant_id()",
                [now_iso] + chunk,
            )
        db.commit()
    except Exception as e:
        _phase_inc("search.record_last_accessed", e)
        logger.warning("_record_last_accessed failed: %s", e)


def _build_search_result_envelope(
    *,
    result_items: list,
    output: list[str],
    results_to_display: list,
    synthesize: bool,
    query: str,
    max_synthesis_sentences: int,
    related_facts: Optional[list[dict]] = None,
) -> dict:
    """Build the final public-API result dict for a search call.

    Joins the per-row ``output`` lines into a single string, attaches
    a fresh ``query_id`` (used downstream for CTR feedback correlation),
    and optionally calls the synthesis pass when ``synthesize=True``.

    T10: if ``related_facts`` is provided:
      - attaches them under the ``related_facts`` key on the envelope
      - appends a "Related facts (KG)" section to the joined output
        (this is done HERE rather than in _format_search_results so
        it survives the post-Phase-10 regeneration passes — safety
        demoting, quality gates, user profiling, strong-match boost,
        and save-hint floater all rewrite the output list).

    The synthesis step is best-effort: a failure to synthesize must
    never block the user from getting the underlying results.

    Args:
        result_items: List of public-API result dicts (one per row).
        output: List of human-readable output lines (one per result).
        results_to_display: Raw DB row tuples for result formatting.
        synthesize: If True, call ``_bb1_synthesize`` and attach the
            ``synthesis`` key to the envelope.
        query: The original search query string (used for synthesis).
        max_synthesis_sentences: Maximum sentences in synthesis output.
        related_facts: Optional list of KG fact dicts to attach.

    Returns:
        A public-API result dict with ``results``, ``count``,
        ``output``, ``raw_results``, ``query_id``, ``agent_scope``,
        and optionally ``synthesis`` and ``related_facts``.
    """
    from search.synthesis import _bb1_synthesize

    # T10: build the related-facts section BEFORE joining so the
    # regeneration passes can't strip it.
    facts_section: list[str] = []
    if related_facts:
        facts_section.append("")
        facts_section.append("--- Related facts (KG) ---")
        for j, fact in enumerate(related_facts, 1):
            et = fact.get("event_time")
            et_str = ""
            if et is not None:
                from datetime import datetime as _dt, timezone as _tz

                try:
                    et_str = f" [event_time={_dt.fromtimestamp(et, tz=_tz.utc).strftime('%Y-%m-%d')}]"
                except (ValueError, OSError):
                    pass
            conf = fact.get("confidence", 0.0) or 0.0
            facts_section.append(
                f"  [F{j}] {fact['subject']} --[{fact['predicate']}]--> "
                f"{fact['object']} (conf={conf:.2f}, mentions={fact.get('mention_count', 1)})"
                f"{et_str}"
            )
    result_str = "\n\n".join(output + facts_section)
    query_id = uuid.uuid4().hex
    result = {
        "results": result_items,
        "count": len(result_items),
        "output": result_str,
        "raw_results": results_to_display,
        "query_id": query_id,
    }
    if related_facts:
        result["related_facts"] = related_facts
    if synthesize and result_items:
        try:
            _display_scores = {
                it.get("id"): it.get("display_score")
                for it in result_items
                if it.get("id") is not None and it.get("display_score") is not None
            }
            synth = _bb1_synthesize(
                query,
                results_to_display,
                max_sentences=max_synthesis_sentences,
                display_scores=_display_scores or None,
            )
            result["synthesis"] = synth
        except Exception as e:
            logger.warning("synthesis failed: %s", e)
    try:
        from agent_context import get_agent
        result["agent_scope"] = get_agent().namespace
    except (ImportError, AttributeError):
        result["agent_scope"] = "default"
    return result
