"""Session context builder for LTR features.

Builds the session_ctx dict that populates session-aware LTR features
(was_returned_in_prior, was_clicked_in_prior, session_dwell) from
recent CTR feedback data.

Usage:
    from search.ltr.session_ctx import build_session_ctx
    ctx = build_session_ctx(db, lookback=10)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


def build_session_ctx(
    db: "AnyConnection | None",
    lookback: int = 10,
    time_window_hours: float = 4.0,
) -> dict:
    """Build session context from recent CTR feedback.

    Queries memory_ctr_feedback for recent impressions and builds:
    - prior_clicked_ids: set of memory IDs that were clicked recently
    - prior_returned_ids: dict of {memory_id: avg_seconds_since_return}

    Args:
        db: Database connection.
        lookback: Max number of recent queries to consider.
        time_window_hours: Only consider impressions from the last N hours.

    Returns:
        Dict with keys 'prior_clicked_ids' and 'prior_returned_ids'.
    """
    if db is None:
        return {"prior_clicked_ids": set(), "prior_returned_ids": {}}

    try:
        cutoff_ts = time.time() - (time_window_hours * 3600)

        # Get recent distinct query_ids
        rows = db.execute(
            "SELECT DISTINCT query_id FROM memory_ctr_feedback "
            "WHERE returned_at > ? "
            "ORDER BY returned_at DESC LIMIT ?",
            (cutoff_ts, lookback),
        ).fetchall()
        if not rows:
            return {"prior_clicked_ids": set(), "prior_returned_ids": {}}

        query_ids = [r[0] for r in rows]

        # Get all impressions for those queries
        ph = ",".join("?" * len(query_ids))
        impressions = db.execute(
            f"SELECT query_id, id, returned_at, clicked_at "
            f"FROM memory_ctr_feedback "
            f"WHERE query_id IN ({ph}) "
            f"ORDER BY returned_at",
            query_ids,
        ).fetchall()

        prior_clicked: set[str] = set()
        prior_returned: dict[str, float] = {}
        now = time.time()

        for qid, mid, returned_at, clicked_at in impressions:
            if mid in prior_clicked:
                continue
            if clicked_at:
                prior_clicked.add(mid)
            # Track how long ago this was returned (for dwell feature)
            if mid not in prior_returned:
                prior_returned[mid] = now - returned_at
            else:
                # Average dwell across multiple appearances
                prior_returned[mid] = (prior_returned[mid] + (now - returned_at)) / 2.0

        return {
            "prior_clicked_ids": prior_clicked,
            "prior_returned_ids": prior_returned,
        }
    except Exception:
        return {"prior_clicked_ids": set(), "prior_returned_ids": {}}
