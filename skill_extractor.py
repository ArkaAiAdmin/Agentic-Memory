"""Skill Extraction — automatic conversion of memories into reusable skills.

Implements the "skill agent" principle from the brain-model approach:
when a memory contains a reusable procedure, extract it as a skill
that can be cached and retrieved without re-running expensive RAG.

A skill is a structured representation of:
  - topic: the skill's name (e.g., "install-ubuntu-proxmox")
  - source_memory_id: which memory the skill was extracted from
  - triggers: keywords that activate the skill
  - steps: ordered list of actions to perform
  - hit_count: how many times the skill has been used
  - created_at / last_used_at: temporal metadata

This is the brain's "I figured this out once, I'll never figure it out
again" pattern. RAG gets us close; skills get us exact.

P0 fix #5: lowered the skill_worthy threshold from 2 procedural
signals to 1 strong signal (code block, numbered steps, command,
or action-verb header), and added a positive/negative signal model
that mirrors the user's spec (code blocks, numbered steps,
imperative verbs, procedure category tag). 1 strong signal is
sufficient as long as the content is not a pure observation/decision
note.

Zero dependencies on other memory modules. Pure Python.
"""

from __future__ import annotations

import re
import time
import sqlite3
from typing import Optional

# Use the config system for feature flags
try:
    from _lazy_imports import get_config
except ImportError:
    get_config = None

__all__ = [
    "ensure_skill_schema",
    "extract_skill_from_memory",
    "save_skill",
    "search_skills",
    "record_skill_hit",
    "list_skills",
    "is_skill_worthy",
]

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SKILL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_skills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    source_memory_id TEXT,
    topic           TEXT,
    description     TEXT,
    triggers        TEXT DEFAULT '[]',
    steps           TEXT DEFAULT '[]',
    content_hash    TEXT,
    hit_count       INTEGER DEFAULT 0,
    last_used_at    REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_skills_topic ON memory_skills(topic);
CREATE INDEX IF NOT EXISTS idx_memory_skills_hit ON memory_skills(hit_count DESC);
"""


def ensure_skill_schema(conn: sqlite3.Connection) -> None:
    """Create the memory_skills table if it doesn't exist. Idempotent."""
    conn.executescript(_SKILL_SCHEMA_SQL)
    conn.commit()


# ---------------------------------------------------------------------------
# Heuristics for skill detection
# ---------------------------------------------------------------------------
# P0 fix #5: split the procedural signal patterns into STRONG vs WEAK
# tiers. A single STRONG signal (code block, numbered step, or shell
# command) is now enough to qualify a memory as skill-worthy, mirroring
# the user's spec ("1 signal OR more lenient detection"). WEAK signals
# (action-verb header, bullet-with-bold) contribute to a sum-of-signals
# path so a lessons note that doesn't have literal code but does have
# two structural markers still qualifies.

