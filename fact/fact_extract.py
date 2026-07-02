"""Fact extraction orchestrator for agentic-memory.

Layer-based SPO extraction from markdown text, fact upsert,
temporal-KG integration, and fact locking/unlocking.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time

from .fact_clean import (
    _preprocess,
    _clean,
    _strip_articles,
    _clean_description,
    _clean_description_inline,
    _first_sentence,
    _is_valid,
    _is_meta_header,
    _WEAK_SUBJECTS,
    _META_LABELS,
    _LAYER5_META_LABELS,
    _COMPLETE_RE,
    _VERB_MAP,
    _BOLD_LABEL,
    _DASH_BULLET,
    _CLASSIFY,
    _SECTION_HEADER,
    _FILE_REF,
    _FUNC_SKIP,
    _should_skip_category,
    extract_event_time,
    _to_epoch,
)
from .fact_schema import ensure_facts_schema
from .fact_search import facts_search, facts_list, facts_stats
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)

get_config: Callable[[], Any] | None = None

try:
    from config import get_config
except ImportError:  # FLAVOR_A: optional dependency guard
    pass  # already declared above


def _llm_iso_to_epoch(iso_str: str | None) -> float | None:
    """Convert an LLM-returned ISO date string to epoch seconds.

    Handles YYYY-MM-DD, YYYY-MM, and YYYY formats. Returns 0.0 for
    unparseable strings, which signals "no event time" to callers.
    """
    if not iso_str or not isinstance(iso_str, str):
        return None
    parts = iso_str.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return _to_epoch(year, month, day)
    except (ValueError, TypeError):
        return None

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
# Layer 1: Section Header + Bold Labels
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Layer 2: Dash Bullets with em-dash separator
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Layer 3: Classification
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Layer 4: Code References
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Layer 5a: Broader Copula
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Layer 5b: Colon Definitions
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Layer 5c: Plain Dash Bullets
# ---------------------------------------------------------------------------


def _layer5c_plain_dash_bullets(text: str, add_fn) -> None:
    """Layer 5c: ``- just a phrase`` lines (no em-dash separator).

    Whole line is the value; subject is first 1-3 words. Confidence 0.5.

    Conservative: rejects subjects whose first word is a known verb
    (prevents ``- Implements the feature`` → subject="Implements").
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
        # Reject subjects whose first word is a known verb — prevents
        # "Implements", "Uses", "Creates" from becoming fact subjects.
        first_subj_word = bullet_subj[0].lower().rstrip(":")
        if first_subj_word in _VERB_MAP:
            continue
        # Reject single-word all-lowercase subjects (generic, not a proper noun)
        if len(bullet_subj) == 1 and not bullet_subj[0][0].isupper():
            continue
        add_fn(subj, "has_description", text_line[:120], 0.5)


# ---------------------------------------------------------------------------
# Layer 5d: Subject-Verb-Object
# ---------------------------------------------------------------------------


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
        if not any(c.isalpha() for c in obj):
            continue
        pred = _VERB_MAP.get(verb, verb)
        if subj.lower().rstrip(":") in _META_LABELS:
            continue
        if len(subj.split()) > 6:
            continue
        if not _is_valid(subj, obj):
            continue
        add_fn(subj, pred, obj[:100], 0.5)


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def _dedup_facts(
    facts: list[tuple[str, str, str, float, str | None, str]],
) -> list[tuple[str, str, str, float, str | None, str]]:
    """Deduplicate by (subject, predicate, object) keeping the
    highest-confidence copy. Normalizes trailing colons and whitespace
    before keying. Preserves event_time from the highest-confidence copy.
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

    deduped: dict[tuple[str, str, str], tuple[str, str, str, float, str | None, str]] = {}
    for item in facts:
        s, p, o = item[0], item[1], item[2]
        c = item[3]
        key = _dedup_key(s, p, o)
        if key not in deduped or c > deduped[key][3]:
            deduped[key] = item
    return list(deduped.values())


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def extract_facts(text: str) -> list[tuple[str, str, str, float, str | None, str]]:
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

    Returns 6-tuples (subject, predicate, object, confidence, event_time,
    event_time_granularity). Regex-based extraction cannot determine
    per-fact event_time, so those fields are always None / "unknown".
    """
    if not text or len(text) < 20:
        return []

    text = _preprocess(text)
    all_facts: list[tuple[str, str, str, float, str | None, str]] = []
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
            all_facts.append((subj, pred, obj, conf, None, "unknown"))

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
    conn: AnyConnection,
    subject: str,
    predicate: str,
    obj: str,
    confidence: float,
    now: float,
    source_memory: str = "",
    context: str = "",
    event_time: "float | None" = None,
    event_time_granularity: "str | None" = None,
    belief_status: str = "active",
    epistemic_source: str = "agent",
    fact_type: str = "observation",
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

    ``belief_status`` and ``epistemic_source`` are set on INSERT and
    preserved on UPDATE — the first-seen source is canonical.

    ``fact_type`` classifies the belief type taxonomy:
    observation | agent_inference | external_stated | hypothesis | derived
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
            "event_time, event_time_granularity, transaction_time, "
            "belief_status, epistemic_source, fact_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                belief_status,
                epistemic_source,
                fact_type,
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id, locked, confidence, source_memory FROM kg_facts "
            "WHERE subject = ? AND predicate = ? AND object = ?",
            (subject.lower(), predicate, obj.lower()),
        ).fetchone()
        if row is None:
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


