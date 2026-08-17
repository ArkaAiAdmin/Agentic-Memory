"""Phase 14: Answer Synthesis & Temporal Date-Delta Solver.

Detects temporal interval queries (how many days/weeks/months passed between,
time difference between, how long after), extracts ISO dates and timestamps
from candidate snippets, and computes deterministic date deltas.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

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


def _extract_event_delta(query: str, candidates: list) -> str | None:
    """Extract dates for specific named events in the query and compute exact delta."""
    m = re.search(r"between\s+(?:when\s+)?(.*?)\s+and\s+(?:when\s+)?(.*)", query, re.IGNORECASE)
    if not m:
        return None
    event_a_phrase = m.group(1).strip().rstrip("?")
    event_b_phrase = m.group(2).strip().rstrip("?")

    def find_best_date_for_phrase(phrase: str) -> tuple[datetime, str] | None:
        stopwords = {"i", "started", "working", "on", "the", "module", "for", "our", "system", "began", "developing", "a", "an", "to", "in", "of", "and", "when"}
        words = set(re.findall(r"\w+", phrase.lower())) - stopwords
        if not words:
            words = set(re.findall(r"\w+", phrase.lower()))

        best_dt = None
        best_overlap = 0
        best_str = ""

        for item in candidates[:10]:
            cnt = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 else ""
            d_match = re.search(r"\[Session Date:\s*(\d{4}-\d{2}-\d{2})", cnt)
            if not d_match:
                d_match = _DATE_RE.search(cnt)
            if not d_match:
                continue
            dt = parse_iso_date(d_match.group(1))
            if not dt:
                continue

            cnt_words = set(re.findall(r"\w+", cnt.lower()))
            overlap = len(words & cnt_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_dt = dt
                best_str = d_match.group(1)

        return (best_dt, best_str) if (best_dt and best_overlap > 0) else None

    res_a = find_best_date_for_phrase(event_a_phrase)
    res_b = find_best_date_for_phrase(event_b_phrase)

    if res_a and res_b and res_a[0] != res_b[0]:
        days = abs((res_b[0] - res_a[0]).days)
        fmt_a = res_a[0].strftime("%B %-d, %Y")
        fmt_b = res_b[0].strftime("%B %-d, %Y")
        return f"{days} days passed between {event_a_phrase} on {fmt_a} and {event_b_phrase} on {fmt_b}."
    return None


def calculate_temporal_delta(query: str, candidates: list[tuple]) -> str | None:
    """Extract dates from top retrieved candidates and compute temporal delta in days/weeks."""
    if not candidates:
        return None

    is_delta_query = any(pat.search(query) for pat in _DELTA_PATTERNS)
    if not is_delta_query:
        return None

    # First check event-specific temporal delta
    event_delta = _extract_event_delta(query, candidates)
    if event_delta:
        return event_delta

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