# STRONG procedural signals — any one of these makes the memory
# skill-worthy (subject to the non-skill veto below).
_STRONG_PROCEDURAL_PATTERNS = [
    re.compile(
        r"```(?:bash|sh|shell|python|py|sql|yaml|json|js|ts|go|rust|java|c|cpp|html|css|diff|patch|toml|ini|env)\b",
        re.IGNORECASE,
    ),  # any fenced code block — these are extremely strong procedural markers
    re.compile(
        r"^#{1,4}\s*(?:step\s+)?\d+[\.\):]",
        re.MULTILINE | re.IGNORECASE,
    ),  # "## 1.", "### Step 1:"
    re.compile(
        r"^#{1,4}\s*step\s+\d+",
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(r"^\s*\$\s+\S+", re.MULTILINE),  # shell command "$ cmd"
    re.compile(r"^\s*sudo\s+\S+", re.MULTILINE),  # sudo commands
    re.compile(
        r"^\s*\d+[\.\)]\s+[A-Z]", re.MULTILINE
    ),  # plain numbered list: "1. First..."
]

# WEAK procedural signals — accumulate; 2+ weak signals also qualify.
_WEAK_PROCEDURAL_PATTERNS = [
    re.compile(
        r"^#{1,4}\s+(install|setup|configure|build|deploy|create|run|execute|test|verify|check|fix|add|remove|update|delete|migrate|setup|enable|disable|start|stop|restart|connect|install|clone|build|publish|release|tag|init|scaffold|bootstrap|provision|wire|register|configure)\b",
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(
        r"^[-*]\s+\*\*[^*]+\*\*\s*:", re.MULTILINE
    ),  # "- **Action**: description" — strict colon form
    re.compile(
        r"^\s*```[a-zA-Z]+",
        re.MULTILINE,
    ),  # any other code block (no language tag)
    re.compile(
        r"\b(create|add|fix|install|configure|deploy|build|setup|run|execute|test|verify|check|remove|update|delete|migrate|enable|disable)\s+\w+",
        re.IGNORECASE,
    ),  # imperative verb in body
]

# Patterns that suggest a fact / observation (not a skill) — these VETO
# the positive signal even when a STRONG signal is present, because
# observations can contain code (e.g. "the system returned this error")
# without being a reusable procedure.
NON_SKILL_PATTERNS = [
    re.compile(
        r"^#{1,3}\s+(decision|rationale|note|todo|summary|background|context|observation|findings?)\b",
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(
        r"^#{1,3}\s+what\s+(?:is|are|was|were)\b",
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(
        r"^#{1,3}\s+why\b",
        re.MULTILINE | re.IGNORECASE,
    ),
]

# Auto-save session markers — these are raw session dumps from the
# auto_save hook. They contain JSON code blocks but no actual
# procedure. Filter them out so the extractor doesn't churn through
# thousands of identical "Auto-save: bash" entries.
_AUTO_SAVE_MARKER_RE = re.compile(
    r"^\*\*Auto-save\*\*\s*:", re.MULTILINE | re.IGNORECASE
)
_AUTO_SAVE_HEADER_RE = re.compile(
    r"^#\s+Auto-save:\s+\w+\s+@", re.MULTILINE | re.IGNORECASE
)


# Backwards-compat alias for callers (and tests) that imported the
# old name.
PROCEDURAL_PATTERNS = _STRONG_PROCEDURAL_PATTERNS + _WEAK_PROCEDURAL_PATTERNS
_CODE_BLOCK_RE = _STRONG_PROCEDURAL_PATTERNS[0]


def is_skill_worthy(content: str, category: str = "") -> bool:
    """Heuristic: should this memory be turned into a skill?

    P0 fix #5: the threshold was lowered from 2 procedural signals to
    a single STRONG signal (code block, numbered step, or shell
    command). Two WEAK signals (action-verb header, imperative verb
    in body, etc.) also qualify. A non-skill veto (decision /
    rationale / observation headers) suppresses the result, and
    auto-save session dumps are filtered out before the heuristic
    even runs.

    Args:
        content: the memory's raw content (markdown or plain text).
        category: optional category tag from the memories table
            (lessons, projects, decisions, etc.). Used to give a
            small bias toward procedural categories.
    """
    if not content or len(content) < 25:
        return False
    # Filter out auto-save session dumps — they have code blocks but
    # no actual procedure. Without this filter, the 8,000+ session
    # entries would flood memory_skills with duplicates.
    if _AUTO_SAVE_MARKER_RE.search(content) or _AUTO_SAVE_HEADER_RE.search(content):
        return False
    strong = sum(1 for p in _STRONG_PROCEDURAL_PATTERNS if p.search(content))
    weak = sum(1 for p in _WEAK_PROCEDURAL_PATTERNS if p.search(content))
    # Category-based bias: lessons/ and projects/ are procedurally
    # skewed in this codebase, so they get a half-signal boost.
    cat = (category or "").lower().strip()
    if cat in ("lessons", "projects", "procedures", "howto", "how-to"):
        weak += 1
    elif cat in ("decisions",):
        # Decisions are observations about past calls, not skills.
        return False
    if strong >= 1:
        # One strong signal is enough.
        pass
    elif weak >= 2:
        # Two weak signals also qualify.
        pass
    else:
        return False
    # Non-skill veto: a dominant decision/observation header blocks
    # the result even with a strong signal.
    non_skill = sum(1 for p in NON_SKILL_PATTERNS if p.search(content))
    if non_skill > 0 and strong == 0:
        return False
    return True


def _extract_topic_from_content(content: str) -> str:
    """Extract a topic slug from the first heading or first 60 chars."""
    # Try first markdown heading
    m = re.search(r"^#+\s+(.+?)[\n:]", content, re.MULTILINE)
    if m:
        heading = m.group(1).strip()
        # Slugify: lowercase, replace non-alphanumeric with -
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
        if len(slug) > 3 and len(slug) < 80:
            return slug[:80]
    # Fallback: first 60 chars
    slug = re.sub(r"[^a-z0-9]+", "-", content[:60].lower()).strip("-")
    return slug[:80] or "untitled-skill"


def _extract_triggers(content: str, topic: str) -> list[str]:
    """Extract trigger keywords from content. Used for skill-first lookup."""
    triggers = set()
    # Topic is always a trigger
    for word in re.split(r"[-_\s]+", topic):
        if len(word) >= 3:
            triggers.add(word)
    # Extract from headers
    for m in re.finditer(r"^#+\s+(.+?)[\n:]", content, re.MULTILINE):
        for word in re.findall(r"\b[a-zA-Z]{4,}\b", m.group(1)):
            triggers.add(word.lower())
    # Extract bold keywords (often important in procedural docs)
    for m in re.finditer(r"\*\*([^*]+)\*\*", content):
        for word in re.findall(r"\b[a-zA-Z]{4,}\b", m.group(1)):
            triggers.add(word.lower())
    return sorted(triggers)[:20]


def _extract_steps(content: str) -> list[str]:
    """Extract ordered steps from a procedural memory.

    Returns a list of step strings. If no clear steps, returns the
    raw paragraphs as a single-item list (skill is a single chunk of
    procedural knowledge).
    """
    # Try numbered steps first
    step_pattern = re.compile(
        r"^(?:##\s*(?:step\s+)?\d+[\.\):]\s*(.+)|[-*]\s+(?:\*\*[^*]+\*\*:?\s*)?(.+))$",
        re.MULTILINE | re.IGNORECASE,
    )
    steps = []
    for m in step_pattern.finditer(content):
        text = (m.group(1) or m.group(2) or "").strip()
        if text and len(text) > 5:
            steps.append(text)
    if steps:
        return steps
    # Fallback: split by paragraphs
    paragraphs = [
        p.strip() for p in content.split("\n\n") if p.strip() and len(p.strip()) > 20
    ]
    return paragraphs[:10] if paragraphs else [content[:500]]


def _content_hash(content: str) -> str:
    """Stable hash for deduplication of skill content."""
    import hashlib

    normalized = re.sub(r"\s+", " ", content.lower().strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_skill_from_memory(
    memory_id: str, content: str, category: str = ""
) -> Optional[dict]:
    """If the memory is skill-worthy, return a dict describing the skill.

    Returns None if the memory isn't procedural. The dict contains:
      - name: slug-style unique identifier
      - topic: human-readable topic
      - description: short description (first sentence of content)
      - triggers: list of keyword strings
      - steps: list of step strings
      - content_hash: hash for dedup

    P0 fix #5: now accepts an optional ``category`` argument that is
    forwarded to ``is_skill_worthy`` so the lower-threshold detector
    can apply the per-category bias (lessons/ and projects/ get a
    half-signal boost).
    """
    # Derive a category fallback from the memory id prefix when not
    # supplied. Many older memories predate the category column.
    if not category and "/" in memory_id:
        category = memory_id.split("/", 1)[0]
    if not is_skill_worthy(content, category=category):
        return None

    topic = _extract_topic_from_content(content)
    name = (
        re.sub(r"[^a-z0-9-]+", "-", topic.lower()).strip("-")
        or f"skill-{memory_id[:8]}"
    )
    triggers = _extract_triggers(content, topic)
    steps = _extract_steps(content)
    # First sentence as description (max 200 chars)
    description = re.split(r"[.\n]", content.strip())[0].strip()[:200]
    return {
        "name": name,
        "source_memory_id": memory_id,
        "topic": topic,
        "description": description,
        "triggers": triggers,
        "steps": steps,
        "content_hash": _content_hash(content),
    }


def save_skill(conn: sqlite3.Connection, skill: dict) -> int:
    """Insert or update a skill in the memory_skills table. Returns the skill id.

    If a skill with the same name exists, updates it (idempotent re-extraction).
    """
    import json

    now = time.time()
    existing = conn.execute(
        "SELECT id, content_hash FROM memory_skills WHERE name = ?", (skill["name"],)
    ).fetchone()

    if existing:
        skill_id, prev_hash = existing
        # If content didn't change, no-op
        if prev_hash == skill.get("content_hash"):
            return int(skill_id)
        conn.execute(
            """UPDATE memory_skills
               SET source_memory_id = ?, topic = ?, description = ?,
                   triggers = ?, steps = ?, content_hash = ?,
                   updated_at = ?
               WHERE id = ?""",
            (
                skill.get("source_memory_id"),
                skill.get("topic"),
                skill.get("description"),
                json.dumps(skill.get("triggers", [])),
                json.dumps(skill.get("steps", [])),
                skill.get("content_hash"),
                now,
                skill_id,
            ),
        )
        conn.commit()
        return int(skill_id)

    cur = conn.execute(
        """INSERT INTO memory_skills
           (name, source_memory_id, topic, description, triggers, steps,
            content_hash, hit_count, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (
            skill["name"],
            skill.get("source_memory_id"),
            skill.get("topic"),
            skill.get("description"),
            json.dumps(skill.get("triggers", [])),
            json.dumps(skill.get("steps", [])),
            skill.get("content_hash"),
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def search_skills(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    """Skill-first search: returns skills whose triggers match the query.

    This is the "indexed memory" path — the system has already figured
    this out, so we return the cached skill instead of running RAG.

    Matching: simple substring + token match against triggers.
    """
    import json

    if not query or not query.strip():
        return []
    # Tokenize query
    query_tokens = set(re.findall(r"\b[a-zA-Z]{3,}\b", query.lower()))
    if not query_tokens:
        return []
    rows = conn.execute(
        """SELECT id, name, topic, description, triggers, steps,
                  hit_count, last_used_at, created_at
           FROM memory_skills
           ORDER BY hit_count DESC, created_at DESC"""
    ).fetchall()
    results = []
    for row in rows:
        rid, name, topic, desc, triggers_json, steps_json, hits, last_used, created = (
            row
        )
        try:
            triggers = set(json.loads(triggers_json or "[]"))
        except (json.JSONDecodeError, TypeError):
            triggers = set()
        # Score: number of matching trigger tokens
        overlap = query_tokens & triggers
        if not overlap:
            # Fallback: topic or name substring
            if any(t in (topic or "").lower() for t in query_tokens):
                score = 1
            else:
                continue
        else:
            score = len(overlap)
        results.append(
            {
                "id": rid,
                "name": name,
                "topic": topic or "",
                "description": desc or "",
                "score": score,
                "matched_triggers": sorted(overlap),
                "hit_count": hits or 0,
                "last_used_at": last_used,
                "steps": json.loads(steps_json or "[]"),
            }
        )
    results.sort(key=lambda r: (r["score"], r["hit_count"]), reverse=True)
    return results[:limit]


def record_skill_hit(conn: sqlite3.Connection, skill_id: int) -> None:
    """Record that a skill was used. Increments hit_count and updates last_used_at."""
    conn.execute(
        """UPDATE memory_skills
           SET hit_count = hit_count + 1, last_used_at = ?
           WHERE id = ?""",
        (time.time(), skill_id),
    )
    conn.commit()


def list_skills(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """List all skills, ordered by hit_count desc (most-used first)."""

    rows = conn.execute(
        """SELECT id, name, topic, description, hit_count, last_used_at, created_at
           FROM memory_skills
           ORDER BY hit_count DESC, created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "topic": r[2] or "",
            "description": r[3] or "",
            "hit_count": r[4] or 0,
            "last_used_at": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


def extract_skill_for_memory(
    conn: sqlite3.Connection,
    note_id: str,
    content: str,
    category: str = "",
) -> dict | None:
    """Best-effort skill extraction for a single memory (post-save hook helper).

    Ensures the skill schema exists, checks if the memory is skill-worthy,
    extracts the skill, and persists it (idempotent via content_hash).
    Returns the skill dict on success, None if not skill-worthy or on error.
    Swallows all exceptions so the save path never fails because of skill
    extraction.

    P0 fix #5: accepts a ``category`` argument that is forwarded to
    ``is_skill_worthy`` so the lower-threshold detector can apply its
    per-category bias.
    """
    try:
        ensure_skill_schema(conn)
        if not category and "/" in note_id:
            category = note_id.split("/", 1)[0]
        skill = extract_skill_from_memory(note_id, content, category=category)
        if skill is None:
            return None
        save_skill(conn, skill)
        conn.commit()
        return skill
    except Exception:
        return None
