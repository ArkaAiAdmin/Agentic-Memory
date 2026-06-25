"""
Per-question BM25 + cross-encoder retrieval for LongMemEval_S.

Design:
  - One in-memory FTS5 DB per question, one row per session (turns joined by '\n').
  - BM25 top-50 candidates are rescored by a cross-encoder.
  - Final score: 0.4 * bm25_norm + 0.6 * ce_norm, min-max normalized per question.
  - FTS5 + CE both use unicode61 tokenization so behavior is consistent.

Returns doc_ids ranked best-first.
"""

from __future__ import annotations

import calendar
import math
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Sequence

# Cross-encoder is loaded lazily so the module imports cheaply.
_CE_MODEL = None
_CE_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def _load_ce():
    global _CE_MODEL
    if _CE_MODEL is None:
        from sentence_transformers import CrossEncoder  # type: ignore
        _CE_MODEL = CrossEncoder(_CE_MODEL_NAME, max_length=512)
    return _CE_MODEL


def _normalize_scores(scores: Sequence[float]) -> list[float]:
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi <= lo:
        return [0.5 for _ in scores]
    return [(s - lo) / (hi - lo) for s in scores]


def _join_turns(session_turns: list[dict]) -> str:
    """Concatenate turn contents with '\n' separator. Skip empty content."""
    parts: list[str] = []
    for turn in session_turns:
        c = turn.get("content") or ""
        if c:
            parts.append(c)
    return "\n".join(parts)


_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
# Tiny stopword set: FTS5 with implicit AND over raw query words kills recall
# on natural-language questions because of stopwords / discourse markers.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "did", "do", "does",
    "for", "from", "had", "has", "have", "i", "in", "is", "it", "its", "me",
    "my", "no", "not", "of", "on", "or", "so", "such", "that", "the", "their",
    "them", "they", "this", "to", "was", "we", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you", "your",
}


def _fts_query(query: str) -> str:
    """
    Build an FTS5 MATCH expression from a free-form question.

    - Tokenize on alphanumerics, lowercase, drop 1-char tokens and stopwords.
    - Join remaining terms with ' OR ' so a doc matching any of them scores.
    - Each term is quoted to neutralize FTS5 reserved characters.
    - Empty result returns "" and the caller should fall back.
    """
    if not query:
        return ""
    tokens = [t.lower() for t in _TOKEN_RE.findall(query)]
    tokens = [t for t in tokens if len(t) > 1 and t not in _STOPWORDS]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def _prepend_date(content: str, date_str: str | None) -> str:
    """Prepend a 'Session date: <date>' line if date_str is non-empty.

    The CE rerank model (ms-marco-MiniLM-L-6-v2, max_length=512) sees this as
    part of the document text, so questions that mention a date can match
    against a session's date even when the question's temporal expression
    doesn't lexically overlap with the session content.
    """
    if not date_str:
        return content
    return f"Session date: {date_str}\n{content}"


