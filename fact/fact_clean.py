"""Text preprocessing, cleaning, validation, and pattern definitions
for fact extraction.  No SQLite dependency — pure text processing.
"""

from __future__ import annotations

import calendar
import logging
import math
import os
import re
import time

logger = logging.getLogger(__name__)

try:
    from config import get_config
except ImportError:
    get_config = None  # type: ignore[assignment]

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
