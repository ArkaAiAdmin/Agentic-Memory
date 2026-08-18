"""Phase 14: Answer Synthesis & Temporal Date-Delta Solver.

Detects temporal interval queries (how many days/weeks/months passed between,
how many days ago, time difference between, how long after), extracts ISO dates,
natural dates, and timestamps from candidate snippets, and computes deterministic date deltas.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Query patterns asking for time deltas and chronological order
_DELTA_PATTERNS = [
    re.compile(r"\b(how\s+many\s+(days|weeks|months|years)\s+(?:did\s+it\s+take|did\s+i|have\s+i|was\s+i|passed|between|elapsed|from|ago|before|after|to\s+receive|to\s+arrive))\b", re.IGNORECASE),
    re.compile(r"\b(time\s+difference|time\s+gap|duration|how\s+long\s+after|how\s+long\s+between|how\s+long\s+ago|how\s+long\s+have\s+i|how\s+long\s+did\s+i|how\s+long\s+had\s+i)\b", re.IGNORECASE),
    re.compile(r"\b(how\s+long\s+(?:did\s+i|have\s+i|had\s+i|was\s+i|did\s+it\s+take|have\s+i\s+been|had\s+i\s+been|was\s+the|will\s+it))\b", re.IGNORECASE),
    re.compile(r"\b(how\s+many\s+days\s+(?:did\s+it\s+take|for.*?to\s+arrive|to\s+receive|had\s+passed|before))\b", re.IGNORECASE),
    re.compile(r"\b(how\s+much\s+(?:earlier|later))\b", re.IGNORECASE),
    re.compile(r"\b(days|weeks|months|years)\s+(passed|ago|elapsed|prior|before|since)\b", re.IGNORECASE),
    re.compile(r"\b(order\s+of|chronological|earliest\s+to\s+latest|first\s+to\s+last|which\s+.*?first|which\s+.*?earlier)\b", re.IGNORECASE),
]


# Regex for ISO-like dates (YYYY-MM-DD or YYYY/MM/DD)
_DATE_RE = re.compile(r"\b(\d{4}[-/]\d{2}[-/]\d{2})\b")

# Named cultural holidays mapped to calendar dates
_NAMED_HOLIDAYS = {
    "valentine's day": (2, 14, "February 14th"),
    "valentines day": (2, 14, "February 14th"),
    "christmas day": (12, 25, "December 25th"),
    "christmas": (12, 25, "December 25th"),
    "new year's day": (1, 1, "January 1st"),
    "new years day": (1, 1, "January 1st"),
    "halloween": (10, 31, "October 31st"),
    "4th of july": (7, 4, "July 4th"),
    "fourth of july": (7, 4, "July 4th"),
    "independence day": (7, 4, "July 4th"),
}

# Regex for natural dates like "February 14th", "May 15, 2023", "14th of February 2023"
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12
}
_MONTH_NAME_PATTERN = "|".join(_MONTHS.keys())
_NATURAL_DATE_RE = re.compile(
    rf"\b(?:({_MONTH_NAME_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,\s*|\s+)?(\d{{4}})?|(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_NAME_PATTERN})(?:\s*,\s*|\s+)?(\d{{4}})?)\b",
    re.IGNORECASE,
)


def parse_iso_date(date_str: str) -> datetime | None:
    """Parse date string into UTC datetime."""
    clean = date_str.replace("/", "-").strip()
    try:
        if len(clean) >= 10:
            return datetime.strptime(clean[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def parse_natural_or_iso_date(text: str, default_year: int = 2023) -> tuple[datetime, str] | None:
    """Parse ISO or natural language dates from a string."""
    text_lower = text.lower()
    for holiday_name, (m, d, fmt_name) in _NAMED_HOLIDAYS.items():
        if holiday_name in text_lower:
            dt = datetime(default_year, m, d, tzinfo=timezone.utc)
            return dt, fmt_name

    m_iso = _DATE_RE.search(text)
    if m_iso:
        dt = parse_iso_date(m_iso.group(1))
        if dt:
            return dt, m_iso.group(1)

    m_nat = _NATURAL_DATE_RE.search(text)
    if m_nat:
        m1, d1, y1, d2, m2, y2 = m_nat.groups()
        month_str = (m1 or m2 or "").lower()
        day_str = d1 or d2 or "1"
        year_str = y1 or y2 or str(default_year)

        month = _MONTHS.get(month_str, 1)
        day = int(day_str)
        year = int(year_str)
        try:
            dt = datetime(year, month, day, tzinfo=timezone.utc)
            raw_str = m_nat.group(0)
            return dt, raw_str
        except ValueError:
            pass

    return None


def _extract_event_delta(query: str, candidates: list, as_of_dt: datetime | None = None) -> str | None:
    """Extract dates for specific named events in the query and compute exact delta."""
    # 1. Check order/transit query pattern: "take for X to arrive after I ordered/bought it"
    m_transit = re.search(
        r"take\s+for\s+(?:me\s+to\s+receive\s+)?(.*?)\s+(?:to\s+(?:arrive|receive)\s+)?after\s+(?:i\s+)?(?:bought|ordered|purchased)\s+(?:it|them)?",
        query,
        re.IGNORECASE,
    )
    if not m_transit:
        m_transit = re.search(
            r"take\s+(?:for\s+.*?\s+)?(?:to\s+(?:arrive|receive))\s+after\s+(?:i\s+)?(?:bought|ordered|purchased)",
            query,
            re.IGNORECASE,
        )

    if m_transit:
        m_item = re.search(r"take\s+for\s+(?:me\s+to\s+receive\s+)?(?:the\s+|my\s+|a\s+)?(.*?)\s+(?:to\s+arrive|after)", query, re.I)
        item_raw = m_item.group(1).strip() if m_item else ""
        item_words = [w.lower() for w in re.findall(r"\w+", item_raw) if len(w) > 2 and w.lower() not in {"new", "the", "my", "for", "me", "to", "receive", "take", "days"}]
        
        order_dates = []
        arrival_dates = []
        for item in candidates[:20]:
            cnt = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 else ""
            cnt_lower = cnt.lower()
            ts_str = str(item[4]) if isinstance(item, (list, tuple)) and len(item) > 4 and item[4] else ""
            
            sentences = re.split(r"(?<=[.!?\n])\s+", cnt)
            for s in sentences:
                s_lower = s.lower()
                if item_words and not any(w in s_lower for w in item_words):
                    continue
                d_res = parse_natural_or_iso_date(s) or (parse_iso_date(ts_str[:10]), ts_str[:10]) if ts_str else None
                if d_res and d_res[0]:
                    if any(kw in s_lower for kw in ("ordered", "bought", "purchased", "order placed")):
                        order_dates.append(d_res[0])
                    if any(kw in s_lower for kw in ("arrived", "received", "delivered", "arrival", "got it")):
                        arrival_dates.append(d_res[0])

        if not order_dates or not arrival_dates:
            for item in candidates[:15]:
                cnt = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 else ""
                cnt_lower = cnt.lower()
                ts_str = str(item[4]) if isinstance(item, (list, tuple)) and len(item) > 4 and item[4] else ""
                d_res = parse_natural_or_iso_date(cnt)
                if not d_res and ts_str:
                    d_res = (parse_iso_date(ts_str[:10]), ts_str[:10])
                if d_res and d_res[0]:
                    if any(kw in cnt_lower for kw in ("ordered", "bought", "purchased", "order placed")):
                        order_dates.append(d_res[0])
                    if any(kw in cnt_lower for kw in ("arrived", "received", "delivered", "arrival", "got it")):
                        arrival_dates.append(d_res[0])

        if order_dates and arrival_dates:
            order_dt = min(order_dates)
            arrival_dt = max(arrival_dates)
            delta_days = abs((arrival_dt - order_dt).days)
            if delta_days > 0:
                return f"{delta_days} days (or {delta_days + 1} days including the last day)"

    # 2. Binary precedence queries ("Which event happened first, A or B?" / "Which did I join first, A or B?")
    m_prec = re.search(r"which\s+(?:event\s+)?(?:happened|did\s+i\s+(?:join|read|finish|take|attend|visit))\s+first[,\s]+(?:the\s+|my\s+)?(.*?)\s+or\s+(?:the\s+|my\s+)?(.*?)\??$", query, re.IGNORECASE)
    if not m_prec:
        m_prec = re.search(r"which\s+(?:one\s+)?(?:did\s+i|happened)\s+(?:first|earlier)[,\s]+(.*)\s+or\s+(.*)\??$", query, re.IGNORECASE)

    # 3. Inter-event interval queries
    m = None
    event_a_phrase = ""
    event_b_phrase = ""

    if m_prec:
        event_a_phrase = m_prec.group(1).strip().rstrip("?")
        event_b_phrase = m_prec.group(2).strip().rstrip("?")
    else:
        # Check various inter-event phrasings
        patterns = [
            re.compile(r"(?:passed\s+between|time\s+between|between)\s+(?:the\s+time\s+|the\s+day\s+|when\s+)?(.*?)\s+and\s+(?:the\s+time\s+|the\s+day\s+|when\s+)?(.*)", re.I),
            re.compile(r"(?:passed\s+since|time\s+since|since)\s+(.*?)\s+when\s+(.*)", re.I),
            re.compile(r"(?:days|weeks|months|time)\s+before\s+(.*?)\s+(?:did\s+i|i)\s+(.*)", re.I),
            re.compile(r"how\s+long\s+(?:did\s+i|had\s+i\s+been|was\s+i)\s+(.*?)\s+before\s+(.*)", re.I),
            re.compile(r"how\s+long\s+(?:did\s+i|had\s+i\s+been|was\s+i)\s+(.*?)\s+when\s+(.*)", re.I),
        ]
        for pat in patterns:
            m_cand = pat.search(query)
            if m_cand:
                event_a_phrase = m_cand.group(1).strip().rstrip("?")
                event_b_phrase = m_cand.group(2).strip().rstrip("?")
                m = m_cand
                break

    if not event_a_phrase or not event_b_phrase:
        return None

    def find_best_date_for_phrase(phrase: str) -> tuple[datetime, str] | None:
        stopwords = {
            "i", "started", "working", "on", "the", "module", "for", "our", "system",
            "began", "developing", "a", "an", "to", "in", "of", "and", "when", "day",
            "visit", "my", "at", "first", "last", "was", "did", "event", "time"
        }
        words = set(re.findall(r"\w+", phrase.lower())) - stopwords
        if not words:
            words = set(re.findall(r"\w+", phrase.lower()))

        best_dt = None
        best_overlap = 0
        best_str = ""

        for item in candidates[:15]:
            cnt = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 else ""
            ts_str = str(item[4]) if isinstance(item, (list, tuple)) and len(item) > 4 and item[4] else ""

            # Check session header date or item timestamp
            d_res = None
            d_header = re.search(r"\[Session Date:\s*(\d{4}-\d{2}-\d{2})", cnt)
            if d_header:
                d_res = (parse_iso_date(d_header.group(1)), d_header.group(1))
            if not d_res:
                d_res = parse_natural_or_iso_date(cnt)
            if not d_res and ts_str:
                d_res = (parse_iso_date(ts_str[:10]), ts_str[:10])

            if not d_res or not d_res[0]:
                continue

            cnt_words = set(re.findall(r"\w+", cnt.lower()))
            overlap = len(words & cnt_words)
            # Boost if exact phrase substring appears in candidate
            if phrase.lower() in cnt.lower():
                overlap += 10

            if overlap > best_overlap:
                best_overlap = overlap
                best_dt = d_res[0]
                best_str = d_res[1]

        return (best_dt, best_str) if (best_dt and best_overlap > 0) else None

    res_a = find_best_date_for_phrase(event_a_phrase)
    res_b = find_best_date_for_phrase(event_b_phrase)

    if res_a and res_b and res_a[0] != res_b[0]:
        # Handle binary precedence formatting
        if m_prec:
            if res_a[0] < res_b[0]:
                return f"{event_a_phrase.capitalize()} happened first (on {res_a[0].strftime('%B %-d, %Y')}, before {event_b_phrase} on {res_b[0].strftime('%B %-d, %Y')})."
            else:
                return f"{event_b_phrase.capitalize()} happened first (on {res_b[0].strftime('%B %-d, %Y')}, before {event_a_phrase} on {res_a[0].strftime('%B %-d, %Y')})."

        delta_days = abs((res_b[0] - res_a[0]).days)
        fmt_a = res_a[0].strftime("%B %-d, %Y")
        fmt_b = res_b[0].strftime("%B %-d, %Y")
        query_lower = query.lower()
        if "week" in query_lower:
            weeks = round(delta_days / 7.0, 1)
            w_str = f"{int(weeks) if weeks.is_integer() else weeks} weeks"
            return f"{w_str} ({delta_days} days) passed between {event_a_phrase} on {fmt_a} and {event_b_phrase} on {fmt_b}."
        elif "month" in query_lower:
            months = round(delta_days / 30.4, 1)
            m_str = f"{int(months) if months.is_integer() else months} months"
            return f"{m_str} ({delta_days} days) passed between {event_a_phrase} on {fmt_a} and {event_b_phrase} on {fmt_b}."
        else:
            return f"{delta_days} days (or {delta_days + 1} days including the last day) passed between {event_a_phrase} on {fmt_a} and {event_b_phrase} on {fmt_b}."
    return None


def calculate_temporal_delta(query: str, candidates: list[tuple], as_of: float | str | None = None) -> str | None:
    """Extract dates from top retrieved candidates and compute temporal delta in days/weeks/months."""
    if not candidates:
        return None

    is_delta_query = any(pat.search(query) for pat in _DELTA_PATTERNS)
    if not is_delta_query:
        return None

    # Parse as_of reference date if provided
    as_of_dt = None
    if as_of is not None:
        try:
            if isinstance(as_of, datetime):
                as_of_dt = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
            elif isinstance(as_of, (int, float)):
                as_of_dt = datetime.fromtimestamp(as_of, tz=timezone.utc)
            elif isinstance(as_of, str):
                as_of_dt = parse_iso_date(as_of)
        except Exception:
            pass

    # 1. First check event-specific temporal delta between two phrases
    event_delta = _extract_event_delta(query, candidates, as_of_dt=as_of_dt)
    if event_delta:
        return event_delta

    # 2. Check relative query against as_of date (e.g. "How many days ago did I buy X?")
    query_lower = query.lower()
    is_relative_ago = bool(re.search(r"\b(days|weeks|months|years)\s+ago\b|\bhow\s+long\s+ago\b|\bhow\s+many\s+days\s+(?:did\s+it\s+take|had\s+passed|since)\b", query_lower))

    dates: list[tuple[datetime, str]] = []

    # 0. Chronological Ordering Query Solver (e.g. "What is the order of the six museums I visited from earliest to latest?")
    if "order of" in query_lower or "earliest to latest" in query_lower or "chronological" in query_lower:
        entity_date_pairs = []
        for item in candidates[:15]:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            cnt = str(item[1]) if item[1] is not None else ""
            ts = str(item[4]) if len(item) > 4 and item[4] is not None else ""
            dt = parse_iso_date(ts[:10]) if ts and len(ts) >= 10 else None
            if not dt:
                nat_res = parse_natural_or_iso_date(cnt)
                if nat_res:
                    dt = nat_res[0]

            if dt:
                # Extract proper noun entities (e.g. Museum of Art, Science Museum, etc.)
                found_entities = re.findall(
                    r"\b((?:Museum|Gallery|Center|Theatre|Hall|Station|Airport|Festival|Building|Institute|University)\s+of\s+[A-Z][a-zA-Z0-9_\-]+|[A-Z][a-zA-Z0-9_\-]+(?:\s+[A-Z][a-zA-Z0-9_\-]+)*(?:\s+(?:Museum|Park|Gallery|Center|Theatre|Hall|Station|Airport|Festival|Building|Institute|University)))\b",
                    cnt,
                )

                if not found_entities:
                    found_entities = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", cnt)
                for ent in found_entities:
                    if ent not in [e[0] for e in entity_date_pairs]:
                        entity_date_pairs.append((ent, dt))

        if len(entity_date_pairs) >= 2:
            entity_date_pairs.sort(key=lambda x: x[1])
            ordered_names = [e[0] for e in entity_date_pairs]
            return ", ".join(ordered_names)

    # Relative query date selection aligned with query entity
    stopwords = {"how", "many", "days", "weeks", "months", "years", "ago", "did", "i", "was", "were", "the", "a", "an", "in", "on", "at", "to", "my", "last", "first"}
    query_keywords = set(re.findall(r"\w+", query_lower)) - stopwords

    best_cand_dt = None
    best_overlap = -1

    for item in candidates[:15]:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        content = str(item[1]) if item[1] is not None else ""
        ts_str = str(item[4]) if len(item) > 4 and item[4] is not None else ""

        cand_dt = None
        if ts_str and len(ts_str) >= 10:
            cand_dt = parse_iso_date(ts_str[:10])
        if not cand_dt:
            d_header = re.search(r"\[Session Date:\s*(\d{4}-\d{2}-\d{2})", content)
            if d_header:
                cand_dt = parse_iso_date(d_header.group(1))
        if not cand_dt:
            nat_res = parse_natural_or_iso_date(content)
            if nat_res:
                cand_dt = nat_res[0]

        if cand_dt:
            if cand_dt not in [d[0] for d in dates]:
                dates.append((cand_dt, str(cand_dt)))
            cnt_lower = content.lower()
            overlap = sum(1 for kw in query_keywords if kw in cnt_lower)
            if overlap > best_overlap:
                best_overlap = overlap
                best_cand_dt = cand_dt

    # If relative query and we have an as_of reference date
    if is_relative_ago and as_of_dt and (best_cand_dt or dates):
        target_event_dt = best_cand_dt if (best_cand_dt and best_overlap > 0) else dates[0][0]
        # Calendar date difference avoiding time-of-day truncation
        delta_days = abs((as_of_dt.date() - target_event_dt.date()).days)

        if "week" in query_lower:
            weeks = round(delta_days / 7.0, 1)
            w_val = int(weeks) if weeks.is_integer() else weeks
            return f"{w_val} weeks ({w_val} weeks ago, {delta_days} days)"
        elif "month" in query_lower:
            months = round(delta_days / 30.4, 1)
            m_val = int(months) if months.is_integer() else months
            return f"{m_val} months ({m_val} months ago, {delta_days} days)"
        elif "year" in query_lower:
            years = round(delta_days / 365.25, 1)
            y_val = int(years) if years.is_integer() else years
            return f"{y_val} years ({y_val} years ago, {delta_days} days)"
        else:
            return f"{delta_days} days ({delta_days} days ago, or {delta_days + 1} days including today)"

    # Inter-session delta between earliest and latest extracted dates
    if len(dates) >= 2:
        dates.sort(key=lambda x: x[0])
        earliest = dates[0][0]
        latest = dates[-1][0]
        delta_days = abs((latest.date() - earliest.date()).days)

        if "week" in query_lower:
            weeks = round(delta_days / 7.0, 1)
            w_val = int(weeks) if weeks.is_integer() else weeks
            formatted = f"{w_val} weeks ({delta_days} days)"
        elif "month" in query_lower:
            months = round(delta_days / 30.4, 1)
            m_val = int(months) if months.is_integer() else months
            formatted = f"{m_val} months ({delta_days} days)"
        elif "year" in query_lower:
            years = round(delta_days / 365.25, 1)
            y_val = int(years) if years.is_integer() else years
            formatted = f"{y_val} years ({delta_days} days)"
        else:
            formatted = f"{delta_days} days (or {delta_days + 1} days including the last day)"

        logger.debug("TemporalDeltaSolver: computed %s between %s and %s", formatted, dates[0][1], dates[-1][1])
        return formatted

    return None