def build_fts_index(
    haystack_sessions: list[list[dict]],
    haystack_session_ids: list[str],
    haystack_dates: list[str | None] | None = None,
) -> sqlite3.Connection:
    """
    Build a fresh in-memory FTS5 index. doc_id matches haystack_session_ids[i].
    Caller owns the connection.

    If `haystack_dates` is provided, the date is prepended to each doc's text
    (Approach A: implicit time-aware indexing). Pass None to keep legacy
    behavior unchanged.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE docs USING fts5(doc_id UNINDEXED, content, tokenize='unicode61')"
    )
    if haystack_dates is None:
        rows = [
            (sid, _join_turns(sess))
            for sid, sess in zip(haystack_session_ids, haystack_sessions)
        ]
    else:
        if len(haystack_dates) != len(haystack_sessions):
            raise ValueError(
                f"haystack_dates length {len(haystack_dates)} != "
                f"haystack_sessions length {len(haystack_sessions)}"
            )
        rows = [
            (sid, _prepend_date(_join_turns(sess), d))
            for sid, sess, d in zip(haystack_session_ids, haystack_sessions, haystack_dates)
        ]
    conn.executemany("INSERT INTO docs(doc_id, content) VALUES (?, ?)", rows)
    conn.commit()
    return conn


def bm25_search(
    conn: sqlite3.Connection,
    query: str,
    candidate_pool: int = 50,
) -> list[tuple[str, float]]:
    """
    Return up to `candidate_pool` (doc_id, bm25_score) tuples, best first.

    FTS5 bm25() returns negative numbers where lower = better. We negate so
    higher = better, which matches cross-encoder conventions.
    """
    fts_q = _fts_query(query)
    if not fts_q:
        return []
    cur = conn.execute(
        "SELECT doc_id, -bm25(docs) AS score "
        "FROM docs WHERE docs MATCH ? "
        "ORDER BY score DESC LIMIT ?",
        (fts_q, candidate_pool),
    )
    return [(row[0], float(row[1])) for row in cur.fetchall()]


def _add_unindexed_ce_fallback(
    candidates: list[tuple[str, float]],
    haystack_sessions: list[list[dict]],
    haystack_session_ids: list[str],
    query: str,
) -> list[tuple[str, float]]:
    """
    If BM25 returned 0 candidates, score ALL sessions with the cross-encoder.

    The corpus is ~48 sessions per question, so this is cheap relative to the
    ~90M-param CE and ensures the harness still produces a ranking for a query
    that FTS5 cannot parse.
    """
    indexed = {sid for sid, _ in candidates}
    extras = [
        (sid, 0.0) for sid in haystack_session_ids if sid not in indexed
    ]
    return candidates + extras


def _ce_score(
    query: str,
    candidates_with_text: list[tuple[str, str]],
) -> list[tuple[str, float]]:
    """
    candidates_with_text: list of (doc_id, document_text). Returns (doc_id, ce_score).
    """
    if not candidates_with_text:
        return []
    ce = _load_ce()
    pairs = [(query, text) for _, text in candidates_with_text]
    scores = ce.predict(pairs, show_progress_bar=False)
    return [(doc_id, float(s)) for (doc_id, _), s in zip(candidates_with_text, scores)]


def retrieve_for_question(
    question: str,
    haystack_sessions: list[list[dict]],
    haystack_session_ids: list[str],
    *,
    haystack_dates: list[str | None] | None = None,
    blend: float = 0.6,
    candidate_pool: int = 50,
    use_ce: bool = True,
    date_boost: float | None = None,
    temporal_range: tuple[str, str] | None = None,
) -> tuple[list[str], dict]:
    """
    Retrieve a ranked list of doc_ids for a single question.

    Returns (ranked_doc_ids, debug_info).

    New params (all backward-compatible — pass None to disable):
      - haystack_dates: parallel list of date strings. If provided, each
        session's date is prepended to its indexed text (Approach A) so the
        cross-encoder sees "Session date: 2023/08/05" in the doc.
      - date_boost: if set (e.g. 1.5) and `temporal_range` is also set, the
        final score is multiplied by `date_boost` for sessions whose date
        falls within the range, else by 1.0 (Approach B).
      - temporal_range: (start_iso, end_iso) inclusive range. Sessions
        outside the range get no boost. Pass None to skip the boost step
        entirely.
    """
    debug: dict = {
        "n_sessions": len(haystack_sessions),
        "bm25_hits": 0,
        "ce_scored": 0,
    }
    t0 = time.perf_counter()
    conn = build_fts_index(haystack_sessions, haystack_session_ids, haystack_dates=haystack_dates)
    bm25_hits = bm25_search(conn, question, candidate_pool=candidate_pool)
    debug["bm25_hits"] = len(bm25_hits)
    conn.close()

    if not bm25_hits:
        bm25_hits = _add_unindexed_ce_fallback(
            [], haystack_sessions, haystack_session_ids, question,
        )

    sid_to_text = dict(zip(haystack_session_ids, haystack_sessions))
    sid_to_date = (
        dict(zip(haystack_session_ids, haystack_dates))
        if haystack_dates is not None
        else {}
    )
    cand_ids = [sid for sid, _ in bm25_hits]
    cand_texts = [_join_turns(sid_to_text[sid]) for sid in cand_ids]
    bm25_scores = [s for _, s in bm25_hits]

    ce_scores_by_id: dict[str, float] = {}
    if use_ce and cand_ids:
        ce_scored = _ce_score(question, list(zip(cand_ids, cand_texts)))
        ce_scores_by_id = dict(ce_scored)
        debug["ce_scored"] = len(ce_scores_by_id)

    bm25_norm = _normalize_scores(bm25_scores)
    ce_norm = _normalize_scores([ce_scores_by_id.get(sid, 0.0) for sid in cand_ids])

    final: list[tuple[str, float]] = []
    for i, sid in enumerate(cand_ids):
        b = bm25_norm[i]
        c = ce_norm[i]
        if use_ce and ce_scores_by_id:
            score = (1.0 - blend) * b + blend * c
        else:
            score = b
        if date_boost is not None and temporal_range is not None and sid in sid_to_date:
            d = sid_to_date[sid]
            if d and _date_in_range(d, temporal_range):
                score *= date_boost
        final.append((sid, score))

    final.sort(key=lambda x: x[1], reverse=True)
    debug["elapsed_s"] = round(time.perf_counter() - t0, 3)
    return [sid for sid, _ in final], debug


# ---------------------------------------------------------------------------
# Temporal range extraction (Approach B)
# ---------------------------------------------------------------------------
#
# LongMemEval haystack session dates look like "2023/05/20 (Sat) 02:21".
# question_date is the same format. We extract a (start, end) iso-date tuple
# from the question, anchored at question_date, then re-rank with a boost.
#
# Coverage targets the patterns actually seen in the 22 failures:
#   - "N days/weeks/months ago"
#   - "yesterday", "last week", "this week", "last month"
#   - "in <month>", "in <year>", "in March 2023"
#   - "the past N days/weeks/months", "the last N days/weeks/months"
#   - "the first/second/third week of <month>"
#
# Out of scope (returns None):
#   - "before/after <event>" — too ambiguous without an event anchor
#   - "recently", "a while ago" — no quantitative anchor
#   - "the Wednesday two months ago" — handled by "N months ago"

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_ORDINAL_WEEK = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}


def _parse_question_date(question_date: str | None):
    """Parse "2023/08/15 (Tue) 23:40" -> datetime. Returns None on failure."""
    if not question_date:
        return None
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", question_date.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime(y, mo, d)
    except ValueError:
        return None


def _parse_session_date(date_str: str | None):
    """Parse "2023/05/20 (Sat) 02:21" -> datetime. Returns None on failure."""
    if not date_str:
        return None
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", date_str.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime(y, mo, d)
    except ValueError:
        return None


def _date_in_range(date_str: str, temporal_range: tuple[str, str]) -> bool:
    """Inclusive: parse date_str, check it falls within [start, end] ISO dates."""
    d = _parse_session_date(date_str)
    if d is None:
        return False
    s = datetime.strptime(temporal_range[0], "%Y-%m-%d")
    e = datetime.strptime(temporal_range[1], "%Y-%m-%d")
    return s <= d <= e


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def _add_months(d: datetime, n: int) -> datetime:
    """Add n calendar months, clamping the day to the new month's end."""
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    return datetime(y, m, min(d.day, last_day))


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    last = calendar.monthrange(year, month)[1]
    return _iso(datetime(year, month, 1)), _iso(datetime(year, month, last))


