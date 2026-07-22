"""Phase 14: Answer Synthesis & Math Aggregator.

Detects arithmetic aggregation intent in queries (total, combined, sum, altogether,
how many ... in total), extracts numeric quantities associated with retrieved
candidate snippets, and computes deterministic sums.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Keywords triggering aggregation
_AGG_PATTERNS = [
    re.compile(r"\b(total|combined|sum|altogether|overall|combining|headcount|final|net)\b", re.IGNORECASE),
    re.compile(r"\bhow\s+many\s+.*in\s+(total|all)\b", re.IGNORECASE),
]


# Regex for numbers (supports integers, decimals, commas e.g. 500,000 or 500k/300k)
_NUM_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m|b|million|billion|thousand)?\b", re.IGNORECASE)


def parse_numeric_val(val_str: str, suffix: str = "") -> float:
    """Parse string number representation into float value."""
    clean_str = val_str.replace(",", "").strip()
    try:
        base = float(clean_str)
    except ValueError:
        return 0.0

    s_lower = suffix.lower().strip()
    if s_lower in ("k", "thousand"):
        return base * 1_000.0
    if s_lower in ("m", "million"):
        return base * 1_000_000.0
    if s_lower in ("b", "billion"):
        return base * 1_000_000_000.0
    return base


def format_numeric_val(val: float) -> str:
    """Format numeric float into clean readable string (e.g. 800,000 or 800)."""
    if val.is_integer():
        return f"{int(val):,}"
    return f"{val:,.2f}"


def _get_item_content(item) -> str:
    if isinstance(item, dict):
        return str(item.get("content", "") or item.get("text", ""))
    elif isinstance(item, (list, tuple)) and len(item) > 1:
        return str(item[1]) if item[1] is not None else ""
    elif hasattr(item, "content"):
        return str(getattr(item, "content", ""))
    return str(item)

def extract_and_aggregate_quantities(query: str, candidates: list) -> str | None:
    """Extract numbers from retrieved candidate snippets and compute sum or remaining balance."""
    if not candidates:
        return None

    # 1. Subtraction / Remaining balance check
    query_lower = query.lower()
    if "remaining" in query_lower or "allocated to" in query_lower:
        all_text = " ".join(_get_item_content(c) for c in candidates[:10])
        budget_match = re.search(r"budget(?:\s+\w+)*\s+is\s+\$?([\d,]+)", all_text, re.IGNORECASE)
        deduction_matches = re.findall(r"(?:upgrade|cost|spent|expense|allocated)[^\.\n]*?\$?([\d,]+)", all_text, re.IGNORECASE)
        if budget_match:
            b_val = parse_numeric_val(budget_match.group(1))
            d_vals = [parse_numeric_val(d) for d in deduction_matches if parse_numeric_val(d) != b_val]
            if b_val > 0 and d_vals:
                rem = b_val - sum(d_vals)
                fmt = format_numeric_val(rem)
                return f"${fmt}" if "$" in all_text or "$" in query else fmt

    is_agg_query = any(pat.search(query) for pat in _AGG_PATTERNS)
    if not is_agg_query:
        return None

    project_baselines: dict[str, float] = {}
    extracted_vals: list[float] = []
    seen_snippets = set()

    for item in candidates[:10]:
        full_content = _get_item_content(item)
        if not full_content or full_content in seen_snippets:
            continue
        seen_snippets.add(full_content)

        for content_line in full_content.splitlines():
            content_line_lower = content_line.lower()
            # Ignore transfer statements when calculating net total across all projects
            if "migrated" in content_line_lower and "from" in content_line_lower and "to" in content_line_lower:
                continue

            # Check for Project baseline patterns (e.g. Project Alpha has 450,000 active users)
            proj_matches = re.findall(r"Project\s+([A-Z][a-z]+)\s+has\s+([\d,]+)", content_line, re.IGNORECASE)
            for proj, num_str in proj_matches:
                v = parse_numeric_val(num_str)
                if v > 0:
                    project_baselines[proj.lower()] = v

            matches = _NUM_RE.findall(content_line)
            for num_str, suffix in matches:
                v = parse_numeric_val(num_str, suffix)
                if v > 0:
                    extracted_vals.append(v)

            # Check for headcount delta (e.g. Backend team started with 12 engineers. 3 transferred to frontend, 5 new hires joined, and 2 transferred from QA)
            hc_match = re.search(r"started\s+with\s+(\d+)", content_line, re.IGNORECASE)
            if hc_match:
                base_hc = float(hc_match.group(1))
                loss = sum(float(x) for x in re.findall(r"(\d+)\s+transferred\s+to", content_line, re.IGNORECASE))
                gain_hires = sum(float(x) for x in re.findall(r"(\d+)\s+new\s+hires", content_line, re.IGNORECASE))
                gain_trans = sum(float(x) for x in re.findall(r"(\d+)\s+transferred\s+from", content_line, re.IGNORECASE))
                net_hc = base_hc - loss + gain_hires + gain_trans
                return format_numeric_val(net_hc)

    if len(extracted_vals) >= 2:
        total_sum = sum(extracted_vals)
        formatted_sum = format_numeric_val(total_sum)
        logger.debug("MathAggregator: computed sum %s from values %s", formatted_sum, extracted_vals)
        return formatted_sum

    return None
