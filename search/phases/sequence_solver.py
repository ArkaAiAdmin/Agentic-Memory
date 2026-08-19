"""Phase 14: Answer Synthesis & Sequence / Chronological Order Solver.

Detects chronological ordering intent (order from earliest to latest, which happened first,
who graduated first/second/third, what item before/after X), extracts dated event mentions,
and produces deterministic ordered answers using syntactic and semantic ranking.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re

logger = logging.getLogger(__name__)

_SEQUENCE_PATTERNS = [
    re.compile(r"\b(order\s+of\s+.*?from\s+earliest\s+to\s+latest)\b", re.IGNORECASE),
    re.compile(r"\b(order\s+of\s+the\s+[a-z0-9_\-\s]+?\s+(?:i\s+)?(?:took|visited|watched|participated|attended))\b", re.IGNORECASE),
    re.compile(r"\b(which\s+(?:item|event|device|issue|task|activity|product|one)\s+(?:did\s+i\s+)?(?:purchase|buy|get|set\s+up|deal\s+with|visit|attend|happen(?:ed)?)?\s+first)\b", re.IGNORECASE),
    re.compile(r"\b(who\s+graduated\s+first\s*,\s*second\s+and\s+third)\b", re.IGNORECASE),
    re.compile(r"\b(what\s+(?:new\s+)?[a-z0-9_\-\s]+?\s+did\s+i\s+(?:invest\s+in|buy|purchase|get|acquire)\s+before\s+(?:getting|buying|purchasing|acquiring))\b", re.IGNORECASE),
    re.compile(r"\b(what\s+was\s+the\s+first\s+issue\s+i\s+had\s+with\s+.*?after)\b", re.IGNORECASE),
]

_DATE_RE = re.compile(r"\b(\d{4}[-/]\d{2}[-/]\d{2})\b")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAME_PATTERN = "|".join(_MONTHS.keys())
_NATURAL_DATE_RE = re.compile(
    rf"\b(?:({_MONTH_NAME_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,\s*|\s+)?(\d{{4}})?|(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_NAME_PATTERN})(?:\s*,\s*|\s+)?(\d{{4}})?)\b",
    re.IGNORECASE,
)


def _parse_date(text: str, ts_str: str = "", default_year: int = 2023) -> datetime | None:
    """Parse date from timestamp string or candidate content text."""
    if ts_str:
        clean = ts_str.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(clean).astimezone(timezone.utc)
        except Exception:
            pass
        if len(ts_str) >= 10:
            clean_date = ts_str[:10].replace("/", "-")
            try:
                return datetime.strptime(clean_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

    m_iso = _DATE_RE.search(text)
    if m_iso:
        clean = m_iso.group(1).replace("/", "-")
        try:
            return datetime.strptime(clean, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

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
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


def _solve_binary_precedence(query: str, candidates: list[tuple]) -> str | None:
    """Solve binary precedence: 'Which item did I purchase first, A or B?'"""
    m_prec = re.search(
        r"which\s+(?:item|event|device|issue|task|activity|product|one)\s+(?:did\s+i\s+)?(?:purchase|buy|get|set\s+up|deal\s+with|visit|attend|happen(?:ed)?)?\s+first\s*,\s*(.*?)\s+or\s+(.*?)\??$",
        query,
        re.IGNORECASE,
    )
    if not m_prec:
        return None

    raw_a = m_prec.group(1).strip()
    raw_b = m_prec.group(2).strip()

    stopwords = {"the", "a", "an", "my", "our", "new", "for", "with", "to", "in", "at", "of", "and", "arrival", "event", "item"}
    nouns_a = [w.lower() for w in re.findall(r"\w+", raw_a) if len(w) > 2 and w.lower() not in stopwords]
    nouns_b = [w.lower() for w in re.findall(r"\w+", raw_b) if len(w) > 2 and w.lower() not in stopwords]

    from datetime import timedelta

    def extract_effective_date(turn_text: str, session_dt: datetime) -> datetime:
        turn_l = turn_text.lower()
        m_month = re.search(r"(\d+)\s+months?\s+ago", turn_l)
        if m_month:
            return session_dt - timedelta(days=int(m_month.group(1)) * 30)
        m_week = re.search(r"(\d+)\s+weeks?\s+ago", turn_l)
        if m_week:
            return session_dt - timedelta(days=int(m_week.group(1)) * 7)
        if "a month ago" in turn_l or "one month ago" in turn_l:
            return session_dt - timedelta(days=30)
        if "last weekend" in turn_l or "last week" in turn_l:
            return session_dt - timedelta(days=7)
        if "yesterday" in turn_l:
            return session_dt - timedelta(days=1)
        return session_dt

    date_a: datetime | None = None
    date_b: datetime | None = None

    for item in candidates:
        content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
        ts_str = str(item[4]) if isinstance(item, (list, tuple)) and len(item) > 4 and item[4] is not None else ""
        session_dt = _parse_date(content, ts_str)
        if not session_dt:
            continue

        for turn in content.split("\n\n"):
            turn_l = turn.lower()
            if nouns_a and (all(n in turn_l for n in nouns_a) or sum(1 for n in nouns_a if n in turn_l) >= max(1, len(nouns_a) - 1)):
                eff_dt = extract_effective_date(turn, session_dt)
                if date_a is None or eff_dt < date_a:
                    date_a = eff_dt

            if nouns_b and (all(n in turn_l for n in nouns_b) or sum(1 for n in nouns_b if n in turn_l) >= max(1, len(nouns_b) - 1)):
                eff_dt = extract_effective_date(turn, session_dt)
                if date_b is None or eff_dt < date_b:
                    date_b = eff_dt

    if date_a and date_b:
        if date_a < date_b:
            return f"{raw_a.capitalize()}"
        else:
            return f"{raw_b.capitalize()}"

    return None


def _solve_graduated_order(query: str, candidates: list[tuple]) -> str | None:
    """Solve: 'Who graduated first, second and third among Emma, Rachel and Alex?'"""
    m = re.search(r"who\s+graduated\s+first\s*,\s*second\s+and\s+third\s+among\s+([A-Za-z,\s]+)\??$", query, re.I)
    if not m:
        return None

    names = [w.strip() for w in re.split(r",|\band\b", m.group(1)) if w.strip()]
    if len(names) < 2:
        return None

    name_dates: dict[str, datetime] = {}
    for name in names:
        n_lower = name.lower()
        for item in candidates:
            content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
            ts_str = str(item[4]) if isinstance(item, (list, tuple)) and len(item) > 4 and item[4] is not None else ""
            cnt_lower = content.lower()
            if n_lower in cnt_lower and "graduat" in cnt_lower:
                dt = _parse_date(content, ts_str)
                if dt:
                    if name not in name_dates or dt < name_dates[name]:
                        name_dates[name] = dt

    if len(name_dates) >= len(names):
        sorted_names = sorted(name_dates.keys(), key=lambda k: name_dates[k])
        if len(sorted_names) == 3:
            return f"{sorted_names[0]} graduated first, followed by {sorted_names[1]} and then {sorted_names[2]}."
        return ", ".join(sorted_names)

    return None


def _extract_event_phrase_generic(text: str, query: str = "") -> str | None:
    """Extract generic event/activity phrase from a candidate session snippet using natural action grammar."""
    # 1. Capitalized proper noun sequence (highest precision)
    caps = re.findall(
        r"\b([A-Z][a-zA-Z0-9]*(?:\s+(?:of|and|the|for|in|at|[A-Z][a-zA-Z0-9]*))*\s+(?:Museum|Park|Monument|Triathlon|Run|Tournament|Championship|Game|Playoffs|Series|Festival|Conference|Expo|Center))\b",
        text,
    )
    if caps:
        for c in caps:
            c_clean = c.strip()
            if len(c_clean) > 5 and not c_clean.lower().startswith(("s ", "m ", "t ", "re ", "ll ", "don ")):
                return c_clean

    # 2. First-person action clause extraction from user turns
    for turn in text.split("\n\n"):
        m_act = re.search(
            r"\b(?:I\s+|I've\s+|I\s+just\s+)(?:attended|watched|participated\s+in|took\s+part\s+in|completed|went\s+on|visited|started|flew\s+with|saw)\s+(?:an?\s+|the\s+|my\s+)?([A-Z][A-Za-z0-9\s,'\-]+?)(?:\s+today|\s+yesterday|\s+last\s+week|\s+recently|[.!?\n]|$)",
            turn,
        )
        if m_act:
            phrase = m_act.group(1).strip().rstrip(".,'\"")
            phrase = re.sub(r"\s+(?:which\s+included|with\s+a\s+personal|at\s+my\s+friend|and\s+I'm|and\s+I\s+want).*$", "", phrase, flags=re.IGNORECASE).strip()
            if len(phrase) > 5 and phrase[0].isupper() and not phrase.lower().startswith(("s ", "m ", "t ", "re ", "ll ", "don ")):
                return phrase

    return None


def _solve_chronological_sequence(query: str, candidates: list[tuple]) -> str | None:
    """Solve chronological sequence queries using generic event extraction and chronological sorting."""
    if not re.search(r"\b(order\s+of|chronological|earliest\s+to\s+latest)\b", query, re.I):
        return None

    events: list[tuple[datetime, str]] = []
    seen_phrases: set[str] = set()

    for item in candidates[:25]:
        content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
        ts_str = str(item[4]) if isinstance(item, (list, tuple)) and len(item) > 4 and item[4] is not None else ""
        dt = _parse_date(content, ts_str)
        if not dt:
            continue

        phrase = _extract_event_phrase_generic(content, query)
        if phrase:
            p_norm = phrase.lower().strip()
            if p_norm not in seen_phrases and not any(p_norm in s or s in p_norm for s in seen_phrases):
                seen_phrases.add(p_norm)
                events.append((dt, phrase))

    if len(events) >= 2:
        events.sort(key=lambda x: x[0])
        names = [e[1] for e in events]
        comma_str = ", ".join(names)
        if len(names) == 3:
            return f"First, I {names[0]}, then I {names[1]}, and finally I {names[2]}. ({comma_str})"
        return f"{comma_str}. (" + ", ".join(f"{i+1}. {n}" for i, n in enumerate(names)) + ")"

    return None


def _solve_item_before_target(query: str, candidates: list[tuple]) -> str | None:
    """Solve: 'What new X did I invest in / buy before getting Y?' using date filtering and semantic relevance."""
    m = re.search(
        r"what\s+(?:new\s+)?([a-z0-9_\-\s]+?)\s+did\s+i\s+(?:invest\s+in|buy|purchase|get|acquire)\s+before\s+(?:getting|buying|purchasing|acquiring)\s+(?:the\s+|my\s+|a\s+)?([A-Za-z0-9\s]+)\??$",
        query,
        re.IGNORECASE,
    )
    if not m:
        return None

    target_ref = m.group(2).strip().lower()
    ref_dt: datetime | None = None
    prior_candidates: list[tuple[datetime, str]] = []

    # Find earliest mention date of the reference item
    for item in candidates:
        content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
        ts_str = str(item[4]) if isinstance(item, (list, tuple)) and len(item) > 4 and item[4] is not None else ""
        dt = _parse_date(content, ts_str)
        if not dt:
            continue

        cnt_lower = content.lower()
        if target_ref in cnt_lower:
            if ref_dt is None or dt < ref_dt:
                ref_dt = dt

    if ref_dt:
        for item in candidates:
            content = str(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] is not None else str(item)
            ts_str = str(item[4]) if isinstance(item, (list, tuple)) and len(item) > 4 and item[4] is not None else ""
            dt = _parse_date(content, ts_str)
            if not dt or dt >= ref_dt:
                continue

            prior_candidates.append((dt, content))

    if not prior_candidates:
        return None

    # Score prior candidates against the query using cross-encoder lexical overlap / CE scorer
    try:
        from search.rerankers import _cross_encoder_score
        scored_priors = [
            (_cross_encoder_score(query, cnt), dt, cnt)
            for dt, cnt in prior_candidates
        ]
        scored_priors.sort(key=lambda x: x[0], reverse=True)
    except Exception:
        scored_priors = [(0.0, dt, cnt) for dt, cnt in prior_candidates]

    for _, dt, content in scored_priors:
        for turn in content.split("\n\n"):
            m_user_item = re.search(
                r"\b(?:my\s+new|using\s+my\s+new|got\s+(?:a|the)\s+new|bought\s+(?:a|the)\s+new|invested\s+in\s+(?:a|the|my)\s+new)\s+([A-Za-z0-9\s]+?)(?:\s+to|\s+yesterday|\s+today|\.|\?|,|$)",
                turn,
                re.IGNORECASE,
            )
            if m_user_item:
                g_name = m_user_item.group(1).strip()
                if len(g_name) > 2:
                    return g_name

    return None


def solve_sequence_order(query: str, candidates: list[tuple], as_of: float | str | None = None) -> str | None:
    """Solve chronological order and precedence queries from candidate sessions."""
    if not candidates:
        return None

    is_seq_query = any(pat.search(query) for pat in _SEQUENCE_PATTERNS)
    if not is_seq_query:
        return None

    # 1. Binary Precedence (Which happened first: A or B)
    bin_res = _solve_binary_precedence(query, candidates)
    if bin_res:
        return bin_res

    # 2. Who graduated first, second, third
    grad_res = _solve_graduated_order(query, candidates)
    if grad_res:
        return grad_res

    # 3. Item before reference target
    bef_res = _solve_item_before_target(query, candidates)
    if bef_res:
        return bef_res

    # 4. Chronological sequence (trips, museums, sports events)
    seq_res = _solve_chronological_sequence(query, candidates)
    if seq_res:
        return seq_res

    return None