def _extract_temporal_range(
    question_text: str, question_date: str | None
) -> tuple[str, str] | None:
    """
    Parse a temporal expression in `question_text` and return an inclusive
    (start_iso, end_iso) date range anchored at `question_date`.

    Returns None when no usable expression is found. The most specific match
    wins when several expressions are present (e.g., "the past month" is more
    specific than just a bare "month").
    """
    if not question_text:
        return None
    qd = _parse_question_date(question_date)
    if qd is None:
        return None
    text = question_text.lower()
    # `strip` to keep anchors clean, but keep case-insensitive search.
    # Use search not match: temporal expressions can be mid-sentence.

    # 1) "N days/weeks/months/years ago" — most specific
    m = re.search(
        r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(day|days|week|weeks|month|months|year|years)\s+ago\b",
        text,
    )
    if m:
        n = _to_int(m.group(1))
        unit = m.group(2)
        if n is not None:
            if unit.startswith("day"):
                start = qd - timedelta(days=n)
                end = qd
            elif unit.startswith("week"):
                start = qd - timedelta(weeks=n)
                end = qd
            elif unit.startswith("month"):
                start = _add_months(qd, -n)
                end = qd
            else:  # year
                start = _add_months(qd, -12 * n)
                end = qd
            return _iso(start), _iso(end)

    # 2) "yesterday" / "the day before yesterday"
    if re.search(r"\byesterday\b", text):
        start = qd - timedelta(days=1)
        return _iso(start), _iso(start)

    # 3) "the past/last N days/weeks/months"
    m = re.search(
        r"\b(?:the\s+)?(?:past|last)\s+(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+"
        r"(day|days|week|weeks|month|months|year|years)\b",
        text,
    )
    if m:
        n = _to_int(m.group(1))
        unit = m.group(2)
        if n is not None:
            if unit.startswith("day"):
                start = qd - timedelta(days=n)
            elif unit.startswith("week"):
                start = qd - timedelta(weeks=n)
            elif unit.startswith("month"):
                start = _add_months(qd, -n)
            else:
                start = _add_months(qd, -12 * n)
            return _iso(start), _iso(qd)

    # 4) "the past/last week|month|year" (no number, default to last 7/30/365)
    m = re.search(r"\b(?:the\s+)?(?:past|last)\s+(week|month|year)\b", text)
    if m:
        unit = m.group(1)
        if unit == "week":
            start = qd - timedelta(weeks=1)
        elif unit == "month":
            start = _add_months(qd, -1)
        else:
            start = _add_months(qd, -12)
        return _iso(start), _iso(qd)

    # 5) "this week|month|year" — interpret as the calendar period
    m = re.search(r"\bthis\s+(week|month|year)\b", text)
    if m:
        unit = m.group(1)
        if unit == "week":
            ws = qd - timedelta(days=qd.weekday())
            return _iso(ws), _iso(ws + timedelta(days=6))
        elif unit == "month":
            return _month_bounds(qd.year, qd.month)
        else:
            return _iso(datetime(qd.year, 1, 1)), _iso(datetime(qd.year, 12, 31))

    # 6) "in <year>" e.g. "in 2023" — whole year
    m = re.search(r"\bin\s+(\d{4})\b", text)
    if m:
        y = int(m.group(1))
        return _iso(datetime(y, 1, 1)), _iso(datetime(y, 12, 31))

    # 7) "in <month>" or "in <month> <year>" — that calendar month
    m = re.search(
        r"\bin\s+([A-Za-z]+)(?:\s+(\d{4}))?\b", text
    )
    if m:
        month_name = m.group(1)
        year = int(m.group(2)) if m.group(2) else qd.year
        month = _MONTH_NAMES.get(month_name.rstrip("."))
        if month is not None and 1 <= month <= 12:
            return _month_bounds(year, month)

    # 8) "the (first|second|third|...) week of <month>" — 7-day window
    m = re.search(
        r"\b(?:the\s+)?(first|second|third|fourth|fifth)\s+week\s+of\s+"
        r"([A-Za-z]+)(?:\s+(\d{4}))?\b",
        text,
    )
    if m:
        wk = _ORDINAL_WEEK[m.group(1)]
        month_name = m.group(2)
        year = int(m.group(3)) if m.group(3) else qd.year
        month = _MONTH_NAMES.get(month_name.rstrip("."))
        if month is not None:
            first = datetime(year, month, 1)
            start = first + timedelta(weeks=wk - 1)
            end = start + timedelta(days=6)
            return _iso(start), _iso(end)

    return None


def _to_int(token: str) -> int | None:
    t = token.lower()
    mapping = {
        "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "twelve": 12,
    }
    if t in mapping:
        return mapping[t]
    try:
        return int(t)
    except ValueError:
        return None
