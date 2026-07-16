"""Phase 8 Session-aware result clustering."""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

_SESSION_BOOST_FACTOR = 1.25
_SESSION_AFFINITY_BOOST = 1.15

# Session keyword sets for intent detection in _phase_nine_session_cluster.
_SESSION_SINGLE_KW = frozenset({
    "session", "meeting", "discussed", "talked about", "talked",
    "today", "this morning", "yesterday",
    "we did", "we fixed", "we were", "we talked",
    "pair", "sprint", "retro", "debug", "review", "code review",
    "this afternoon", "earlier", "this week",
})
_SESSION_MULTI_KW = frozenset({
    "week", "month", "project", "all", "patterns",
    "strategies", "practices", "approaches", "decisions",
    "across", "established", "timeline", "have we",
    "best practices", "what have we", "have we learned",
})


def _get_session_entities(
    db: AnyConnection,
    session_source_files: list[str],
    limit_per_session: int = 10,
) -> dict[str, set[str]]:
    """Extract entity names from memories belonging to specific sessions.

    Looks up memories by source_file prefix and extracts entity tokens
    from their memory IDs (the slug after the category prefix).

    Returns a dict mapping session_source_file -> set of entity names.
    """
    if not session_source_files:
        return {}

    session_entities: dict[str, set[str]] = {}

    for sf in session_source_files:
        try:
            rows = db.execute(
                "SELECT id FROM tenant_memories "
                "WHERE source_file LIKE ? AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (f"{sf}%", limit_per_session),
            ).fetchall()
            entities = set()
            for row in rows:
                mid = row[0] if not isinstance(row, sqlite3.Row) else row["id"]
                if "/" in mid:
                    slug = mid.split("/", 1)[1]
                    entities.add(slug.lower())
                    for word in re.findall(r"[a-z0-9]+", slug.lower()):
                        if len(word) > 2:
                            entities.add(word)
            session_entities[sf] = entities
        except (sqlite3.Error, Exception) as e:
            logger.debug("_get_session_entities failed for %s: %s", sf, e)
            session_entities[sf] = set()

    return session_entities


def _compute_query_entities(query: str) -> set[str]:
    """Extract entity-like tokens from a query for cross-session matching."""
    tokens = set()
    for word in re.findall(r"[a-z0-9]{3,}", query.lower()):
        tokens.add(word)
    return tokens


def _compute_session_affinity_scores(
    query_entities: set[str],
    session_entities: dict[str, set[str]],
) -> dict[str, float]:
    """Compute affinity scores for sessions based on shared entities.

    Higher scores indicate more overlap between query entities and
    session entities. The score is normalized to [0, 1].
    """
    if not query_entities or not session_entities:
        return {}

    scores: dict[str, float] = {}
    max_possible = len(query_entities) if query_entities else 1

    for sf, entities in session_entities.items():
        if not entities:
            scores[sf] = 0.0
            continue
        overlap = query_entities & entities
        scores[sf] = len(overlap) / max_possible if max_possible > 0 else 0.0

    return scores


def _phase_nine_session_cluster(
    results: list,
    query: str,
    limit: int,
    boost_ids: set | None = None,
    db: AnyConnection | None = None,
) -> list:
    """Phase 9: session-aware result clustering and score adjustment.

    Extracts session_id from each candidate's ``source_file`` (index 2),
    groups by session, detects single-session vs multi-session query
    intent via keyword analysis, and adjusts ranks accordingly.

    *Single-session intent* (keywords like "session", "meeting", "discussed"):
      boosts results from the most-represented session by reducing their
      rank (0.5×), so they float higher when the reranker processes them.

    *Multi-session intent* (keywords like "patterns", "across", "decisions"):
      caps per-session representation in the top results to diversify
      across different sessions.

    *Cross-session entity boost* (when MEMORY_SESSION_CROSS_ENTITY_BOOST=1):
      detects shared entities between sessions via the knowledge graph and
      boosts results from sessions that share entities with the current query.

    No-op when no session-type results are found or when intent is
    ambiguous.  All exceptions are swallowed — clustering is a quality
    optimization, never a precondition.
    """
    if not results:
        return results

    q_lower = query.lower()
    has_single = any(kw in q_lower for kw in _SESSION_SINGLE_KW)
    has_multi = any(kw in q_lower for kw in _SESSION_MULTI_KW)

    session_groups: dict[str, list] = {}
    non_session: list = []
    for r in results:
        sf = r[2] or ""
        if sf.startswith("sessions/"):
            session_groups.setdefault(sf, []).append(r)
        else:
            non_session.append(r)

    if not session_groups:
        return results

    if has_single and not has_multi:
        best_session = max(session_groups, key=lambda s: len(session_groups[s]))
        # Collect the best-session result ids so _rerank_results can apply a
        # real final_score boost (post-normalization).  The previous approach
        # multiplied the raw rank here, which the later re-normalization
        # erased — a no-op.  We no longer mutate ranks in this phase.
        if boost_ids is not None:
            for r in results:
                sf = r[2] or ""
                if sf.startswith("sessions/") and sf == best_session:
                    boost_ids.add(r[0])
        return results

    if has_multi and not has_single:
        n_sessions = len(session_groups)
        if n_sessions >= 2:
            per_session_cap = max(1, limit // n_sessions)
            interleaved = []
            for sid, group in sorted(session_groups.items()):
                sorted_group = sorted(group, key=lambda x: x[5])
                interleaved.extend(sorted_group[:per_session_cap])
            combined = interleaved + non_session
            combined.sort(key=lambda x: x[5])
            return combined

    # Cross-session entity boost (feature-flagged)
    try:
        from infra.memory_common import get_config
        config = get_config()
        if not getattr(config.features, "session_cross_entity_boost", False):
            return results
    except (ImportError, AttributeError):
        return results

    # Only run cross-session boost when we have a DB connection and multiple sessions
    if db is None or len(session_groups) < 2:
        return results

    try:
        # Extract entities from the query
        query_entities = _compute_query_entities(query)
        if not query_entities:
            return results

        # Extract entities from each session's memories
        session_source_files = list(session_groups.keys())
        session_entities = _get_session_entities(db, session_source_files)

        # Compute affinity scores
        affinity_scores = _compute_session_affinity_scores(query_entities, session_entities)
        if not affinity_scores:
            return results

        # Find sessions with high affinity (>0.2 overlap)
        high_affinity_sessions = {
            sf for sf, score in affinity_scores.items() if score > 0.2
        }

        if not high_affinity_sessions:
            return results

        # Boost results from sessions with high entity affinity
        if boost_ids is not None:
            for r in results:
                sf = r[2] or ""
                if sf in high_affinity_sessions:
                    boost_ids.add(r[0])

        return results

    except Exception as e:
        logger.debug("_phase_nine_session_cluster cross-entity boost failed: %s", e)
        return results