def index_facts_for_memory(
    conn: AnyConnection, memory_id: str, content: str,
    belief_status: str = "active", epistemic_source: str = "agent",
    fact_type: str = "observation",
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
    now = time.time()

    # P3.3 (2026-06-19): hybrid strategy — regex default, LLM only for
    # high-value memories (pinned OR importance_score >= threshold).
    # This keeps the LLM cost bounded (~minutes vs ~hours) while still
    # giving premium extraction to the most important notes.
    #
    # Env vars:
    #   MEMORY_LLM_FORCE=1               — always use LLM (override hybrid)
    #   MEMORY_LLM_HYBRID_THRESHOLD=0.5  — importance_score cutoff (default 0.5)
    #   MEMORY_LLM_HYBRID=0              — disable hybrid, never use LLM
    # Determine effective fact_type based on caller specification and
    # extraction method.  If the caller explicitly set fact_type, honour
    # it; otherwise infer from the extraction method.
    used_llm = False
    effective_fact_type = fact_type
    use_llm = _should_use_llm_for_memory(conn, memory_id)

    facts: list[tuple[str, str, str, float, str | None, str]] = []
    if use_llm:
        try:
            from llm_extraction import extract_facts_via_llm

            llm_facts = extract_facts_via_llm(content)
            if llm_facts:
                facts = llm_facts
                used_llm = True
        except Exception:
            logger.warning(
                "LLM fact extraction failed for memory %s, falling back to regex",
                memory_id,
            )
            pass

    if not facts:
        facts = extract_facts(content)

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
        from fact.fact_temporal import (
            audit_fact_temporal_event,
            invalidate_stale_facts,
            reconcile_fact_supersession,
        )

    # Capture the set of new (subject, predicate, object) triples for
    # the diff in T5.
    new_fact_keys: set[tuple[str, str, str]] = {
        (s.lower(), p, o.lower()) for s, p, o, *_ in facts
    }

    for item in facts:
        subj = item[0]
        pred = item[1]
        obj = item[2]
        conf = float(item[3])
        per_fact_et = item[4]
        per_fact_etg = item[5]
        # Use per-fact event_time from LLM when available, else fall back
        # to the memory-level regex extract_event_time(content).
        fact_event_time: float | None
        fact_etg: str | None
        if per_fact_et is not None:
            fact_event_time = _llm_iso_to_epoch(per_fact_et)
            fact_etg = per_fact_etg
        else:
            fact_event_time = event_time
            fact_etg = event_time_granularity
        # _upsert_fact expects float | None for event_time; granularity
        # is stored as-is.
        fact_epoch = fact_event_time if fact_event_time != 0.0 else None
        fact_id = _upsert_fact(
            conn,
            subj,
            pred,
            obj,
            conf,
            now,
            memory_id,
            content[:200],
            event_time=fact_epoch,
            event_time_granularity=fact_etg,
            belief_status=belief_status,
            epistemic_source=epistemic_source,
            fact_type=effective_fact_type,
        )
        # T3.4: supersession reconciliation (gated by feature_temporal_kg)
        if fact_id is not None and temporal_kg_enabled:
            try:
                reconcile_fact_supersession(conn, fact_id)
            except Exception:
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
            logger.warning(
                "fact_temporal: invalidation failed for memory %s; continuing",
                memory_id,
            )
    return {"facts": len(facts)}


def _should_use_llm_for_memory(conn: AnyConnection, memory_id: str) -> bool:
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


def lock_fact(conn: AnyConnection, subject: str, predicate: str, obj: str) -> bool:
    """Lock a fact so consolidation / dedup can't merge or remove it.

    Locked facts still appear in search results but are exempt from
    automatic quality filtering. Returns True if a fact was locked.
    """
    cur = conn.execute(
        "UPDATE kg_facts SET locked = 1 WHERE subject = ? AND predicate = ? AND object = ?",
        (subject.lower(), predicate, obj.lower()),
    )
    return (cur.rowcount or 0) > 0


def unlock_fact(
    conn: AnyConnection, subject: str, predicate: str, obj: str
) -> bool:
    """Unlock a previously-locked fact. Returns True if a fact was
    unlocked, False if the fact doesn't exist or wasn't locked.
    """
    cur = conn.execute(
        "UPDATE kg_facts SET locked = 0 WHERE subject = ? AND predicate = ? AND object = ?",
        (subject.lower(), predicate, obj.lower()),
    )
    return (cur.rowcount or 0) > 0


def _extract_facts_via_llm(
    conn: AnyConnection, memory_id: str, content: str
) -> list[tuple[str, str, str, float, str | None, str]]:
    """Run the hybrid LLM-extraction path. Returns the extracted facts."""
    if not _should_use_llm_for_memory(conn, memory_id):
        return []
    try:
        from llm_extraction import extract_facts_via_llm
        llm_facts = extract_facts_via_llm(content)
        if llm_facts:
            return llm_facts  # type: ignore[no-any-return]
    except Exception:
        logger.warning(
            "LLM fact extraction failed for memory %s, falling back to regex",
            memory_id,
        )
    return []


def _process_extracted_facts(
    conn: AnyConnection,
    memory_id: str,
    content: str,
    facts: list[tuple[str, str, str, float, str | None, str]],
) -> dict:
    """Common fact persistence pipeline after extraction."""
    temporal_kg_enabled = True
    if get_config is not None:
        try:
            temporal_kg_enabled = bool(get_config().feature_temporal_kg)
        except Exception:
            logger.warning("feature_temporal_kg read failed; defaulting to ON")
    if os.environ.get("MEMORY_TEMPORAL_KG") == "0":
        temporal_kg_enabled = False

    if temporal_kg_enabled:
        event_time, event_time_granularity = extract_event_time(content)
    else:
        event_time, event_time_granularity = None, None

    if temporal_kg_enabled:
        from fact.fact_temporal import (
            audit_fact_temporal_event,
            invalidate_stale_facts,
            reconcile_fact_supersession,
        )

    new_fact_keys: set[tuple[str, str, str]] = {
        (s.lower(), p, o.lower()) for s, p, o, *_ in facts
    }

    now = time.time()

    for item in facts:
        subj = item[0]
        pred = item[1]
        obj = item[2]
        conf = float(item[3])
        per_fact_et = item[4]
        per_fact_etg = item[5]
        fact_epoch: float | None
        fact_etg: str | None
        if per_fact_et is not None:
            fact_epoch = _llm_iso_to_epoch(per_fact_et)
            fact_etg = per_fact_etg
        else:
            fact_epoch = event_time
            fact_etg = event_time_granularity
        fact_epoch_val = fact_epoch if fact_epoch != 0.0 else None
        fact_id = _upsert_fact(
            conn, subj, pred, obj, conf, now, memory_id, content[:200],
            event_time=fact_epoch_val, event_time_granularity=fact_etg,
        )
        if fact_id is not None and temporal_kg_enabled:
            try:
                reconcile_fact_supersession(conn, fact_id)
            except Exception:
                logger.warning(
                    "fact_temporal: reconciliation failed for fact %d "
                    "(memory %s); continuing", fact_id, memory_id,
                )

    if temporal_kg_enabled and new_fact_keys:
        try:
            invalidated = invalidate_stale_facts(conn, memory_id, new_fact_keys)
            for fid in invalidated:
                row = conn.execute(
                    "SELECT subject, predicate, object FROM kg_facts WHERE id = ?",
                    (fid,),
                ).fetchone()
                if row is not None:
                    try:
                        audit_fact_temporal_event(
                            conn, event="invalidate", fact_id=fid,
                            reason="manual", subject=row[0], predicate=row[1],
                            obj=row[2], memory_id=memory_id,
                        )
                    except Exception:
                        logger.warning(
                            "fact_temporal: audit log failed for fact %d (memory %s)",
                            fid, memory_id,
                        )
        except Exception:
            logger.warning(
                "fact_temporal: invalidation failed for memory %s; continuing",
                memory_id,
            )
    return {"facts": len(facts)}


def index_facts_for_memory_bulk(
    conn: AnyConnection, memory_id: str, content: str
) -> dict:
    """Bulk fact indexing — regex-only by design."""
    if get_config is not None:
        if not get_config().knowledge_graph:
            return {"facts": 0}
        if os.environ.get("MEMORY_KNOWLEDGE_GRAPH") == "0":
            return {"facts": 0}
    if _should_skip_category(memory_id):
        return {"facts": 0}
    if memory_id and "auto_save" in (content[:200] if content else ""):
        return {"facts": 0}
    if content and "*Auto-generated by auto_save.py*" in content[:500]:
        return {"facts": 0}
    facts = extract_facts(content)
    return _process_extracted_facts(conn, memory_id, content, facts)
