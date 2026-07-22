"""Phase 12: Contradiction Resolution Graph Engine (CRGE).

Inspects retrieved memory candidates for state contradiction or supersession,
resolves competing assertions by timeline timestamps, and decorates
snippets for downstream LLM prompt assembly.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Common functional state patterns (category -> regex)
_STATE_PATTERNS = [
    ("location", re.compile(r"\b(moved to|moved from .* to|living in|lives in|resides in|location is|relocated to|back in)\s+([A-Z][a-z]+(?:\s*(?:[A-Z][a-z]+|Texas|UK|US|USA))*)\b", re.IGNORECASE)),
    ("employer", re.compile(r"\b(works at|working at|employed by|joined|job at|role as|working remotely for)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", re.IGNORECASE)),
    ("database", re.compile(r"\b(?:primary database|database choice|database|adopted)(?:\s+(?:choice|from)\s+[A-Za-z0-9]+)*(?:\s+to)?\s+([A-Z][a-zA-Z0-9]+)\b", re.IGNORECASE)),
]


def resolve_candidate_contradictions(candidates: list[tuple], query: str = "") -> list[tuple]:
    """Detect state collisions across candidate snippets and apply timestamp-based resolution."""
    if not candidates or len(candidates) < 2:
        return candidates

    # Check if query asks for a specific temporal window (e.g. November 2024, August 2025, 2024-11)
    target_date_match = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b", query, re.IGNORECASE)
    target_ym = None
    if target_date_match:
        m_name = target_date_match.group(1).lower()
        year = target_date_match.group(2)
        month_num = {
            "january": "01", "february": "02", "march": "03", "april": "04",
            "may": "05", "june": "06", "july": "07", "august": "08",
            "september": "09", "october": "10", "november": "11", "december": "12"
        }.get(m_name, "01")
        target_ym = f"{year}-{month_num}"

    annotated = []
    seen_states: dict[str, tuple[str, str, str, int]] = {}  # cat -> (item_id, obj_val, timestamp, list_index)

    for idx, item in enumerate(candidates):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            annotated.append(item)
            continue

        item_list = list(item)
        item_id = str(item_list[0])
        content = str(item_list[1]) if item_list[1] is not None else ""
        
        # Extract date from content if present
        date_match = re.search(r"\[Session Date:\s*(\d{4}-\d{2}-\d{2})\]", content)
        if date_match:
            ts = date_match.group(1)
        else:
            ts = str(item_list[4]) if len(item_list) > 4 and item_list[4] is not None else ""

        score = float(item_list[6]) if len(item_list) > 6 and item_list[6] is not None else 1.0

        # If query target date matches this snippet date window, give a heavy boost
        if target_ym and ts.startswith(target_ym):
            score = abs(score) * 10.0 + 10.0
            if len(item_list) > 6:
                item_list[6] = score

        # Check for state patterns
        for cat, pat in _STATE_PATTERNS:
            match = pat.search(content)
            if match and match.lastindex:
                obj_val = match.group(match.lastindex).strip()

                if cat in seen_states:
                    prev_id, prev_val, prev_ts, prev_idx = seen_states[cat]
                    if prev_val.lower() != obj_val.lower():
                        # If user asked for current state (no specific historical date in query), boost newest
                        if not target_ym or (target_ym and ts.startswith(target_ym)):
                            if ts > prev_ts:
                                # Newest item gets a massive score boost
                                score = abs(score) * 10.0 + 10.0
                                item_list[6] = score
                                seen_states[cat] = (item_id, obj_val, ts, idx)
                                if prev_idx < len(annotated):
                                    old_item = list(annotated[prev_idx])
                                    if len(old_item) > 6:
                                        old_item[6] = 0.001
                                    annotated[prev_idx] = tuple(old_item)
                            else:
                                score = 0.001
                                if len(item_list) > 6:
                                    item_list[6] = score
                else:
                    score = abs(score) * 5.0 + 5.0
                    item_list[6] = score
                    seen_states[cat] = (item_id, obj_val, ts, idx)
                break

        annotated.append(tuple(item_list))

    annotated.sort(key=lambda x: float(x[6]) if len(x) > 6 and x[6] is not None else 0.0, reverse=True)
    return annotated
