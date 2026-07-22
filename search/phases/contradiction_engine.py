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
    ("location", re.compile(r"\b(moved to|living in|lives in|resides in|location is|relocated to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", re.IGNORECASE)),
    ("employer", re.compile(r"\b(works at|working at|employed by|joined|job at|role as)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", re.IGNORECASE)),
    ("preference", re.compile(r"\b(favorite|prefers|preference for)\s+([a-z\s]+)", re.IGNORECASE)),
]


def resolve_candidate_contradictions(candidates: list[tuple]) -> list[tuple]:
    """Detect state collisions across candidate snippets and apply timestamp-based resolution.

    Each candidate item is a tuple where:
      r[0] = id / note_id
      r[1] = content text
      r[4] = timestamp (created_at / observed_at)
      r[6] = score

    Returns modified candidate tuples with updated scores and context annotations.
    """
    if not candidates or len(candidates) < 2:
        return candidates

    annotated = []
    seen_states: dict[str, tuple[str, str, float, int]] = {}  # cat -> (item_id, obj_val, timestamp, list_index)

    for idx, item in enumerate(candidates):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            annotated.append(item)
            continue

        item_list = list(item)
        item_id = str(item_list[0])
        content = str(item_list[1]) if item_list[1] is not None else ""
        ts = str(item_list[4]) if len(item_list) > 4 and item_list[4] is not None else ""
        score = float(item_list[6]) if len(item_list) > 6 and item_list[6] is not None else 0.0

        # Check for state patterns
        for cat, pat in _STATE_PATTERNS:
            match = pat.search(content)
            if match:
                obj_val = match.group(2).strip()

                if cat in seen_states:
                    prev_id, prev_val, prev_ts, prev_idx = seen_states[cat]
                    if prev_val.lower() != obj_val.lower():
                        # Conflict detected between prev_val and obj_val
                        if ts > prev_ts:
                            # Current item is newer -> demote the older item
                            logger.debug("CRGE: item %s (%s) supersedes older item %s (%s)", item_id, obj_val, prev_id, prev_val)
                            seen_states[cat] = (item_id, obj_val, ts, idx)
                            if prev_idx < len(annotated):
                                old_item = list(annotated[prev_idx])
                                old_item[6] = float(old_item[6]) * 0.05
                                annotated[prev_idx] = tuple(old_item)
                        else:
                            # Current item is older -> demote current score
                            score *= 0.05
                            item_list[6] = score
                else:
                    seen_states[cat] = (item_id, obj_val, ts, idx)
                break

        annotated.append(tuple(item_list))

    return annotated
