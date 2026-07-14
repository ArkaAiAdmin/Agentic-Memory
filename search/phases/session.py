"""Phase 8 Session-aware result clustering."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SESSION_BOOST_FACTOR = 1.25

# Session keyword sets for intent detection in _phase_eight_session_cluster.
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


def _phase_eight_session_cluster(
    results: list,
    query: str,
    limit: int,
    boost_ids: set | None = None,
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

    return results
