"""Fact Extraction & Locking for agentic-memory.

Extracts structured facts (entity-relation-entity triples) from memories
with confidence scoring and temporal decay.

Zero dependencies. Practical extraction from markdown patterns:
  1. Bold labels ("**Feature:** description")
  2. Dash bullets ("- **Feature** — description")
  3. Classification ("X is a Y")
  4. Code references (file.py defines function_name)

Opt-in via MEMORY_KNOWLEDGE_GRAPH=1.
"""

from __future__ import annotations

import calendar
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import time

logger = logging.getLogger(__name__)

# Use config system for feature flags
try:
    from config import get_config
except ImportError:
    get_config = None  # type: ignore[assignment]

__all__ = [
    "ensure_facts_schema",
    "extract_facts",
    "extract_event_time",
    "lock_fact",
    "unlock_fact",
    "facts_search",
    "facts_list",
    "facts_stats",
]

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_FACTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kg_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    locked INTEGER DEFAULT 0,
    first_seen REAL,
    last_seen REAL,
    mention_count INTEGER DEFAULT 1,
    source_memory TEXT,
    context TEXT,
    UNIQUE(subject, predicate, object)
);

CREATE INDEX IF NOT EXISTS idx_kg_facts_subject ON kg_facts(subject);
CREATE INDEX IF NOT EXISTS idx_kg_facts_predicate ON kg_facts(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_facts_object ON kg_facts(object);
CREATE INDEX IF NOT EXISTS idx_kg_facts_spo ON kg_facts(subject, predicate, object);
"""


def ensure_facts_schema(conn: sqlite3.Connection) -> None:
    """Create the ``kg_facts`` table and indexes if they don't exist.

    Idempotent: safe to call on every connection open. The CREATE
    statements use ``IF NOT EXISTS`` so re-running on a DB that
    already has the table is a no-op.
    """
    # Base table + indexes (idempotent on all schema versions)
    conn.executescript(_FACTS_SCHEMA_SQL)
    # B24: backfill entity FK columns on pre-migration DBs — must run BEFORE
    # entity_id indexes so columns exist.
    # T1.x (temporal-kg plan): backfill the v18 temporal columns on pre-migration
    # DBs — also must run BEFORE the v18 indexes so columns exist.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(kg_facts)").fetchall()}
    if "subject_entity_id" not in cols:
        conn.execute(
            "ALTER TABLE kg_facts ADD COLUMN subject_entity_id INTEGER REFERENCES kg_entities(id)"
        )
    if "object_entity_id" not in cols:
        conn.execute(
            "ALTER TABLE kg_facts ADD COLUMN object_entity_id INTEGER REFERENCES kg_entities(id)"
        )
    # T1.x: v18 temporal columns. Each column is independent — if a
    # pre-v18 DB has some but not others, only the missing ones are
    # added. All columns are NULL-able so existing rows are unaffected.
    if "event_time" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN event_time REAL")
    if "event_time_granularity" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN event_time_granularity TEXT")
    if "transaction_time" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN transaction_time REAL")
    if "valid_at" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN valid_at REAL")
    if "invalid_at" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN invalid_at REAL")
    if "superseded_by" not in cols:
        conn.execute(
            "ALTER TABLE kg_facts ADD COLUMN superseded_by INTEGER "
            "REFERENCES kg_facts(id) ON DELETE SET NULL"
        )
    if "supersedes" not in cols:
        conn.execute(
            "ALTER TABLE kg_facts ADD COLUMN supersedes INTEGER "
            "REFERENCES kg_facts(id) ON DELETE SET NULL"
        )
    if "contradiction_score" not in cols:
        conn.execute(
            "ALTER TABLE kg_facts ADD COLUMN contradiction_score REAL DEFAULT 0.0"
        )
    if "invalidation_reason" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN invalidation_reason TEXT")
    # Now entity_id and temporal indexes are safe to create.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_subject_entity ON kg_facts(subject_entity_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_object_entity ON kg_facts(object_entity_id)"
    )
    # T1.x: v18 temporal indexes.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_validity ON kg_facts(valid_at, invalid_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_superseded_by ON kg_facts(superseded_by)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_event_time ON kg_facts(event_time)"
    )
    # T20 (2026-06-23): kg_facts FTS5 index. Brings kg_facts in line with
    # the other 3 text-searchable tables (memories, memory_chunks,
    # kg_entities) which all have FTS5 + 3 sync triggers. The FTS is
    # contentless (backed by kg_facts) so it doesn't duplicate storage.
    # Use IF NOT EXISTS so this is safe to call on every connection
    # open.  Idempotent with migration 020_kg_facts_fts.sql.
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS kg_facts_fts USING fts5("
        "subject, predicate, object, context, "
        "content='kg_facts', content_rowid='id', "
        "tokenize='porter unicode61'"
        ")"
    )
    # 3 sync triggers (ai, ad, au). Use IF NOT EXISTS for idempotency.
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS kg_facts_fts_ai AFTER INSERT ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context) "
        "VALUES (new.id, new.subject, new.predicate, new.object, new.context); "
        "END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS kg_facts_fts_ad AFTER DELETE ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context) "
        "VALUES('delete', old.id, old.subject, old.predicate, old.object, old.context); "
        "END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS kg_facts_fts_au AFTER UPDATE ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context) "
        "VALUES('delete', old.id, old.subject, old.predicate, old.object, old.context); "
        "INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context) "
        "VALUES (new.id, new.subject, new.predicate, new.object, new.context); "
        "END"
    )


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

_FRONTMATTER = re.compile(r"^---[\s\S]*?---\s*", re.MULTILINE)
_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_INLINE_CODE = re.compile(r"`([^`]+)`")


def _preprocess(text: str) -> str:
    """Strip YAML frontmatter, code blocks, and inline code spans
    from *text* before fact extraction.

    Why: frontmatter contains key=value pairs that look like facts
    ("status: active" -> subject="status", predicate=has_value,
    object="active") but they're metadata, not content. Code blocks
    can contain function signatures that look like code-reference
    facts. Stripping these reduces noise.
    """
    text = _FRONTMATTER.sub("", text)
    text = _CODE_BLOCK.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    return text


def _preprocess_dates(text: str) -> str:
    """Strip frontmatter, code blocks, and inline code spans FULLY (not
    just the backticks) from *text* before date extraction.

    The shared ``_preprocess`` used by fact extraction keeps inline code
    content (so `open()` can be detected as a function reference). For
    date extraction, dates in inline code (``2024-01-01``) are usually
    version numbers, build IDs, or commit hashes — not natural-language
    event times — so we strip them entirely.

    Why: a memory that says "we shipped `v3.0` on 2024-01-15" should
    extract the date 2024-01-15 (the "on" preposition binds the date to
    the event), but a memory that says "the API version is `2024-01-15`"
    should NOT extract any date (the date is just a version label).
    """
    text = _FRONTMATTER.sub("", text)
    text = _CODE_BLOCK.sub("", text)
    text = _INLINE_CODE.sub("", text)  # FULL strip, not r"\1"
    return text


# ---------------------------------------------------------------------------
# Event Time Extraction (T2 of the temporal-kg plan, schema v18)
# ---------------------------------------------------------------------------
#
# extract_event_time(content) returns the most prominent event time
# (when a fact was true in the world) extracted from a memory's text.
# Used by kg_facts.event_time and kg_facts.event_time_granularity
# (see migrations/018_fact_temporal.sql).
#
# Granularity values:
#   'day'    — exact date known (e.g. "2026-03-15", "March 15, 2026")
#   'month'  — month precision (e.g. "March 2026", "Q1 2026", "early 2024")
#   'year'   — year precision (e.g. "in 2024", "since 2020")
#   'unknown'— no recognizable date
#
# Plain "YYYY" without a temporal preposition is NOT matched (too noisy:
# matches version numbers like "v2024", IDs, code references, etc.).
# Preposition-bound patterns are preferred because they make the time
# reference explicit.
#
# Patterns are tried in order of specificity — the first match wins.
# For ranges like "from 2018 to 2022", the START is returned (2018) and
# the end is left for the LLM extraction path (T2.3) to handle.
# ---------------------------------------------------------------------------

_MONTH_NAMES: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _to_epoch(year: int, month: int = 1, day: int = 1) -> float:
    """Convert (year, month, day) to UTC epoch seconds (midnight UTC).

    Returns 0.0 if the date is out of the supported range or invalid.
    Supported range: 1900-2200. Out-of-range dates return 0.0 to signal
    "no event time" rather than silently producing nonsense epochs.
    """
    if year < 1900 or year > 2200:
        return 0.0
    if month < 1 or month > 12 or day < 1 or day > 31:
        return 0.0
    try:
        return float(calendar.timegm((year, month, day, 0, 0, 0, 0, 0, 0)))
    except (ValueError, OverflowError, OSError):
        return 0.0


def _parse_iso(m: "re.Match") -> tuple[int, int, int]:
    """ISO date: YYYY-MM-DD or YYYY/MM/DD (groups 1-2-3 are year-month-day)."""
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _parse_us(m: "re.Match") -> tuple[int, int, int]:
    """US date: M/D/YYYY (groups 1-2-3 are month-day-year)."""
    return (int(m.group(3)), int(m.group(1)), int(m.group(2)))


def _parse_day_first_named(m: "re.Match") -> tuple[int, int, int]:
    """Day-first named: "15 March 2026" / "1 Jan 2024" / "31 Dec. 2025".

    Groups: 1=day, 2=month-name, 3=year.
    """
    return (
        int(m.group(3)),
        _MONTH_NAMES[m.group(2).lower().rstrip(".")],
        int(m.group(1)),
    )


def _parse_month_first_named(m: "re.Match") -> tuple[int, int, int]:
    """Month-first named: "March 15, 2026" / "Jan 1 2024" / "Dec. 31 2025".

    Groups: 1=month-name, 2=day, 3=year.
    """
    return (
        int(m.group(3)),
        _MONTH_NAMES[m.group(1).lower().rstrip(".")],
        int(m.group(2)),
    )


def _parse_named_month_year(m: "re.Match") -> tuple[int, int]:
    """Month + year: "March 2026" / "Jan 2024" / "December 2025".

    Groups: 1=month-name, 2=year. Day defaults to 1.
    """
    return (int(m.group(2)), _MONTH_NAMES[m.group(1).lower().rstrip(".")])


def _parse_quarter(m: "re.Match") -> tuple[int, int]:
    """Q[1-4] YYYY: returns start of quarter (Q1=Jan, Q2=Apr, Q3=Jul, Q4=Oct)."""
    quarter = int(m.group(1))
    return (int(m.group(2)), (quarter - 1) * 3 + 1)


def _parse_partial_year(m: "re.Match") -> tuple[int, int]:
    """early/mid/late YYYY: returns representative month (Jan/Apr/Oct).

    Groups: 1=modifier, 2=year. The representative months are arbitrary
    anchors; the granularity tag ('month') tells callers the precision is
    approximate.
    """
    modifier = m.group(1).lower()
    year = int(m.group(2))
    if modifier == "early":
        return (year, 1)
    if modifier == "mid":
        return (year, 4)
    return (year, 10)


def _parse_year_only(m: "re.Match") -> tuple[int]:
    """Just YYYY. Day and month default to January 1."""
    return (int(m.group(1)),)


def _parse_now(m: "re.Match") -> None:
    """Sentinel for present-tense markers (currently/now/today).

    The caller detects this parser and substitutes time.time() rather
    than parsing groups.
    """


# Pattern list: (compiled regex, parser, granularity).  Order matters:
# more-specific patterns are tried first, so the first match wins.  Add
# a new pattern ABOVE an existing one only if it's strictly more specific
# (e.g., ISO date above bare year).
_DATE_PATTERNS: list[tuple] = []


def _add_date_pattern(pattern: str, parser, granularity: str) -> None:
    _DATE_PATTERNS.append((re.compile(pattern, re.I), parser, granularity))


# 1. ISO date YYYY-MM-DD — most specific day-precision pattern.
_add_date_pattern(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", _parse_iso, "day")

# 2. ISO slash YYYY/MM/DD.
_add_date_pattern(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b", _parse_iso, "day")

# 3. US slash M/D/YYYY (month first).
_add_date_pattern(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", _parse_us, "day")

# 4. Day-first named: "15 March 2026" / "1 Jan 2024" / "31 Dec. 2025".
_add_date_pattern(
    r"\b(\d{1,2})\s+([A-Za-z]{3,9}\.?)\s+(\d{4})\b",
    _parse_day_first_named,
    "day",
)

# 5. Month-first named: "March 15, 2026" / "Jan 1 2024" / "Dec. 31, 2025".
_add_date_pattern(
    r"\b([A-Za-z]{3,9}\.?)\s+(\d{1,2})(?:,)?\s+(\d{4})\b",
    _parse_month_first_named,
    "day",
)

# 6. Bare month + year: "March 2026" / "January 2024".
_add_date_pattern(
    r"\b([A-Za-z]{3,9})\s+(\d{4})\b",
    _parse_named_month_year,
    "month",
)

# 7. Quarter: "Q1 2026" / "Q4 2025".
_add_date_pattern(r"\bQ([1-4])\s+(\d{4})\b", _parse_quarter, "month")

# 8. early/mid/late + year: "early 2024" / "mid 2025" / "late 2026".
_add_date_pattern(
    r"\b(early|mid|late)\s+(\d{4})\b",
    _parse_partial_year,
    "month",
)

# 9. Preposition + bare year — the most common form in narrative text.
#    Prepositions are limited to temporal ones (not "of", "the", etc.)
#    to avoid noise.  `as\s+of` handles "as of YYYY".
_add_date_pattern(
    r"\b(?:in|during|since|until|as\s+of|from|to|by|before|after|"
    r"around|circa|ca\.?|about|between|through|throughout|"
    r"prior\s+to|following|effective)\s+(\d{4})\b",
    _parse_year_only,
    "year",
)

# 10. Preposition + month-name + year: "in March 2026" / "since Jan 2024".
_add_date_pattern(
    r"\b(?:in|during|since|until|by|on|from|as\s+of|around|about)\s+"
    r"([A-Za-z]{3,9})\.?\s+(\d{4})\b",
    _parse_named_month_year,
    "month",
)

# 11. Preposition + ISO date: "as of 2026-03-15" / "on 2024-01-01".
#     Lower priority than bare ISO (#1) because the preposition adds
#     nothing to the date precision; the preposition is just a confidence
#     booster for the extraction.
_add_date_pattern(
    r"\b(?:in|during|since|until|by|on|from|as\s+of|around|about|"
    r"effective)\s+(\d{4})-(\d{1,2})-(\d{1,2})\b",
    _parse_iso,
    "day",
)

# 12. Present-tense markers: "currently", "now", "today", "as of now".
#     Resolved at extraction time to time.time() (now, day-granularity).
_add_date_pattern(
    r"\b(currently|now(?:\s+that)?|today|presently|right\s+now|"
    r"as\s+of\s+(?:now|today)|at\s+present)\b",
    _parse_now,
    "day",
)


def extract_event_time(content: "str | None") -> tuple[float | None, str]:
    """Extract the most prominent event time from natural-language content.

    Returns ``(epoch_seconds, granularity)`` where granularity is one of:

      * ``'day'``   : exact date known (e.g. "2026-03-15", "March 15, 2026")
      * ``'month'`` : month precision (e.g. "March 2026", "Q1 2026", "early 2024")
      * ``'year'``  : year precision (e.g. "in 2024", "since 2020")
      * ``'unknown'``: no recognizable date

    Returns ``(None, 'unknown')`` if no pattern matches.

    Patterns are tried in order of specificity — the first match wins.
    For ranges like "from 2018 to 2022", the **start** is returned (2018)
    and the end is left for the LLM extraction path (T2.3) to handle.

    Plain "YYYY" without a temporal preposition is NOT matched (too noisy:
    matches version numbers like "v2024", IDs, code references, etc.).
    Use the preposition-bound pattern when authoring content.

    Code blocks and YAML frontmatter are stripped before pattern matching
    so a "2024-01-01" in a fenced code block doesn't pollute results.
    """
    if not content or not isinstance(content, str) or len(content) < 4:
        return (None, "unknown")

    # Use a stricter preprocessor that fully strips inline code (not just
    # the backticks). Dates in `2024-01-01` are usually version numbers or
    # IDs, not natural-language event times. The shared ``_preprocess``
    # used by fact extraction keeps inline code content (so `open()` can
    # be detected as a function reference), which is wrong for dates.
    text = _preprocess_dates(content)

    for pattern, parser, granularity in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        if parser is _parse_now:
            return (time.time(), "day")
        try:
            parsed = parser(m)
            if not parsed:
                continue
            year = parsed[0]
            month = parsed[1] if len(parsed) > 1 else 1
            day = parsed[2] if len(parsed) > 2 else 1
            epoch = _to_epoch(year, month, day)
            if epoch > 0:
                return (float(epoch), granularity)
        except (ValueError, IndexError, KeyError, AttributeError):
            continue

    return (None, "unknown")


# ---------------------------------------------------------------------------
# Verb Detection
# ---------------------------------------------------------------------------

# Verbs we care about (canonical form -> predicate label)
_VERB_MAP: dict[str, str] = {
    "extract": "extracts",
    "extracts": "extracts",
    "create": "creates",
    "creates": "creates",
    "build": "creates",
    "builds": "creates",
    "generate": "creates",
    "generates": "creates",
    "produce": "creates",
    "produces": "creates",
    "store": "stores",
    "stores": "stores",
    "save": "stores",
    "saves": "stores",
    "cache": "stores",
    "caches": "stores",
    "persist": "stores",
    "persists": "stores",
    "hold": "contains",
    "holds": "contains",
    "contain": "contains",
    "contains": "contains",
    "delete": "deletes",
    "deletes": "deletes",
    "remove": "deletes",
    "removes": "deletes",
    "drop": "deletes",
    "drops": "deletes",
    "purge": "deletes",
    "purges": "deletes",
    "clear": "deletes",
    "clears": "deletes",
    "write": "writes",
    "writes": "writes",
    "read": "reads",
    "reads": "reads",
    "load": "loads",
    "loads": "loads",
    "fetch": "fetches",
    "fetches": "fetches",
    "use": "uses",
    "uses": "uses",
    "utilize": "uses",
    "utilizes": "uses",
    "employ": "uses",
    "employs": "uses",
    "leverage": "uses",
    "leverages": "uses",
    "call": "calls",
    "calls": "calls",
    "invoke": "calls",
    "invokes": "calls",
    "require": "requires",
    "requires": "requires",
    "need": "requires",
    "needs": "requires",
    "depend": "depends_on",
    "depends": "depends_on",
    "provide": "provides",
    "provides": "provides",
    "offer": "provides",
    "offers": "provides",
    "handle": "handles",
    "handles": "handles",
    "process": "processes",
    "processes": "processes",
    "parse": "parses",
    "parses": "parses",
    "analyze": "analyzes",
    "analyzes": "analyzes",
    "validate": "validates",
    "validates": "validates",
    "verify": "verifies",
    "verifies": "verifies",
    "check": "checks",
    "checks": "checks",
    "monitor": "monitors",
    "monitors": "monitors",
    "track": "tracks",
    "tracks": "tracks",
    "enable": "enables",
    "enables": "enables",
    "disable": "disables",
    "disables": "disables",
    "configure": "configures",
    "configures": "configures",
    "override": "overrides",
    "overrides": "overrides",
    "connect": "connects_to",
    "connects": "connects_to",
    "bind": "binds_to",
    "binds": "binds_to",
    "trigger": "triggers",
    "triggers": "triggers",
    "initiate": "triggers",
    "initiates": "triggers",
    "combine": "combines",
    "combines": "combines",
    "merge": "merges",
    "merges": "merges",
    "apply": "applies",
    "applies": "applies",
    "support": "supports",
    "supports": "supports",
    "prevent": "prevents",
    "prevents": "prevents",
    "capture": "captures",
    "captures": "captures",
    "detect": "detects",
    "detects": "detects",
    "identify": "identifies",
    "identifies": "identifies",
    "evaluate": "evaluates",
    "evaluates": "evaluates",
    "manage": "manages",
    "manages": "manages",
    "compute": "computes",
    "computes": "computes",
    "calculate": "calculates",
    "calculates": "calculates",
    "measure": "measures",
    "measures": "measures",
    "rank": "ranks",
    "ranks": "ranks",
    "sort": "sorts",
    "sorts": "sorts",
    "filter": "filters",
    "filters": "filters",
    "wrap": "wraps",
    "wraps": "wraps",
    "extend": "extends",
    "extends": "extends",
    "implement": "implements",
    "implements": "implements",
    "replace": "replaces",
    "replaces": "replaces",
    "supersede": "supersedes",
    "supersedes": "supersedes",
    "convert": "converts",
    "converts": "converts",
    "transform": "transforms",
    "transforms": "transforms",
    "roll": "rolls_up",
    "rolls": "rolls_up",
    "deduplicate": "deduplicates",
    "deduplicates": "deduplicates",
}

# Regex to find the first verb in a description sentence
_VERB_RE = re.compile(
    r"\b("
    + "|".join(re.escape(v) for v in sorted(_VERB_MAP.keys(), key=len, reverse=True))
    + r")\b",
    re.I,
)


def _find_verb(text: str) -> str | None:
    """Find the first recognized verb in text, return canonical predicate."""
    m = _VERB_RE.search(text)
    if m:
        return _VERB_MAP.get(m.group(1).lower())
    return None


# ---------------------------------------------------------------------------
# Fact Extraction Patterns
# ---------------------------------------------------------------------------

# Meta-labels to skip (not real features)
_META_LABELS = {
    "what it does",
    "what it does:",
    "how it works",
    "how it works:",
    "overview",
    "description",
    "note",
    "notes",
    "todo",
    "example",
    "examples",
    "usage",
    "install",
    "status",
    "status:",
    "date",
    "date:",
    "type",
    "type:",
    "version",
    "version:",
    "author",
    "author:",
    "license",
    "license:",
    "timestamp",
    "timestamp:",
    "created",
    "created:",
    "updated",
    "updated:",
    "installation",
    "references",
    "see also",
    "related",
    "background",
    "context",
    "rationale",
    "motivation",
    "goal",
    "objective",
    "key design principles",
    "key design principles:",
    "what was done",
    "key lessons",
    "what went wrong",
    "what went well",
    "root cause",
    "root cause:",
    "fix:",
    "solution:",
    "impact:",
    "next steps",
    "action items",
    "follow-up",
    "follow-up:",
    "files modified",
    "test count",
    "test results",
    "results:",
    "performance",
    "metrics",
    "benchmarks",
    "statistics",
    # Table/status headers that aren't facts
    "completed items",
    "completed",
    "remaining",
    "in progress",
    "final status",
    "final gate",
    "production db",
    "tables present",
    "columns present",
    "indexes present",
    "fix required",
    "verification",
    "findings",
    "issues found",
    "audit findings",
    "audit results",
    "before",
    "after",
    "before fix",
    "after fix",
    # Section labels
    "key files modified",
    "key files",
    "files changed",
    "changes",
    "environment",
    "setup",
    "configuration",
    "config",
    "requirements",
    "dependencies",
    "prerequisites",
    "limitations",
    "caveats",
    "warnings",
    "notes:",
    "api reference",
    "接口",
    "参数",
    "返回值",
    # Generic template labels from structured notes
    "file",
    "bug",
    "fix",
    "effort",
    "staying",
    "task",
    "issue",
    "priority",
    "severity",
    "assignee",
    "labels",
    "milestone",
    # B24 noise cleanup — observed in sessions/auto-* corpus
    "workflow",
    "workflow:",
    "document",
    "document:",
    "type",
    "type:",
    "document type",
    "document type:",
    "test gap",
    "test gap:",
    "active todos",
    "recent tool activity",
    "what was being",
    "worked on",
    "what was being worked on",
    "time before compaction",
    "time before compaction:",
    "auto-save notes (with content summaries)",
    "save notes",
    "do after compaction",
    "compaction context save",
    "recent conclusions",
    "key insights",
}

# B24 — narrow set of bullet-header words that the NEW Layer 5
# patterns (5b colon, 5c plain dash) should reject but Layer 1/2/3
# legitimately use. Kept SEPARATE from _META_LABELS to avoid breaking
# existing extractions.
_LAYER5_META_LABELS: frozenset[str] = frozenset(
    {
        "score",
        "score:",
        "tags",
        "tags:",
        "rules",
        "rules:",
    }
)

# Operational category prefixes that should NOT have facts extracted.
# These are tool-call logs, audit events, and compaction artefacts — not
# knowledge. Extracting facts from them produces only boilerplate
# ("tool has_description ...", "timestamp has_value ...") and pollutes
# the KG. Set MEMORY_FACT_EXTRACTION_INCLUDE_OPERATIONAL=1 to override.
_SKIP_CATEGORIES: tuple[str, ...] = (
    "sessions/auto-",
    "sessions/audit-",
    "sessions/compaction-",
    "sessions/idle-",
    "sessions/end-",
)


def _should_skip_category(memory_id: str) -> bool:
    """Return True if ``memory_id`` belongs to an operational category.

    Operational categories are tool-call transcripts, audit events, and
    compaction artefacts. They have structured metadata, not natural
    language content; extracting facts from them creates noise without
    adding knowledge. Override with MEMORY_FACT_EXTRACTION_INCLUDE_OPERATIONAL=1.
    """
    if not memory_id:
        return False
    if os.environ.get("MEMORY_FACT_EXTRACTION_INCLUDE_OPERATIONAL") == "1":
        return False
    return any(memory_id.startswith(prefix) for prefix in _SKIP_CATEGORIES)


# Leading articles to strip from subjects
_ARTICLES = re.compile(r"^(?:The|A|An|This|That|These|Those|Its|Our|Your)\s+", re.I)

# Weak subjects — single words or pronouns that shouldn't be facts
_WEAK_SUBJECTS = frozenset(
    {
        "it",
        "this",
        "that",
        "we",
        "they",
        "you",
        "i",
        "he",
        "she",
        "what",
        "why",
        "how",
        "when",
        "where",
        "which",
        "who",
        "whose",
        "there",
        "here",
        "then",
        "now",
        "also",
        "just",
        "only",
        "still",
        "however",
        "therefore",
        "moreover",
        "furthermore",
        "nevertheless",
        "why this",
        "but this",
        "and this",
        "so this",
        "then this",
        "that this",
        "if this",
        "but it",
        "and it",
        "so it",
        "then it",
        "that it",
        "if it",
        "but that",
        "and that",
        "memory",
        "note",
        "notes",
        "data",
        "value",
        "result",
        "output",
        "input",
        "error",
        "issue",
        "problem",
        "task",
        "item",
    }
)

# Priority markers to skip in labels
_PRIORITY_RE = re.compile(
    r"^(?:P\d+|[A-Z]\d+|Phase\s+\d+|Step\s+\d+|Task\s+\d+|Fix\s+\d+|Bug\s+\d+|BLK-\d+|Item\s+\d+)\s*[—–:\-]"
)

# Completion markers to skip — only standalone markers, not words inside headers
_COMPLETE_RE = re.compile(r"(?:✅|☑|✔|DONE$|COMPLETED$)")

# Bold label: "**Feature:** description" or "**Feature** description"
_BOLD_LABEL = re.compile(r"\*\*([^*]+)\*\*:?\s*([^\n]+)", re.S)

# Dash bullet: "- **Feature** — description" or "- Feature — description"
_DASH_BULLET = re.compile(
    r"(?:^|\n)\s*[-*]\s*\*?\*?([A-Za-z][\w\s./-]*?\w)\*?\*?\s*[—–]\s*(.+?)(?=\n[-*]|\n##|\Z)",
    re.S,
)

# Classification: "X is a Y"
_CLASSIFY = re.compile(
    r"\b(\w[\w\s]*?\w)\s+(?:is\s+a|is\s+an|is\s+the|acts\s+as|behaves\s+as)\s+"
    r"(.+?)(?:\.|,|\s+(?:that|which|where|when|for|with|and|but|because|while"
    r"|although|though|unless|if|except|however|so|since|as)\b)",
    re.I,
)

# Section header: "## Feature Name"
_SECTION_HEADER = re.compile(r"^##\s+(.+?)$", re.M)

# Code references
_FUNC_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(")
_FILE_REF = re.compile(
    r"\b[\w./-]+\.(?:py|js|ts|rs|go|java|rb|php|sql|json|yaml|yml|toml|md|sh)\b"
)
_FUNC_SKIP = frozenset(
    {
        "if",
        "for",
        "while",
        "def",
        "class",
        "return",
        "print",
        "len",
        "str",
        "int",
        "list",
        "dict",
        "set",
        "tuple",
        "type",
        "isinstance",
        "range",
        "enumerate",
        "zip",
        "map",
        "filter",
        "sorted",
        "reversed",
        "super",
        "open",
        "yield",
        "assert",
        "raise",
        "try",
        "except",
        "finally",
        "with",
        "import",
        "from",
        "not",
        "and",
        "or",
        "in",
        "is",
        "as",
        "lambda",
        "pass",
        "break",
        "continue",
        "del",
        "global",
        "nonlocal",
        "any",
        "all",
        "min",
        "max",
        "sum",
        "abs",
        "round",
        "int",
        "float",
        "bool",
        "hex",
        "oct",
        "bin",
        "chr",
        "ord",
        "input",
        "exec",
        "eval",
        "compile",
        "globals",
        "locals",
        "vars",
        "dir",
        "hasattr",
        "getattr",
        "setattr",
        "delattr",
        "id",
        "hash",
        "repr",
        "format",
        # Python builtins and common imports
        "exit",
        "quit",
        "sys",
        "os",
        "re",
        "json",
        "sqlite3",
        "time",
        "math",
        "pathlib",
        "logging",
        "threading",
        "subprocess",
        "shutil",
        "collections",
        "itertools",
        "functools",
        "operator",
        "string",
        "io",
        "copy",
        "pprint",
        "dataclass",
        "enum",
        "abc",
        "typing",
        "contextlib",
        "textwrap",
        # Common test/framework calls
        "test",
        "tests",
        "assert",
        "assertEqual",
        "assertTrue",
        "assertFalse",
        "pytest",
        "unittest",
        "mock",
        "patch",
        "fixture",
        "parametrize",
        # Generic verbs that appear in code but aren't definitions
        "run",
        "get",
        "set",
        "add",
        "remove",
        "delete",
        "update",
        "create",
        "push",
        "pop",
        "insert",
        "append",
        "extend",
        "merge",
        "join",
        "start",
        "stop",
        "close",
        "flush",
        "read",
        "write",
        "seek",
        "connect",
        "disconnect",
        "send",
        "recv",
        "accept",
        "listen",
        "lock",
        "unlock",
        "acquire",
        "release",
        "wait",
        "notify",
        "encode",
        "decode",
        "serialize",
        "deserialize",
        "dump",
        "load",
        "parse",
        "format",
        "validate",
        "check",
        "verify",
        "confirm",
        "log",
        "debug",
        "info",
        "warn",
        "error",
        "fatal",
        "critical",
    }
)


def _clean(text: str) -> str:
    """Clean extracted text."""
    text = text.strip().rstrip(".")
    text = re.sub(r"\s+", " ", text).strip()
    # Strip trailing junk
    text = re.sub(
        r"\s+(?:and|but|who|that|which|where|when|with|for|in|at|on|to|so|"
        r"because|since|while|although|however|therefore|using|via)\s*$",
        "",
        text,
        flags=re.I,
    )
    return text


# Markdown table row pattern
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
# Parenthetical-only pattern: "(text)" with nothing else
_PAREN_ONLY = re.compile(r"^\s*\(.+\)\s*$")


def _clean_description(desc: str) -> str:
    """Clean a description extracted from bold labels or dash bullets.

    Strips markdown table syntax, parenthetical status notes, and other noise.
    Returns empty string if the description is not a meaningful fact.
    """
    if "|" in desc:
        return ""

    # Strip markdown table rows
    desc = _TABLE_ROW.sub("", desc).strip()
    # Strip inline table cells that leaked through
    desc = re.sub(r"\|[^|]+\|", "", desc).strip()
    desc = re.sub(r"^[\s|]+", "", desc).strip()
    desc = re.sub(r"[\s|]+$", "", desc).strip()

    # Skip parenthetical-only descriptions
    if _PAREN_ONLY.match(desc):
        return ""

    # Skip descriptions that are just pure numbers/counts (no text at all)
    if re.match(r"^[\d,/\s]+$", desc):
        return ""

    # Skip "X/Y done/pass" status-only patterns (must have "done" or "pass" keyword)
    if re.match(r"^\d+/\d+\s+(done|pass|fail|complete|ok)\b", desc, re.I):
        return ""

    # Skip "N.N-N.N done" patterns
    if re.match(r"^[\d.]+-[\d.]+\s+(done|pass|fail|complete)", desc, re.I):
        return ""

    # Skip descriptions that are just table separators
    if re.match(r"^[-:|=\s]+$", desc):
        return ""

    # Skip very short or very generic
    if len(desc) < 3:
        return ""

    return desc


def _is_valid(subj: str, obj: str) -> bool:
    """Return True if (subj, obj) is a valid fact pair (both have
    enough content to be meaningful).

    Quality gate used by every layer. Rejects pairs that are
    obviously noise: empty strings, single characters, pure
    punctuation, or one-side numeric only.
    """
    if len(subj) < 2 or len(obj) < 2:
        return False
    if subj.lower() == obj.lower():
        return False
    if re.match(r"^[\d\s\-:/.,]+$", subj):
        return False
    if re.match(r"^'[^']{1,4}'$", obj):
        return False
    if len(obj) <= 2 and obj.islower() and obj.isalpha():
        return False
    return True


def _strip_articles(text: str) -> str:
    """Strip leading articles from text."""
    return _ARTICLES.sub("", text).strip()


def _is_meta_header(header: str) -> bool:
    """Return True if a section header is one of the skip categories.

    Extracted from extract_facts() to make Layer 1 readable. Encodes
    the rules:
      - Pure meta-labels (about, summary, etc.)
      - "Final status / current gate" type headers
      - "Sprint N summary" headers
      - Priority markers (P0, P1, BLK-1, etc.)
      - Phase/Step/Task/Item number prefixes
    """
    if header.lower().rstrip(":") in _META_LABELS:
        return True
    if re.match(r"^(?:final|current|latest)\s+(?:status|gate|state)", header, re.I):
        return True
    if re.match(r"^sprint\s+\d+\s+summary", header, re.I):
        return True
    if _PRIORITY_RE.match(header):
        return True
    if re.match(
        r"^(?:p[0-4]|phase\s+\d+|step\s+\d+|task\s+\d+|fix\s+\d+|"
        r"bug\s+\d+|blk-\d+|item\s+\d+)\s*[—–:\-]",
        header,
        re.I,
    ):
        return True
    return False


def _clean_description_inline(desc: str) -> str:
    """Strip leading bold markers, em-dashes, and bullet prefixes from
    a fact description. Used by Layers 1, 2, and 5c.
    """
    desc = re.sub(r"^\*\*[^*]+\*\*:?\s*", "", desc)
    desc = re.sub(r"^[-—–]\s*", "", desc)
    return desc


def _first_sentence(text: str) -> str:
    """Return the first sentence (split on sentence-ending punctuation)
    of *text*, stripped. Used by Layers 1, 2, and 5c.
    """
    parts = re.split(r"(?<=[.!?])\s", text, maxsplit=1)
    return parts[0].strip()


def _layer1_section_header_bold(
    text: str,
    add_fn,
) -> None:
    """Layer 1: section header + bold sub-labels.

    Walks each ``## Section`` block. For each section whose header
    isn't a meta-label, finds ``**Label:** description`` patterns and
    emits (label, has_description, first_sentence_of_desc, 0.6). If a
    section has no bold sub-labels, emits (header, has_description,
    first_sentence_after_header, 0.6) instead.
    """
    sections = re.split(r"(?=^##\s)", text, flags=re.M)
    for section in sections:
        header_m = _SECTION_HEADER.match(section)
        if not header_m:
            continue
        header = header_m.group(1).strip()
        if _is_meta_header(header):
            continue
        found_bold = False
        for m in _BOLD_LABEL.finditer(section):
            label = m.group(1).strip()
            desc = m.group(2).strip()
            if label.lower().rstrip(":") in _META_LABELS:
                continue
            if _COMPLETE_RE.search(label):
                continue
            desc = _clean_description_inline(desc)
            first_sent = _first_sentence(desc)
            first_sent = _clean_description(first_sent)
            if not first_sent:
                continue
            add_fn(label, "has_description", first_sent[:150], 0.6)
            found_bold = True
        if not found_bold:
            after_header = section[header_m.end() :].strip()
            first_sent = _first_sentence(after_header)
            if first_sent and len(first_sent) > 20 and not first_sent.startswith("#"):
                first_sent = _clean_description_inline(first_sent)
                first_sent = _clean_description(first_sent)
                if first_sent:
                    add_fn(header, "has_description", first_sent[:150], 0.6)


def _layer2_dash_bullets(text: str, add_fn) -> None:
    """Layer 2: dash bullets with em-dash separator.

    ``- Label — description`` → (label, has_description, first_sentence, 0.6).
    """
    for m in _DASH_BULLET.finditer(text):
        label = m.group(1).strip()
        desc = m.group(2).strip()
        if label.lower() in _META_LABELS:
            continue
        if _COMPLETE_RE.search(label):
            continue
        desc = _clean_description_inline(desc)
        first_sent = _first_sentence(desc)
        first_sent = _clean_description(first_sent)
        if not first_sent:
            continue
        add_fn(label, "has_description", first_sent[:150], 0.6)


def _layer3_classification(text: str, add_fn) -> None:
    """Layer 3: classification sentences like ``X is a Y``.

    Emits (subj, is_a, obj, 0.8). Applies the quality gates that
    prevent single-word → single-word matches and reject sentence-
    fragment subjects.
    """
    for m in _CLASSIFY.finditer(text):
        subj = _clean(_strip_articles(m.group(1)))
        obj = _clean(m.group(2))
        if subj.lower() in _WEAK_SUBJECTS or len(subj) < 4:
            continue
        if re.match(
            r"^(?:and|but|so|then|or|yet|nor|because|while|"
            r"although|though|unless|if|except|however)\b",
            subj,
            re.I,
        ):
            continue
        if re.search(
            r"\b(?:the|a|an|of|in|to|with|by|for|and|but|or|"
            r"so|because|while|although|though|unless|if|except)\s*$",
            obj,
            re.I,
        ):
            continue
        subj_words = len(subj.split())
        obj_words = len(obj.split())
        if subj_words < 2 and obj_words < 2:
            if not (subj[0].isupper() and len(subj) >= 2):
                continue
        if subj_words == 1 and subj[0].islower():
            continue
        if obj_words == 1 and len(obj) < 5 and obj.islower():
            continue
        if subj_words + obj_words < 3 and not (
            subj_words == 1 and obj_words == 1 and subj[0].isupper()
        ):
            continue
        if not _is_valid(subj, obj):
            continue
        add_fn(subj, "is_a", obj, 0.8)


def _layer4_code_references(text: str, add_fn) -> None:
    """Layer 4: code references.

    Two sub-patterns:
      a) ``file.py calls/uses/requires other_file.py``
         → (file, action, other_file, 0.6)
      b) On a line that contains both a file ref and a ``def name(``,
         → (file, defines, name, 0.6)
    """
    code_action = re.compile(
        r"(\b[\w./-]+\.(?:py|js|ts|rs|go|java|rb|php|sql|sh))\s+"
        r"(calls|uses|requires|depends\s+on|invokes|reads|writes|"
        r"reads\s+from|writes\s+to)\s+"
        r"(\b[\w./-]+\.(?:py|js|ts|rs|go|java|rb|php|sql|sh))",
        re.I,
    )
    for m in code_action.finditer(text):
        add_fn(m.group(1), m.group(2).replace(" ", "_"), m.group(3), 0.6)
    func_def = re.compile(r"\bdef\s+([a-z_][a-z0-9_]*)\s*\(")
    for line in text.split("\n"):
        files = _FILE_REF.findall(line)
        defs = func_def.findall(line)
        if files and defs:
            for f in files[:3]:
                for fn in defs[:3]:
                    if fn not in _FUNC_SKIP and len(fn) >= 3:
                        add_fn(f, "defines", fn, 0.6)


def _layer5a_copula(text: str, add_fn) -> None:
    """Layer 5a: broader copula — ``X is/are/was/were a/an/the Y``.

    Broader than Layer 3 (which only matches "is a", "is an", etc.).
    Confidence 0.7.
    """
    copula = re.compile(
        r"\b(\w[\w\s]*?\w)\s+(?:is|are|was|were)\s+(?:a|an|the)\s+"
        r"([A-Za-z][^.\n,;]{1,100})",
    )
    for m in copula.finditer(text):
        subj_raw = m.group(1)
        subj = _clean(_strip_articles(subj_raw))
        obj = _clean(m.group(2))
        if subj.lower() in _WEAK_SUBJECTS or len(subj) < 3:
            continue
        if len(subj_raw.split()) == 1 and subj_raw[0].islower():
            continue
        raw_words = subj_raw.split()
        if len(raw_words) > 6:
            continue
        if "," in subj_raw:
            continue
        if re.search(
            r"\b(?:and|but|or|because|so|while|although|though|"
            r"unless|if|except|however|since|as)\b",
            subj_raw,
            re.I,
        ):
            continue
        if re.match(r"^[\d\s\-:/.,]+$", obj):
            continue
        if len(obj.split()) == 1 and len(obj) < 4:
            continue
        if subj.lower().rstrip(":") in _META_LABELS:
            continue
        if not _is_valid(subj, obj):
            continue
        add_fn(subj, "is_a", obj, 0.7)


def _layer5b_colon_definitions(text: str, add_fn) -> None:
    """Layer 5b: ``Label: Value`` lines (plain text, not bold).

    Predicate ``has_value``, confidence 0.6. Caps value at 80 chars.
    """
    colon_def = re.compile(
        r"^[ \t]{0,4}([A-Z][A-Za-z][\w\s/().-]{1,40})[ \t]*:[ \t]+"
        r"([^\n]{2,150})",
        re.M,
    )
    for m in colon_def.finditer(text):
        label = m.group(1).strip()
        value = m.group(2).strip()
        label_norm = label.lower().rstrip(":")
        if label_norm in _META_LABELS or label_norm in _LAYER5_META_LABELS:
            continue
        if re.match(r"^[\d\s\-:/.,]+$", value):
            continue
        if value.startswith("http://") or value.startswith("https://"):
            continue
        if value.startswith("```") or value.startswith("{"):
            continue
        if value.startswith("[") and value.endswith("]"):
            continue
        if len(value) < 3:
            continue
        if not _is_valid(label, value):
            continue
        add_fn(label, "has_value", value[:80], 0.6)


def _layer5c_plain_dash_bullets(text: str, add_fn) -> None:
    """Layer 5c: ``- just a phrase`` lines (no em-dash separator).

    Whole line is the value; subject is first 1-3 words. Confidence 0.5.
    """
    plain_dash = re.compile(
        r"(?:^|\n)[ \t]*[-*][ \t]+"
        r"([A-Za-z][\w\s,./():;!?'-]{4,120})"
        r"(?=\n|$)",
    )
    for m in plain_dash.finditer(text):
        text_line = m.group(1).strip()
        if (
            text_line.endswith("—")
            or text_line.endswith("–")
            or " — " in text_line
            or " – " in text_line
        ):
            continue
        first_word = (
            text_line.split()[0].lower().rstrip(":") if text_line.split() else ""
        )
        if first_word in _META_LABELS or first_word in _LAYER5_META_LABELS:
            continue
        if text_line.startswith("[") or text_line.startswith("!"):
            continue
        if "," in text_line and not any(
            c in text_line for c in ["is ", "are ", "was ", "uses "]
        ):
            continue
        words = text_line.split()
        if not words:
            continue
        bullet_subj: list[str] = []
        for w in words:
            if w[0].isupper():
                bullet_subj.append(w)
            elif bullet_subj:
                if len(bullet_subj) < 3:
                    bullet_subj.append(w)
                else:
                    break
            else:
                if len(words) >= 2:
                    bullet_subj.append(w)
                    if len(bullet_subj) >= 2:
                        break
                else:
                    break
        if not bullet_subj:
            continue
        subj = " ".join(bullet_subj)[:40]
        if subj.lower().rstrip(":") in _META_LABELS:
            continue
        if not _is_valid(subj, text_line):
            continue
        add_fn(subj, "has_description", text_line[:120], 0.5)


def _layer5d_subject_verb_object(text: str, add_fn) -> None:
    """Layer 5d: SVO sentences like ``The system processes requests``.

    Predicate is the canonical form from ``_VERB_MAP``. Confidence 0.5
    (SVO is the most error-prone layer).
    """
    svo = re.compile(
        r"\b([A-Z][\w\s]{0,40})\s+("
        + "|".join(
            re.escape(v) for v in sorted(_VERB_MAP.keys(), key=len, reverse=True)
        )
        + r")\s+([\w][\w\s,]{1,80})",
    )
    for m in svo.finditer(text):
        subj_raw = m.group(1)
        subj = _clean(_strip_articles(subj_raw))
        verb = m.group(2).lower()
        obj = _clean(m.group(3).split(",")[0].split(" and ")[0].split(" or ")[0])
        if subj.lower() in _WEAK_SUBJECTS or len(subj) < 3:
            continue
        if len(obj) < 2 or re.match(r"^[\d\s\-:/.,]+$", obj):
            continue
        pred = _VERB_MAP.get(verb, verb)
        if subj.lower().rstrip(":") in _META_LABELS:
            continue
        if len(subj.split()) > 6:
            continue
        if not _is_valid(subj, obj):
            continue
        add_fn(subj, pred, obj[:100], 0.5)


def _dedup_facts(
    facts: list[tuple[str, str, str, float]],
) -> list[tuple[str, str, str, float]]:
    """Deduplicate by (subject, predicate, object) keeping the
    highest-confidence copy. Normalizes trailing colons and whitespace
    before keying.
    """

    def _dedup_key(s: str, p: str, o: str) -> tuple[str, str, str]:
        """Build the dedup key for a (subj, pred, obj) triple.

        Normalizes trailing colons/whitespace BEFORE keying so
        "Score:" vs "Score" and "Tag list:" vs "Tag list" collapse
        to the same fact.
        """
        s_norm = re.sub(r"[:\s]+$", "", s.lower()).strip()
        o_norm = re.sub(r"[:\s]+$", "", o.lower()).strip()
        return (s_norm, p, o_norm)

    deduped: dict[tuple[str, str, str], tuple[str, str, str, float]] = {}
    for s, p, o, c in facts:
        key = _dedup_key(s, p, o)
        if key not in deduped or c > deduped[key][3]:
            deduped[key] = (s, p, o, c)
    return list(deduped.values())


def extract_facts(text: str) -> list[tuple[str, str, str, float]]:
    """Extract Subject-Predicate-Object triples from text.

    Decomposed into 8 layer functions (extracted 2026-06-22 so the
    orchestrator is readable; each layer is independently testable):

      Layer 1: ``_layer1_section_header_bold``
      Layer 2: ``_layer2_dash_bullets``
      Layer 3: ``_layer3_classification``
      Layer 4: ``_layer4_code_references``
      Layer 5a: ``_layer5a_copula``
      Layer 5b: ``_layer5b_colon_definitions``
      Layer 5c: ``_layer5c_plain_dash_bullets``
      Layer 5d: ``_layer5d_subject_verb_object``
    """
    if not text or len(text) < 20:
        return []

    text = _preprocess(text)
    all_facts: list[tuple[str, str, str, float]] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(subj: str, pred: str, obj: str, conf: float) -> None:
        """Add a fact to the running list, gated on quality checks.

        Applies _is_valid + has_description-specific filters and
        dedup-on-key. The closure captures `seen` and `all_facts`
        from the enclosing function scope.
        """
        subj = _clean(_strip_articles(subj))
        obj = _clean(obj)
        if not _is_valid(subj, obj):
            return
        # Description quality filter for has_description predicate
        if pred == "has_description":
            if re.match(
                r"^(?:yes|no|true|false|done|not? implemented|n/?a)\s*$", obj, re.I
            ):
                return
            if re.match(r"^(?:\d+[-:/\s]?)+$", obj):
                return
            if len(obj) <= 3 and not any(c.isalpha() for c in obj):
                return
        key = (subj.lower(), pred, obj.lower())
        if key not in seen:
            seen.add(key)
            all_facts.append((subj, pred, obj, conf))

    _layer1_section_header_bold(text, _add)
    _layer2_dash_bullets(text, _add)
    _layer3_classification(text, _add)
    _layer4_code_references(text, _add)
    _layer5a_copula(text, _add)
    _layer5b_colon_definitions(text, _add)
    _layer5c_plain_dash_bullets(text, _add)
    _layer5d_subject_verb_object(text, _add)

    return _dedup_facts(all_facts)


# ---------------------------------------------------------------------------
# Fact Indexing
# ---------------------------------------------------------------------------


def _upsert_fact(
    conn: sqlite3.Connection,
    subject: str,
    predicate: str,
    obj: str,
    confidence: float,
    now: float,
    source_memory: str = "",
    context: str = "",
    event_time: "float | None" = None,
    event_time_granularity: "str | None" = None,
) -> "int | None":
    """Insert or update a fact. Used by `index_facts_for_memory`.

    T2.5 (temporal-kg plan): accept optional ``event_time`` and
    ``event_time_granularity`` from the caller's `extract_event_time`.
    Both are stored on INSERT. On UPDATE (duplicate fact), the original
    event_time is preserved — only ``last_seen`` and confidence change.
    Once a fact is first seen with a particular event_time, that time
    becomes the canonical "when was this true" anchor; subsequent
    mentions don't override it (otherwise the time would drift forward
    every time the fact re-appeared in a newer context).

    ``transaction_time`` is set to ``now`` on INSERT (we're learning it
    now). The ``first_seen`` and ``last_seen`` columns are the
    legacy "transaction time" trackers and are kept in sync.

    Returns the fact id (new on INSERT, existing on UPDATE, or None on
    the vanishingly-rare DELETE-between-fail-and-retry race).
    """
    row = conn.execute(
        "SELECT id, locked, confidence, source_memory FROM kg_facts "
        "WHERE subject = ? AND predicate = ? AND object = ?",
        (subject.lower(), predicate, obj.lower()),
    ).fetchone()
    if row:
        if not row[1]:
            new_conf = max(row[2], confidence)
            conn.execute(
                "UPDATE kg_facts SET last_seen = ?, mention_count = mention_count + 1, "
                "confidence = ?, source_memory = ? WHERE id = ?",
                (now, new_conf, source_memory or row[3], row[0]),
            )
        else:
            conn.execute(
                "UPDATE kg_facts SET last_seen = ? WHERE id = ?",
                (now, row[0]),
            )
        return int(row[0])
    # C2 fix: handle the race where another writer INSERTs the same
    # (subject, predicate, object) between our SELECT and INSERT. The
    # UNIQUE constraint on kg_facts (line 56) raises sqlite3.IntegrityError;
    # we recover by re-SELECTing and falling into the merge path above.
    subj_entity_id = None
    obj_entity_id = None
    try:
        subj_entity_id = conn.execute(
            "SELECT id FROM kg_entities WHERE name = ?", (subject.lower(),)
        ).fetchone()
        obj_entity_id = conn.execute(
            "SELECT id FROM kg_entities WHERE name = ?", (obj.lower(),)
        ).fetchone()
    except sqlite3.OperationalError:
        pass
    try:
        cur = conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, "
            "first_seen, last_seen, source_memory, context, "
            "subject_entity_id, object_entity_id, "
            "event_time, event_time_granularity, transaction_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                subject.lower(),
                predicate,
                obj.lower(),
                confidence,
                now,
                now,
                source_memory,
                context[:500],
                subj_entity_id[0] if subj_entity_id else None,
                obj_entity_id[0] if obj_entity_id else None,
                event_time,
                event_time_granularity,
                now,  # transaction_time: when we learned it
            ),
        )
        # lastrowid is None only when no row was inserted, but a successful
        # INSERT guarantees it's set.  Cast for the type checker.
        assert cur.lastrowid is not None
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id, locked, confidence, source_memory FROM kg_facts "
            "WHERE subject = ? AND predicate = ? AND object = ?",
            (subject.lower(), predicate, obj.lower()),
        ).fetchone()
        if row is None:
            # Vanishingly rare: someone DELETEd between our failed
            # INSERT and re-SELECT. Just bail; the next save will retry.
            return None
        if not row[1]:
            new_conf = max(row[2], confidence)
            conn.execute(
                "UPDATE kg_facts SET last_seen = ?, mention_count = mention_count + 1, "
                "confidence = ?, source_memory = ? WHERE id = ?",
                (now, new_conf, source_memory or row[3], row[0]),
            )
        else:
            conn.execute(
                "UPDATE kg_facts SET last_seen = ? WHERE id = ?",
                (now, row[0]),
            )
        return int(row[0])


def _extract_facts_via_llm(
    conn: sqlite3.Connection, memory_id: str, content: str
) -> list[tuple[str, str, str, float]]:
    """Run the hybrid LLM-extraction path. Returns the extracted facts.

    Encapsulates the P3.3 hybrid decision (``_should_use_llm_for_memory``)
    plus the ``try/except`` around ``extract_facts_via_llm``. Returns
    an empty list when LLM is disabled, the memory doesn't qualify,
    or the LLM call fails — the caller falls back to regex.

    This helper exists so that ``index_facts_for_memory`` (per-save,
    may-use-LLM) and ``index_facts_for_memory_bulk`` (backfill,
    regex-only) can share the LLM call shape without sharing the
    decision to use LLM. The bulk variant simply never calls this
    helper, which is structurally easier to audit than a ``force_regex``
    flag inside the per-save function.
    """
    if not _should_use_llm_for_memory(conn, memory_id):
        return []
    try:
        from llm_extraction import extract_facts_via_llm

        llm_facts = extract_facts_via_llm(content)
        if llm_facts:
            return llm_facts
    except Exception:
        logger.warning(
            "LLM fact extraction failed for memory %s, falling back to regex",
            memory_id,
        )
    return []


def _process_extracted_facts(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    facts: list[tuple[str, str, str, float]],
) -> dict:
    """Common fact persistence pipeline after extraction.

    Handles event_time extraction, fact upsert, temporal KG
    supersession reconciliation, stale-fact invalidation, and
    audit logging. Used by both index_facts_for_memory (LLM+regex)
    and index_facts_for_memory_bulk (regex-only).
    """
    # T8 (temporal-kg plan): gate the entire temporal subsystem
    # (event_time extraction, contradiction reconciliation, edit
    # invalidation, audit log) behind a feature flag.  Default ON
    # (see memory.toml [features] feature_temporal_kg = true).  When
    # disabled, fact extraction still works but no event_time is
    # stored and no supersession/invalidation logic runs.
    temporal_kg_enabled = True
    if get_config is not None:
        try:
            temporal_kg_enabled = bool(get_config().feature_temporal_kg)
        except Exception:
            logger.warning("feature_temporal_kg read failed; defaulting to ON")
    if os.environ.get("MEMORY_TEMPORAL_KG") == "0":
        temporal_kg_enabled = False

    if temporal_kg_enabled:
        # T2.5: extract event_time once per memory and apply it to
        # every fact.  The memory-level time is the best signal we
        # have without per-fact LLM reasoning; per-fact time from the
        # LLM (T2.3-T2.4) is captured in the prompt and parsed out,
        # but for now we apply the same memory-level time to all facts.
        event_time, event_time_granularity = extract_event_time(content)
    else:
        event_time, event_time_granularity = None, None

    # Imported lazily here to avoid a circular import (fact_temporal
    # could in the future import from fact_extraction for entity
    # resolution, and we don't need it loaded at module import time
    # for hooks that skip fact extraction).
    if temporal_kg_enabled:
        from fact_temporal import (
            audit_fact_temporal_event,
            invalidate_stale_facts,
            reconcile_fact_supersession,
        )

    # Capture the set of new (subject, predicate, object) triples for
    # the diff in T5.
    new_fact_keys: set[tuple[str, str, str]] = {
        (s.lower(), p, o.lower()) for s, p, o, _ in facts
    }

    now = time.time()

    for subj, pred, obj, conf in facts:
        fact_id = _upsert_fact(
            conn,
            subj,
            pred,
            obj,
            conf,
            now,
            memory_id,
            content[:200],
            event_time=event_time,
            event_time_granularity=event_time_granularity,
        )
        # T3.4: supersession reconciliation (gated by feature_temporal_kg)
        if fact_id is not None and temporal_kg_enabled:
            try:
                reconcile_fact_supersession(conn, fact_id)
            except Exception:
                # Reconciliation is best-effort: a failure here must not
                # break the save path.  Log and move on; the next save
                # will re-attempt.
                logger.warning(
                    "fact_temporal: reconciliation failed for fact %d "
                    "(memory %s); continuing",
                    fact_id,
                    memory_id,
                )

    # T5.1-T5.3: invalidate facts that were attributed to this memory
    # but are no longer in the new content.  Gated by feature_temporal_kg.
    if temporal_kg_enabled and new_fact_keys:
        try:
            invalidated = invalidate_stale_facts(conn, memory_id, new_fact_keys)
            # T5.4: audit log each invalidation
            for fid in invalidated:
                row = conn.execute(
                    "SELECT subject, predicate, object FROM kg_facts WHERE id = ?",
                    (fid,),
                ).fetchone()
                if row is not None:
                    try:
                        audit_fact_temporal_event(
                            conn,
                            event="invalidate",
                            fact_id=fid,
                            reason="manual",
                            subject=row[0],
                            predicate=row[1],
                            obj=row[2],
                            memory_id=memory_id,
                        )
                    except Exception:
                        logger.warning(
                            "fact_temporal: audit log failed for fact %d (memory %s)",
                            fid,
                            memory_id,
                        )
        except Exception:
            # T5 is best-effort: a failure here must not break the
            # save path.
            logger.warning(
                "fact_temporal: invalidation failed for memory %s; continuing",
                memory_id,
            )
    return {"facts": len(facts)}


def index_facts_for_memory(
    conn: sqlite3.Connection, memory_id: str, content: str
) -> dict:
    # Use config system for feature flag
    if get_config is not None:
        if not get_config().knowledge_graph:
            return {"facts": 0}
        if os.environ.get("MEMORY_KNOWLEDGE_GRAPH") == "0":
            return {"facts": 0}
    # Skip operational categories (sessions/auto-*, sessions/audit-*,
    # sessions/compaction-*, sessions/idle-*, sessions/end-*). These
    # are tool transcripts and audit events with no natural language
    # content — extracting facts produces only boilerplate noise.
    # Override via MEMORY_FACT_EXTRACTION_INCLUDE_OPERATIONAL=1.
    if _should_skip_category(memory_id):
        return {"facts": 0}
    # Skip auto-save tool-log notes — they are ephemeral tool transcripts
    # that produce only boilerplate facts ("tool has_description ...",
    # "time before compaction has_value ..."). Daily-digest rolled-up
    # notes do NOT have this marker and are processed normally.
    if memory_id and "auto_save" in (content[:200] if content else ""):
        return {"facts": 0}
    if content and "*Auto-generated by auto_save.py*" in content[:500]:
        return {"facts": 0}
    # P3.3 (2026-06-19): hybrid strategy — regex default, LLM only for
    # high-value memories (pinned OR importance_score >= threshold).
    # This keeps the LLM cost bounded (~minutes vs ~hours) while still
    # giving premium extraction to the most important notes.
    #
    # Env vars:
    #   MEMORY_LLM_FORCE=1               — always use LLM (override hybrid)
    # P3.3 (2026-06-19): hybrid strategy — LLM only for high-value
    # memories (pinned OR importance_score >= threshold). The LLM
    # call is encapsulated in _extract_facts_via_llm so the per-save
    # path (this function) and the bulk path
    # (index_facts_for_memory_bulk) can share the shape without
    # sharing the decision. The bulk variant simply doesn't call
    # the helper, which is structurally easier to audit than a
    # force_regex override.
    #
    # Env vars:
    #   MEMORY_LLM_FORCE=1               — always use LLM (override hybrid)
    #   MEMORY_LLM_HYBRID_THRESHOLD=0.5  — importance_score cutoff (default 0.5)
    #   MEMORY_LLM_HYBRID=0              — disable hybrid, never use LLM
    facts = _extract_facts_via_llm(conn, memory_id, content)
    if not facts:
        facts = extract_facts(content)

    return _process_extracted_facts(conn, memory_id, content, facts)


def index_facts_for_memory_bulk(
    conn: sqlite3.Connection, memory_id: str, content: str
) -> dict:
    """Bulk fact indexing — regex-only by design.

    Use this for any context that iterates over many memories: heartbeat
    drift recovery, manual backfills, migration scripts, etc. The
    function NEVER calls the LLM extractor — no model load, no inference
    per-memory, no loky-worker deadlock. For per-save LLM extraction,
    use ``index_facts_for_memory`` instead.

    Background: 2026-06-26 cron_heartbeat.py hung at 66% CPU after
    loading the Qwen 3B LLM. The hang was caused by a per-memory LLM
    call inside a bulk backfill loop (5,000+ memories, hundreds above
    the 0.5 importance threshold). The 2026-06-26 fix added
    ``force_regex=True`` to ``index_facts_for_memory``; this bulk
    variant makes the deny-by-exception unnecessary by making the
    LLM path physically unreachable from a backfill call site.

    Same skip/feature-flag/temporal-kg contract as
    ``index_facts_for_memory`` — only the LLM call is omitted.
    """
    # Use config system for feature flag
    if get_config is not None:
        if not get_config().knowledge_graph:
            return {"facts": 0}
        if os.environ.get("MEMORY_KNOWLEDGE_GRAPH") == "0":
            return {"facts": 0}
    # Skip operational categories (sessions/auto-*, sessions/audit-*,
    # sessions/compaction-*, sessions/idle-*, sessions/end-*). These
    # are tool transcripts and audit events with no natural language
    # content — extracting facts produces only boilerplate noise.
    # Override via MEMORY_FACT_EXTRACTION_INCLUDE_OPERATIONAL=1.
    if _should_skip_category(memory_id):
        return {"facts": 0}
    # Skip auto-save tool-log notes — they are ephemeral tool transcripts
    # that produce only boilerplate facts ("tool has_description ...",
    # "time before compaction has_value ..."). Daily-digest rolled-up
    # notes do NOT have this marker and are processed normally.
    if memory_id and "auto_save" in (content[:200] if content else ""):
        return {"facts": 0}
    if content and "*Auto-generated by auto_save.py*" in content[:500]:
        return {"facts": 0}
    # Bulk path: regex only. The LLM extractor is never called here.
    # This is the structural fix for the 2026-06-26 hang — the
    # previous ``force_regex=True`` flag was a deny-by-exception
    # workaround; this function makes the LLM path physically
    # unreachable by not calling it.
    facts = extract_facts(content)

    return _process_extracted_facts(conn, memory_id, content, facts)


def _should_use_llm_for_memory(conn: sqlite3.Connection, memory_id: str) -> bool:
    """Hybrid strategy: return True iff this memory deserves LLM extraction.

    Returns True (use LLM) when:
      - MEMORY_LLM_FORCE=1 (force LLM on every memory), OR
      - Memory is pinned (pinned=1), OR
      - Memory's importance_score >= MEMORY_LLM_HYBRID_THRESHOLD (default 0.5)

    Returns False (regex only) when:
      - MEMORY_LLM_HYBRID=0 (disable hybrid, never use LLM), OR
      - Memory is not pinned AND importance_score < threshold, OR
      - LLM extraction is disabled at the config level

    On any error (memory not found, no importance column, etc.) — defaults
    to regex (False) so we never crash the save path.
    """
    # Config-level gates
    try:
        from llm_extraction import is_llm_extraction_available

        if not is_llm_extraction_available():
            return False
    except Exception:
        logger.warning("Failed to check LLM extraction availability")
        return False

    # Env-var shortcuts
    if os.environ.get("MEMORY_LLM_HYBRID") == "0":
        return False
    if os.environ.get("MEMORY_LLM_FORCE") == "1":
        return True

    # Resolve threshold (default 0.5). Check config first, then env.
    threshold = 0.5
    try:
        from config import get_config

        threshold = float(get_config().llm_extraction_hybrid_threshold)
    except Exception:
        logger.warning("Failed to read LLM extraction hybrid threshold from config")
        pass
    try:
        env_threshold = os.environ.get("MEMORY_LLM_HYBRID_THRESHOLD")
        if env_threshold:
            threshold = float(env_threshold)
    except ValueError:
        pass

    # Resolve force flag. Check config first, then env.
    force = False
    try:
        from config import get_config

        force = bool(get_config().llm_extraction_force)
    except Exception:
        logger.warning("Failed to read LLM extraction force flag from config")
        pass
    if os.environ.get("MEMORY_LLM_FORCE") == "1":
        force = True

    if force:
        return True

    # Look up the memory's pinned status + importance_score
    try:
        row = conn.execute(
            "SELECT pinned, importance_score FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
    except Exception:
        logger.warning(
            "Failed to query memory %s for LLM extraction eligibility", memory_id
        )
        return False

    if row is None:
        return False
    pinned, importance_score = row
    if pinned:
        return True
    if importance_score is not None and importance_score >= threshold:
        return True
    return False


# ---------------------------------------------------------------------------
# Fact Locking / Unlocking
# ---------------------------------------------------------------------------


def lock_fact(conn: sqlite3.Connection, subject: str, predicate: str, obj: str) -> bool:
    """Lock a fact so consolidation / dedup can't merge or remove it.

    Locked facts still appear in search results but are exempt from
    automatic quality filtering. Returns True if a fact was locked.
    """
    cur = conn.execute(
        "UPDATE kg_facts SET locked = 1 WHERE subject = ? AND predicate = ? AND object = ?",
        (subject.lower(), predicate, obj.lower()),
    )
    return cur.rowcount > 0


def unlock_fact(
    conn: sqlite3.Connection, subject: str, predicate: str, obj: str
) -> bool:
    """Unlock a previously-locked fact. Returns True if a fact was
    unlocked, False if the fact doesn't exist or wasn't locked.
    """
    cur = conn.execute(
        "UPDATE kg_facts SET locked = 0 WHERE subject = ? AND predicate = ? AND object = ?",
        (subject.lower(), predicate, obj.lower()),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Fact Search
# ---------------------------------------------------------------------------


def _build_fts_query(query_lower: str) -> str | None:
    """Build an FTS5 OR-joined query string from a user search.

    Each whitespace-separated token is wrapped in double quotes and OR-joined.
    FTS5 special characters (`*`, `^`) are stripped from tokens to avoid
    syntax errors on untrusted input.  Returns None for an empty/blank query.
    """
    tokens = query_lower.split()
    if not tokens:
        return None
    safe: list[str] = []
    for t in tokens:
        # Strip FTS5 operators that would change query semantics.
        t = t.replace('"', '""').replace("*", "").replace("^", "")
        t = t.strip()
        if t:
            safe.append(f'"{t}"')
    if not safe:
        return None
    return " OR ".join(safe)


def _facts_search_fts(
    conn: sqlite3.Connection, fts_query: str, limit: int
) -> list[sqlite3.Row] | None:
    """FTS5-backed fact search.

    Returns up to `limit` rows ordered by FTS5 BM25 rank.  Returns None on
    any FTS5 error (caller falls back to LIKE).  The SELECT is column-stable
    with the LIKE fallback so downstream scoring is identical.
    """
    try:
        rows = conn.execute(
            "SELECT kf.id, kf.subject, kf.predicate, kf.object, kf.confidence, "
            "kf.locked, kf.first_seen, kf.last_seen, kf.mention_count, "
            "kf.source_memory "
            "FROM kg_facts_fts "
            "JOIN kg_facts kf ON kf.rowid = kg_facts_fts.rowid "
            "WHERE kg_facts_fts MATCH ? "
            "ORDER BY kg_facts_fts.rank "
            "LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        return rows
    except Exception:
        logger.warning(
            "FTS5 fact search failed; falling back to LIKE scan", exc_info=True
        )
        return None


def _facts_search_like(
    conn: sqlite3.Connection, query_lower: str, limit: int
) -> list[sqlite3.Row]:
    """Original LIKE-based fact search.  Fallback for pre-v20 DBs and FTS5
    syntax errors.  O(n) full table scan due to leading-wildcard LIKE."""
    return conn.execute(
        "SELECT id, subject, predicate, object, confidence, locked, "
        "first_seen, last_seen, mention_count, source_memory "
        "FROM kg_facts "
        "WHERE subject LIKE ? OR predicate LIKE ? OR object LIKE ? "
        "LIMIT ?",
        (f"%{query_lower}%", f"%{query_lower}%", f"%{query_lower}%", limit),
    ).fetchall()


def facts_search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    query_lower = query.lower().strip()
    now = time.time()
    half_life = 180 * 86400

    if not query_lower:
        return []

    fts_query = _build_fts_query(query_lower)
    rows: list | None = None
    if fts_query is not None:
        rows = _facts_search_fts(conn, fts_query, limit * 3)
    if not rows:
        rows = _facts_search_like(conn, query_lower, limit * 3)

    def _effective(conf: float, locked: int, last_seen: float) -> float:
        if locked:
            return conf
        age = now - (last_seen or now)
        return conf * math.pow(0.5, age / half_life)

    scored = []
    for r in rows:
        eff = _effective(r[4], r[5], r[7])
        scored.append(
            (
                eff,
                {
                    "id": r[0],
                    "subject": r[1],
                    "predicate": r[2],
                    "object": r[3],
                    "confidence": r[4],
                    "locked": bool(r[5]),
                    "first_seen": r[6],
                    "last_seen": r[7],
                    "mention_count": r[8],
                    "source_memory": r[9],
                    "effective_confidence": round(eff, 4),
                },
            )
        )
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s[1] for s in scored[:limit]]


def facts_list(
    conn: sqlite3.Connection, limit: int = 20, min_confidence: float = 0.0
) -> list[dict]:
    rows = conn.execute(
        "SELECT id, subject, predicate, object, confidence, locked, "
        "first_seen, last_seen, mention_count, source_memory "
        "FROM kg_facts WHERE confidence >= ? "
        "ORDER BY confidence DESC, mention_count DESC LIMIT ?",
        (min_confidence, limit),
    ).fetchall()
    return [
        {
            "id": r[0],
            "subject": r[1],
            "predicate": r[2],
            "object": r[3],
            "confidence": r[4],
            "locked": bool(r[5]),
            "first_seen": r[6],
            "last_seen": r[7],
            "mention_count": r[8],
            "source_memory": r[9],
        }
        for r in rows
    ]


def facts_stats(conn: sqlite3.Connection) -> dict:
    try:
        total = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
        locked = conn.execute(
            "SELECT COUNT(*) FROM kg_facts WHERE locked = 1"
        ).fetchone()[0]
        predicates = {}
        for row in conn.execute(
            "SELECT predicate, COUNT(*) FROM kg_facts GROUP BY predicate"
        ).fetchall():
            predicates[row[0]] = row[1]
        avg_conf = (
            conn.execute("SELECT AVG(confidence) FROM kg_facts").fetchone()[0] or 0.0
        )
        return {
            "total_facts": total,
            "locked_facts": locked,
            "avg_confidence": round(avg_conf, 4),
            "predicate_distribution": predicates,
        }
    except sqlite3.OperationalError:
        return {
            "total_facts": 0,
            "locked_facts": 0,
            "error": "facts table not initialized",
        }


# ---------------------------------------------------------------------------
# DB-lifecycle wrappers (T3-item3: push conn mgmt out of MCP layer)
# ---------------------------------------------------------------------------


def facts_search_db(db_path: str | Path, query: str, limit: int = 10) -> list[dict]:
    """facts_search with connection lifecycle managed."""
    from memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_facts_schema(conn)
    try:
        return facts_search(conn, query, limit=limit)
    finally:
        safe_close_db(conn)


def facts_list_db(
    db_path: str | Path, limit: int = 20, min_confidence: float = 0.0
) -> list[dict]:
    """facts_list with connection lifecycle managed."""
    from memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_facts_schema(conn)
    try:
        return facts_list(conn, limit=limit, min_confidence=min_confidence)
    finally:
        safe_close_db(conn)


def facts_stats_db(db_path: str | Path) -> dict:
    """facts_stats with connection lifecycle managed."""
    from memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_facts_schema(conn)
    try:
        return facts_stats(conn)
    finally:
        safe_close_db(conn)
