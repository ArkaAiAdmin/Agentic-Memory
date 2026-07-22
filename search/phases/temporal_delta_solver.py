"""Temporal Date-Delta & Interval Solver for Agentic Memory.

Detects temporal interval queries (how many days/weeks/months passed between,
time difference between, how long after), extracts ISO dates and timestamps
from candidate snippets, and computes deterministic date deltas.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Query patterns asking for time deltas
_DELTA_PATTERNS = [
    re.compile(r"\b(how\s+many\s+(days|weeks|months|years)\s+(passed|between|elapsed|from))\b", re.IGNORECASE),
    re.compile(r"\b(time\s+difference|time\s+gap|duration|how\s+long\s+after|how\s+long\s+between)\b", re.IGNORECASE),
    re.compile(r"\b(days|weeks|months)\s+passed\b", re.IGNORECASE),
]

# Regex for ISO-like dates (YYYY-MM-DD or YYYY/MM/DD)
_DATE_RE = re.compile(r"\b(\d{4}[-/]\d{2}[-/]\d{2})\b")


def parse_iso_date(date_str: str) -> datetime | None:
    """Parse date string into UTC datetime."""
    clean = date_str.replace("/", "-").strip()
    try:
        return datetime.strptime(clean, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def calculate_temporal_delta(query: str, candidates: list[tuple]) -> str | None:
    """Extract dates from top retrieved candidates and compute temporal delta in days/weeks."""
    if not candidates:
        return None

    is_delta_query = any(pat.search(query) for pat in _DELTA_PATTERNS)
    if not is_delta_query:
        return None

    dates: list[tuple[datetime, str]] = []

    for item in candidates[:10]:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        content = str(item[1]) if item[1] is not None else ""
        ts_str = str(item[4]) if len(item) > 4 and item[4] is not None else ""

        # Extract dates from content text
        content_dates = _DATE_RE.findall(content)
        for d_str in content_dates:
            dt = parse_iso_date(d_str)
            if dt and dt not in [d[0] for d in dates]:
                dates.append((dt, d_str))

        # Extract date from item timestamp if present
        if ts_str and len(ts_str) >= 10:
            dt = parse_iso_date(ts_str[:10])
            if dt and dt not in [d[0] for d in dates]:
                dates.append((dt, ts_str[:10]))

    if len(dates) >= 2:
        # Sort chronologically
        dates.sort(key=lambda x: x[0])
        earliest = dates[0][0]
        latest = dates[-1][0]
        delta_days = abs((latest - earliest).days)

        query_lower = query.lower()
        if "week" in query_lower:
            weeks = round(delta_days / 7.0, 1)
            formatted = f"{int(weeks) if weeks.is_integer() else weeks} weeks"
        elif "month" in query_lower:
            months = round(delta_days / 30.4, 1)
            formatted = f"{int(months) if months.is_integer() else months} months"
        elif "year" in query_lower:
            years = round(delta_days / 365.25, 1)
            formatted = f"{int(years) if years.is_integer() else years} years"
        else:
            formatted = f"{delta_days} days"

        logger.debug("TemporalDeltaSolver: computed %s between %s and %s", formatted, dates[0][1], dates[-1][1])
        return formatted

    return None
