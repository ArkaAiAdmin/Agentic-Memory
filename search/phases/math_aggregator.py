"""Math Aggregator & Quantity Sum Solver for Agentic Memory.

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
    re.compile(r"\b(total|combined|sum|altogether|overall|combining)\b", re.IGNORECASE),
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


def extract_and_aggregate_quantities(query: str, candidates: list[tuple]) -> str | None:
    """Extract numbers from retrieved candidate snippets and compute sum if query requests total."""
    if not candidates:
        return None

    is_agg_query = any(pat.search(query) for pat in _AGG_PATTERNS)
    if not is_agg_query:
        return None

    extracted_vals: list[float] = []
    seen_snippets = set()

    for item in candidates[:10]:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        content = str(item[1]) if item[1] is not None else ""
        if content in seen_snippets:
            continue
        seen_snippets.add(content)

        matches = _NUM_RE.findall(content)
        for num_str, suffix in matches:
            v = parse_numeric_val(num_str, suffix)
            if v > 0:
                extracted_vals.append(v)

    if len(extracted_vals) >= 2:
        total_sum = sum(extracted_vals)
        formatted_sum = format_numeric_val(total_sum)
        logger.debug("MathAggregator: computed sum %s from values %s", formatted_sum, extracted_vals)
        return formatted_sum

    return None
