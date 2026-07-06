"""Post-save hook functions for the save pipeline.

Extracted from save_pipeline.py (2026-06-20) as part of the god-module
decomposition. These run after a save commits:

- _enrich_context: contextual enrichment of related notes metadata
- _recalculate_fitness_scores: incremental fitness recompute
- _run_post_save_hooks: the full post-save orchestration chain
- _enqueue_background_tasks: enqueue entity-resolution and
  fact-consolidation jobs for the background worker

_NOTE_: ``_update_memory_index_incremental`` is intentionally NOT
extracted here. It is the central orchestrator that ties together
all the indexers, backlink generators, and enrichers. It lives in
save_pipeline.py because pulling it out would force this module
to re-import every other save.* subpackage — the dependency would
just be a circular reference in disguise.

Behavior is identical to the inline versions. Re-exported from
save_pipeline for backward compat.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Any

if TYPE_CHECKING:
    from infra.db import AnyConnection

from infra.infrastructure import update_memory_md_locked
from infra.cache import _search_cache
import infra.audit as audit
from save.backlinks import (
    _auto_backlink_multi_part,
)

logger = logging.getLogger(__name__)

_get_config: Callable[[], Any] | None = None
try:
    from config import get_config as _gc
    _get_config = _gc
except ImportError:  # FLAVOR_A: optional dependency guard
    pass


def _enrich_context(db, note_id: str, content: str, category: str, tags: list):
    """Contextual enrichment: find related notes and add them to metadata.

    Research shows this provides 49% retrieval boost by creating richer
    embedding space connections between related memories.

    When MEMORY_CONTEXTUAL_ENRICHMENT=1, this function:
    1. Queries existing notes for similar content using FTS5
    2. Identifies top-N related notes by content overlap
    3. Adds related_note_ids to the memory's metadata
    4. Creates implicit context links for retrieval

    This is non-destructive — it only adds metadata, never removes content.
    """
    if _get_config is None or not _get_config().contextual_enrichment:
        return

    try:
        # Extract key terms from the new note's content
        # Use simple tokenization for now (avoid LLM dependency)
        words = re.findall(r"\b[a-zA-Z]{3,}\b", content.lower())
        if not words:
            return

        # Get unique terms, limit to top 20 to avoid noise
        search_terms = list(set(words))[:20]
        if not search_terms:
            return

        # Query for related notes using FTS5
        # Search for notes that share significant content overlap
        related_notes = []
        seen_ids = set()

        for term in search_terms[:5]:  # Limit to 5 terms to avoid slow queries
            try:
                rows = db.execute(
                    """SELECT fts.id, fts.content, fts.rank
                       FROM memories_fts fts
                       JOIN memories m ON m.id = fts.id
                       WHERE memories_fts MATCH ?
                       AND fts.id != ?
                       AND m.deleted_at IS NULL
                       ORDER BY fts.rank
                       LIMIT 5""",
                    (term, note_id),
                ).fetchall()

                for row in rows:
                    rid, rcontent, rank = row
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        # Calculate content overlap score
                        rwords = set(
                            re.findall(r"\b[a-zA-Z]{3,}\b", (rcontent or "").lower())
                        )
                        overlap = len(set(words) & rwords) / max(
                            1, len(set(words) | rwords)
                        )
                        if overlap > 0.1:  # At least 10% content overlap
                            related_notes.append((rid, overlap))
            except Exception:
                logger.warning(
                    "FTS search failed for term '%s' on note %s", term, note_id
                )
                continue

        # Sort by overlap score and take top 5
        related_notes.sort(key=lambda x: x[1], reverse=True)
        top_related = [rid for rid, score in related_notes[:5]]

        if not top_related:
            return

        # Update the memory's metadata with related notes
        try:
            # Get current metadata
            row = db.execute(
                "SELECT metadata FROM memories WHERE id = ?", (note_id,)
            ).fetchone()

            if row:
                metadata = json.loads(row[0]) if row[0] else {}
                metadata["contextual_related"] = top_related
                metadata["contextual_enriched_at"] = datetime.now(
                    timezone.utc
                ).isoformat()

                # Update the metadata
                db.execute(
                    "UPDATE memories SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata), note_id),
                )

                logger.debug(
                    "Contextual enrichment added %d related notes to %s",
                    len(top_related),
                    note_id,
                )
        except Exception as me:
            logger.warning(
                "Contextual enrichment metadata update failed for %s: %s", note_id, me
            )

    except Exception as e:
        logger.warning("Contextual enrichment skipped for %s: %s", note_id, e)


def _recalculate_fitness_scores(
    db_path: Path,
    memory_ids: list[str],
    conn: AnyConnection | None = None,
):
    """
    Incrementally recalculate fitness scores for specific memories.
    Avoids full index rebuild by updating only the affected rows.

    Two call modes:
    - Pass ``conn``: use the caller's connection (preferred — atomic
      with the caller's transaction; no extra connection-pool acquire).
    - Pass only ``db_path``: open a fresh pooled connection (legacy;
      used by tests and standalone callers).

    When called with ``conn``, the caller owns commit/close. When called
    without, this function opens, commits, and closes its own connection.
    """
    import math

    if not memory_ids:
        return

    # Initialize db to None so the finally block can safely check
    # `if owns_connection and db is not None`. The early-return path
    # (db_path doesn't exist) leaves db as None.
    db: AnyConnection | None = None
    owns_connection = conn is None
    try:
        if owns_connection:
            if not db_path.exists():
                return
            from infra.db_write_queue import sqlite_write_queue
            db = sqlite_write_queue.start_session(db_path)
        else:
            assert conn is not None
            db = conn
        today = date.today()
        w_r, w_f, w_s = (0.4, 0.3, 0.3)
        for mid in memory_ids:
            row = db.execute(
                "SELECT access_count, success_score, updated_at, decay, pinned\n                   FROM memories WHERE id = ?",
                (mid,),
            ).fetchone()
            if not row:
                continue
            access_count, success_score, updated_at, decay_setting, pinned = row
            access_count = access_count or 1
            success_score = success_score or 0.0
            decay_setting = str(decay_setting or "none").lower()
            decay_rates = {"none": 0.0, "standard": 0.01, "fast": 0.1}
            decay_rate = decay_rates.get(decay_setting, 0.0)
            try:
                updated_str = str(updated_at)
                if "T" in updated_str:
                    updated_date = date.fromisoformat(updated_str[:10])
                else:
                    updated_date = date.fromisoformat(updated_str)
            except (ValueError, TypeError):
                updated_date = today
            days_since_update = (today - updated_date).days
            decay_score = math.exp(-decay_rate * days_since_update)
            fitness_score = min(
                1.0,
                max(
                    0.0,
                    w_r * decay_score
                    + w_f * min(math.log1p(access_count), math.log1p(100))
                    + w_s * success_score,
                ),
            )
            db.execute(
                "UPDATE memories SET fitness_score = ? WHERE id = ?",
                (fitness_score, mid),
            )
        if owns_connection:
            db.commit()
    except Exception as e:
        logger.error("Error recalculating fitness scores: %s", e)
    finally:
        if owns_connection and db is not None:
            try:
                db.close()
            except Exception:
                logger.warning("Failed to safely close DB after fitness recalculation")
                pass


def _hook_invalidate_search_cache(note_id):
    """Invalidate the search cache for the just-saved note.

    Best-effort: falls back to a full cache clear on any failure so
    the save never blocks on cache maintenance.
    """
    try:
        from infra.cache import invalidate_cache_for_note

        invalidate_cache_for_note(note_id)
    except Exception:
        _search_cache.clear()


def _hook_update_memory_md_index(target_base, category, title_slug):
    """Refresh the per-repo MEMORY.md pointer for the just-saved note.

    Best-effort: a stale or missing index file is non-fatal.  The
    search-side hint (see ``recent_save_hint``) is the defense-in-depth
    fallback for any case where the index update fails or the file is
    missing.
    """
    index_file = target_base / "MEMORY.md"
    if not index_file.exists():
        return
    try:
        update_memory_md_locked(index_file, category, title_slug)
    except Exception:
        logger.warning("Failed to update MEMORY.md for %s/%s", category, title_slug)


def _hook_run_contradiction_check(db_path_obj, content, note_id):
    """Run the save-time contradiction scan and return the findings.

    Returns an empty list on failure — the caller decides whether to
    surface the failure (audit it) or continue silently.
    """
    try:
        from memory_contradiction_save import check_contradictions_on_save

        return check_contradictions_on_save(
            str(db_path_obj), content, note_id, top_n=20, min_confidence="low"
        )
    except Exception as _ce:
        logger.warning("save_memory: contradiction check failed: %s", _ce)
        return []


def _hook_audit_contradictions(db_path_obj, content, note_id, contradictions):
    """Emit a ``memory_save_contradiction_check`` audit row and log a warning.

    Called only when the contradiction scan returned a non-empty list.
    Best-effort: audit failures are logged at debug level so the save
    itself never fails because of a contradiction-audit problem.
    """
    if not contradictions:
        return
    try:
        audit.enqueue_audit(
            db_path=str(db_path_obj),
            tool="memory_save_contradiction_check",
            args={
                "note_id": note_id,
                "content_preview": (content or "")[:200],
                "contradiction_count": len(contradictions),
                "top_contradiction_ids": [
                    c.get("memory_id", "?") for c in contradictions[:3]
                ],
            },
            results_count=len(contradictions),
            top1_id=contradictions[0].get("memory_id"),
            latency_ms=0.0,
            error=None,
        )
    except Exception as _ae:
        logger.debug("save_memory: audit enqueue failed: %s", _ae)
    logger.warning(
        "save_memory: %d potential contradiction(s) detected for %s: %s",
        len(contradictions),
        note_id,
        [c.get("memory_id") for c in contradictions[:3]],
    )


def _hook_auto_backlink_with_flush(db_path_obj, note_id, category, title_slug, conn):
    """Run the multi-part auto-backlink generator and return any pending .md writes.

    P1-6 fix: pass conn to participate in active transaction, defer file writes.
    """
    try:
        pending = _auto_backlink_multi_part(db_path_obj, note_id, category, title_slug, conn=conn)
        if pending:
            from pathlib import Path
            return [(Path(db_path_obj).parent / f"{pid}.md", new_content) for pid, new_content in pending]
    except Exception as _abe:
        logger.debug("save_memory: auto-backlink failed: %s", _abe)
    return []


def _hook_track_decisions(db_path_obj, note_id, content, category):
    """Sprint 4: extract heuristic decision candidates and record them
    as thread events via SessionManager.

    Runs only when ``session_memory`` is enabled and *category* is one
    of the decision-relevant categories.  Errors are contained — a
    failure here never blocks the save.
    """
    try:
        from config import DECISION_CATEGORIES
    except ImportError:
        return
    if category not in DECISION_CATEGORIES:
        return
    try:
        from decision_extraction import _extract_decision_candidates
        from session_manager import SessionManager

        candidates = _extract_decision_candidates(content, category)
        if not candidates:
            return

        # Sprint 6: optionally enrich candidates via LLM (best-effort).
        try:
            from decision_extraction import _enrich_candidates_with_llm

            candidates = _enrich_candidates_with_llm(candidates, content)
        except ImportError:
            pass

        mgr = SessionManager()
        # Resolve the active session for this project; best-effort.
        try:
            from infra.memory_common import get_memory_paths

            _, local_mem, _ = get_memory_paths()
            conn = mgr._conn()
            try:
                row = conn.execute(
                    "SELECT id, project_root FROM sessions "
                    "WHERE status='active' ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            finally:
                from infra.db import safe_close_db

                safe_close_db(conn)
        except Exception:
            row = None

        if not row:
            return

        session_id, _ = row

        # Build a map of existing open threads for this session keyed by slug.
        try:
            conn = mgr._conn()
            try:
                existing = conn.execute(
                    "SELECT id, title, status FROM decision_threads "
                    "WHERE session_id=? AND status='open'",
                    (session_id,),
                ).fetchall()
            finally:
                from infra.db import safe_close_db

                safe_close_db(conn)
        except Exception:
            existing = []

        open_threads: dict[str, tuple[str, str]] = {}
        for tid, title, status in existing:
            open_threads[_slugify(title)] = (tid, title)

        for cand in candidates:
            if cand.thread_slug in open_threads:
                tid, title = open_threads[cand.thread_slug]
                mgr.record_event(
                    session_id=session_id,
                    thread_id=tid,
                    event_type=cand.event_type,
                    content=cand.claim,
                    confidence=cand.confidence,
                )
            else:
                tid = f"thread_{cand.thread_slug[:20]}"
                from session_manager import _save_system_record

                _save_system_record(
                    "decision_threads",
                    {
                        "id": tid,
                        "session_id": session_id,
                        "title": cand.title,
                        "status": "open",
                        "created_at": mgr._now(),
                        "version_vector": "{}",
                    },
                )
                open_threads[cand.thread_slug] = (tid, cand.title)
                mgr.record_event(
                    session_id=session_id,
                    thread_id=tid,
                    event_type=cand.event_type,
                    content=cand.claim,
                    confidence=cand.confidence,
                )
    except ImportError:
        pass
    except Exception as _e:
        logger.debug("save_memory: decision tracking failed: %s", _e)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _hook_extract_skill(conn, note_id, content, category):
    """Try to extract a skill tag for the just-saved note.

    Requires a live ``conn`` — the auto-backlink pass owns commit
    before this runs, so the connection is still open and on the
    current transaction's view.  No-op if skill extraction is disabled
    or fails.  P0 fix #5: pass ``category`` so the lower-threshold
    detector can apply its per-category bias.
    """
    if conn is None:
        return
    try:
        from skill_extractor import extract_skill_for_memory

        extract_skill_for_memory(conn, note_id, content, category=category)
    except Exception as _se:
        logger.debug("save_memory: skill extraction failed: %s", _se)


def _hook_audit_save_success(
    db_path_obj,
    note_id,
    category,
    title_slug,
    content,
    tags,
    pinned,
    is_global,
    start_time,
):
    """Record a successful save in the audit table.

    The audit row carries the caller's tags, pinning state, and
    elapsed wall-clock latency.  All exceptions are swallowed — the
    audit row is informational, not a precondition for the save.
    """
    try:
        audit.enqueue_audit(
            db_path=str(db_path_obj),
            tool="memory_save",
            args={
                "category": category,
                "title_slug": title_slug,
                "tags": tags,
                "pinned": pinned,
                "is_global": is_global,
                "content_preview": (content or "")[:200],
            },
            results_count=1,
            top1_id=note_id,
            latency_ms=(time.time() - start_time) * 1000.0,
            error=None,
        )
    except Exception as _ae:
        logger.debug("save_memory: audit enqueue failed: %s", _ae)


def _hook_record_recent_save(db_path_obj, note_id):
    """Publish a recent-save hint for the search-side floater.

    Defense-in-depth: ``search_memories`` reads this hint within a
    small window after a save and re-verifies the new note is visible
    in the result set — guards against any FTS5-vs-search path
    divergence.  No-op on failure.
    """
    try:
        from recent_save_hint import note_saved

        note_saved(note_id, str(db_path_obj))
    except Exception:
        logger.warning("Failed to record recent-save hint for %s", note_id)


def _hook_resolve_contradictions(db_path_obj, note_id, contradictions):
    """Close the time window on notes that contradict the new note.

    For each contradictory old note discovered by
    ``check_contradictions_on_save``, this hook marks it as superseded
    by the new note (sets ``valid_to`` and ``superseded_by``). This
    turns contradictions from warnings into structured temporal data:
    the old knowledge is preserved but timestamped.

    Best-effort — never raises. Individual failures are logged.
    """
    if not contradictions:
        return

    from save_pipeline import memory_supersede_db

    old_ids = list(dict.fromkeys(
        c.get("existing_note_id", "") for c in contradictions
    ))
    old_ids = [oid for oid in old_ids if oid and oid != note_id]

    for old_id in old_ids:
        try:
            if os.environ.get("MEMORY_CONTRADICTION_AUTO_RESOLVE_LLM") == "1":
                _resolve_with_llm(db_path_obj, old_id, note_id)
            else:
                ok, err = memory_supersede_db(db_path_obj, old_id, note_id)
                if ok:
                    logger.info(
                        "save_memory: resolved contradiction — closed %s (superseded by %s)",
                        old_id, note_id,
                    )
                else:
                    logger.warning(
                        "save_memory: contradiction resolution failed for %s: %s",
                        old_id, err,
                    )
        except Exception as e:
            logger.warning(
                "save_memory: contradiction resolution error for %s: %s",
                old_id, e,
            )


def _resolve_with_llm(db_path: str, source_note_id: str, target_note_id: str) -> None:
    """Attempt LLM-assisted contradiction resolution (best-effort, never raises)."""
    try:
        from kg.contradiction_resolver import auto_resolve_contradiction_pair
        result = auto_resolve_contradiction_pair(db_path, source_note_id, target_note_id)
        logger.info(
            "save_memory: LLM contradiction resolution — %s | action=%s",
            source_note_id,
            result.get("action") if isinstance(result, dict) else result,
        )
    except Exception as e:
        logger.warning(
            "save_memory: LLM contradiction resolution skipped for %s: %s",
            source_note_id, e,
        )


def _run_post_save_hooks(
    target_base,
    db_path_obj,
    note_id,
    category,
    title_slug,
    content,
    tags,
    pinned,
    is_global,
    safety_wiring,
    start_time,
    conn=None,
):
    """Orchestrate all post-save hook work as a sequence of named steps.

    Each step is a single-purpose helper — see the per-step functions
    above for individual contract and best-effort error handling.  The
    orchestrator itself never raises; the helpers are responsible for
    their own error containment.
    """
    deferred_writes = []
    _hook_update_memory_md_index(target_base, category, title_slug)
    _hook_invalidate_search_cache(note_id)
    if safety_wiring:
        contradictions = _hook_run_contradiction_check(db_path_obj, content, note_id)
        _hook_audit_contradictions(db_path_obj, content, note_id, contradictions)
        _hook_resolve_contradictions(db_path_obj, note_id, contradictions)
    backlink_writes = _hook_auto_backlink_with_flush(db_path_obj, note_id, category, title_slug, conn)
    if backlink_writes:
        deferred_writes.extend(backlink_writes)
    _hook_track_decisions(db_path_obj, note_id, content, category)
    _hook_extract_skill(conn, note_id, content, category)
    _hook_audit_save_success(
        db_path_obj,
        note_id,
        category,
        title_slug,
        content,
        tags,
        pinned,
        is_global,
        start_time,
    )
    _hook_record_recent_save(db_path_obj, note_id)
    return deferred_writes


def _enqueue_background_tasks(db_path_obj: Path, note_id: str, conn=None) -> None:
    """Best-effort enqueue of entity-resolution and fact-consolidation tasks.

    Runs after the main save transaction has committed. The background
    worker polls the task_queue table on its own schedule, so the memory
    row is guaranteed visible by the time the worker picks up the task.
    """
    try:
        from background.background_queue import init_task_queue, enqueue_task
        from infra._lazy_imports import get_config

        cfg = get_config()
        max_qs = getattr(cfg, "background_max_queue_size", 500)
        reject_pol = getattr(cfg, "background_reject_policy", "reject_new")

        _bq_conn = conn
        if _bq_conn is None:
            from infra.db_write_queue import sqlite_write_queue
            _bq_conn = sqlite_write_queue.start_session(db_path_obj)
        init_task_queue(_bq_conn)

        # Core KG indexing tasks
        enqueue_task(
            _bq_conn, "entity_resolution", {"memory_id": note_id},
            max_queue_size=max_qs, reject_policy=reject_pol,
        )
        enqueue_task(
            _bq_conn, "fact_consolidation", {"memory_id": note_id},
            max_queue_size=max_qs, reject_policy=reject_pol,
        )

        # Sprint 3 — Knowledge Compilation
        # Enqueue cross-memory entailment chain inference and concept
        # compilation per-save. dedup keeps queue bounded.
        try:
            SKILL_ENRICH = True  # Sprint 3: always on; promote to config flag later
            if SKILL_ENRICH:
                enqueue_task(
                    _bq_conn, "entailment_chains",
                    {"memory_id": note_id, "batch_size": 200, "min_confidence": 0.3},
                    max_queue_size=max_qs, reject_policy=reject_pol,
                )
                enqueue_task(
                    _bq_conn, "concept_compilation",
                    {"memory_id": note_id, "min_confidence": 0.4},
                    max_queue_size=max_qs, reject_policy=reject_pol,
                )
                enqueue_task(
                    _bq_conn, "skill_enrichment",
                    {"memory_id": note_id},
                    max_queue_size=max_qs, reject_policy=reject_pol,
                )
        except Exception as _enqueue_exc:
            logger.debug("save_memory: Sprint3 task enqueue failed: %s", _enqueue_exc)

        if conn is None:
            _bq_conn.close()
    except Exception as _bqe:
        logger.debug("save_memory: background queue enqueue failed: %s", _bqe)
