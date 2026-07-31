#!/usr/bin/env python3
from __future__ import annotations
"""Contradiction detector for Agentic Memory.

Phase 1 improvements over the original:
- Word-boundary matching for negation pairs (no partial-word matches)
- Sentence-level co-occurrence (pos/neg must appear in the same sentence)
- Subject-overlap gate (notes must share significant vocabulary)
- Confidence scoring: high / medium / low
- Output includes the matching sentence(s) as evidence
- CLI flag --min-confidence filters out low/medium matches

Phase 2 improvements:
- Antonym-aware semantic polarity detection
- Detects contradictions built from antonyms (e.g., fast/slow, enabled/disabled)

The 'supersedes' detection logic is preserved unchanged.
"""

__all__ = [
    "detect_contradictions",
    "detect_contradictions_semantic",
    "detect_contradictions_all",
    "split_segments",
    "split_sentences",
    "significant_words",
    "classify_operation",
]

import sys
import re
import os
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

get_config: Callable[[], Any] | None = None
try:
    from config import get_config as _gc
    get_config = _gc
except Exception:
    logging.getLogger(__name__).warning("Failed to import get_config")

# M4 fix: import the canonical find_project_root. The 5-line copy that
# used to live here checked only (memory, .git, CLAUDE.md) — the
# canonical version in memory_common checks 7 markers (.agents,
# AGENTS.md, package.json, pyproject.toml in addition) which catches
# project roots the older copy missed.
#
# P0-7 fix (2026-06-23): ensure memory_common is importable before
# the try block. The previous design relied on a fallback that
# duplicated find_project_root / safe_close_db / _FallbackConnectionPool,
# which could drift from the real implementations and break mypy. Now
# we always import from memory_common; if the install layout is
# non-standard, the import will fail loudly rather than silently using
# a divergent fallback.
import sys as _sys
from pathlib import Path as _Path

_cd_dir = str(_Path(__file__).resolve().parent.parent)
try:
    from infra.memory_common import find_project_root, safe_close_db, connection_pool
except ImportError:
    if _cd_dir not in _sys.path:
        _sys.path.insert(0, _cd_dir)
    try:
        from infra.memory_common import find_project_root, safe_close_db, connection_pool
    except ImportError as _e:
        raise ImportError(
            "contradiction_detector.py requires memory_common to be importable. "
            "Ensure the agentic-memory install is on PYTHONPATH or run from the "
            "install root (e.g., ~/.config/agentic-memory)."
        ) from _e


logger = logging.getLogger(__name__)

# Negation pairs: (positive phrase, negative phrase).
# Matching requires BOTH phrases to be present in the SAME SENTENCE
# of the two notes under comparison, AND the notes must share
# significant subject vocabulary.
NEGATION_PAIRS = [
    ("is true", "is false"),
    ("works", "does not work"),
    ("supported", "not supported"),
    ("enabled", "disabled"),
    ("available", "unavailable"),
    ("should", "should not"),
    ("must", "must not"),
    ("increase", "decrease"),
    ("success", "failure"),
    ("safe", "unsafe"),
    ("dangerous", "safe"),
]

# Stop words for significant_words() — common English words that
# don't help distinguish one note from another.
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "should",
        "could",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "dare",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "when",
        "where",
        "while",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "from",
        "as",
        "into",
        "about",
        "between",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "up",
        "down",
        "out",
        "off",
        "over",
        "under",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "they",
        "them",
        "their",
        "we",
        "us",
        "our",
        "you",
        "your",
        "i",
        "me",
        "my",
        "he",
        "she",
        "him",
        "her",
        "his",
        "which",
        "what",
        "who",
        "whom",
        "not",
        "no",
        "nor",
        "so",
        "than",
        "too",
        "very",
        "just",
        "s",
        "t",
        "m",
        "d",
        "ll",
        "re",
        "ve",
        "ain",
        "aren",
        "couldn",
        "didn",
        "doesn",
        "hadn",
        "hasn",
        "haven",
        "isn",
        "mightn",
        "mustn",
        "needn",
        "shan",
        "shouldn",
        "wasn",
        "weren",
        "won",
        "wouldn",
        "use",
        "using",
        "used",
        "check",
        "find",
        "note",
        "notes",
        "true",
        "false",
        "yes",
        "no",
    }
)

# Minimum IDF-weighted subject overlap required to consider
# two notes as being "about the same subject".
#
# IDF formula: log((N+1)/(df+1)) + 1
#   - common words (df ≈ N/3): score ≈ 1.3
#   - medium words (df ≈ N/10): score ≈ 2.4
#   - rare words (df = 1): score ≈ log(N+1) + 1 ≈ 4-5
#
# Threshold of 5.0 ≈ 2-3 rare words OR 4+ medium words shared.
# This filters out pairs that only share generic dev vocabulary.
MIN_SUBJECT_OVERLAP = 5.0

# Segment-level overlap: required for the HIGH-confidence path
# (matching pos/neg segments must also be about the same topic).
# Lower than MIN_SUBJECT_OVERLAP because single segments are short
# and naturally have fewer significant words.
MIN_SEGMENT_OVERLAP = 1.5

# Minimum length (chars) of a "significant" word. Filters out
# 1-2 letter abbreviations and noise.
MIN_WORD_LEN = 4

# Hard cap on pairwise comparisons per detection pass. At ~70us per pair
# this bounds a full pass to ~7s even on pathologically dense vocabularies,
# keeping in-process cron/worker runs well under the task timeout.
_MAX_PHRASE_PAIRS = 100_000

# Cache for segment splitting results (LRU-limited to avoid unbounded growth)
_segment_cache: OrderedDict[str, list] = OrderedDict()
_SEGMENT_CACHE_MAX = 1024


def split_segments(text: str) -> list[tuple[str, str]]:
    """Split a markdown document into meaningful segments.

    Memory notes are markdown-heavy, so a pure sentence splitter
    (period/exclamation/question marks) often returns the whole
    document as one "sentence" — which defeats the purpose of
    sentence-level contradiction detection.

    Segments are:
    - Each markdown header (## / ### / ####) and the prose that
      follows it up to the next header or blank line
    - Each list item (lines starting with - or digit+.)
    - Each line inside a fenced code block
    - Each sentence inside prose paragraphs (split on .!? boundaries)

    Returns a list of (segment_text, kind) tuples where kind is one of
    'header', 'list', 'code', 'prose'.
    """
    if not text or not text.strip():
        return []
    import hashlib

    cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    if cache_key in _segment_cache:
        _segment_cache.move_to_end(cache_key)
        return _segment_cache[cache_key]

    segments = []
    lines = text.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Skip blank lines
        if not stripped:
            i += 1
            continue

        # Markdown header
        if re.match(r"^#{1,6}\s+", stripped):
            header_text = re.sub(r"^#+\s+", "", stripped)
            # Collect prose until next header or blank line
            prose_lines = []
            i += 1
            while (
                i < n
                and lines[i].strip()
                and not re.match(r"^#{1,6}\s+", lines[i].strip())
            ):
                prose_lines.append(lines[i].strip())
                i += 1
            full = (
                header_text + " " + " ".join(prose_lines)
                if prose_lines
                else header_text
            )
            segments.append((full.strip(), "header"))
            continue

        # List item
        if re.match(r"^[-*+]\s+|^\d+\.\s+", stripped):
            segments.append((stripped, "list"))
            i += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            code_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1  # skip closing ```
            if code_lines:
                segments.append(("\n".join(code_lines), "code"))
            continue

        # Prose paragraph — collect until blank line or header
        para_lines = []
        while (
            i < n and lines[i].strip() and not re.match(r"^#{1,6}\s+", lines[i].strip())
        ):
            para_lines.append(lines[i].strip())
            i += 1
        paragraph = " ".join(para_lines)
        # Split paragraph into sentences
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", paragraph):
            sentence = sentence.strip()
            if sentence:
                segments.append((sentence, "prose"))

    _segment_cache[cache_key] = segments
    if len(_segment_cache) > _SEGMENT_CACHE_MAX:
        _segment_cache.popitem(last=False)
    return segments


def split_sentences(text: str) -> list[str]:
    """Split text into sentences (simpler fallback for non-markdown content)."""
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def significant_words(text: str) -> set[str]:
    """Extract significant words from text, filtering stop words and short words."""
    words = re.findall(r"\b[\w@#\.\-]+\b", text.lower())
    return {w for w in words if len(w) >= MIN_WORD_LEN and w not in STOP_WORDS}


def find_phrase_in_sentence(sentence: str, phrase: str) -> bool:
    """Return True if *phrase* appears in *sentence* (case-insensitive)."""
    if not sentence or not phrase:
        return False
    return phrase.lower() in sentence.lower()


def subject_overlap(text_a: str, text_b: str) -> tuple[int, set[str]]:
    """Return ``(overlap_score, shared_words)`` for two text blocks.

    ``overlap_score`` is the count of significant words shared between the
    two texts.  ``shared_words`` is the set itself, useful for diagnostics.
    """
    words_a = significant_words(text_a)
    words_b = significant_words(text_b)
    shared = words_a & words_b
    return len(shared), shared


def classify_operation(
    new_content: str, existing_content: str | None
) -> tuple[str, str]:
    """Classify the operation as ADD/UPDATE/DELETE/NOOP."""
    if not existing_content:
        return "ADD", "No existing memory found"

    if new_content.strip() == existing_content.strip():
        return "NOOP", "Content is identical"

    # Check if new content is a deletion marker
    if new_content.strip().startswith("[DELETED]") or new_content.strip() == "":
        return "DELETE", "Content marked as deleted"

    # Check for contradiction indicators
    contradiction_signals = [
        ("contradicts", "Explicit contradiction marker"),
        ("supersedes", "Supersedes existing memory"),
        ("incorrect", "Corrects existing information"),
        ("wrong", "Corrects existing information"),
        ("actually", "Corrects existing information"),
        ("instead of", "Replaces existing information"),
        ("no longer", "Invalidates existing information"),
        ("deprecated", "Deprecates existing information"),
    ]

    new_lower = new_content.lower()
    for signal, reason in contradiction_signals:
        if signal in new_lower:
            return "UPDATE", f"Contradiction signal: {reason}"

    # Check if content significantly overlaps
    new_words = set(new_content.lower().split())
    old_words = set(existing_content.lower().split())
    if len(new_words) > 0 and len(old_words) > 0:
        overlap = len(new_words & old_words) / max(len(new_words), len(old_words))
        if overlap > 0.8:
            return "UPDATE", f"High overlap ({overlap:.0%}), likely update"
        elif overlap > 0.5:
            return "UPDATE", f"Moderate overlap ({overlap:.0%}), possible update"

    return "ADD", "New distinct information"


# ---------------------------------------------------------------------------
# Phase 1: Phrase-based contradiction detector (high-precision)
# ---------------------------------------------------------------------------


def _find_sentence_containing(text: str, phrase: str) -> str | None:
    sentences = split_sentences(text)
    for sent in sentences:
        if phrase in sent.lower():
            return sent.strip()
    return None


def detect_contradictions(memory_dir, min_confidence="low", tenant_id=None):
    """Scan memory for contradictions using phrase-based detection.

    This is the high-precision detector that looks for explicit negation
    pairs (e.g., "is true" / "is false") in the same sentence.

    Args:
        memory_dir: path to memory directory
        min_confidence: 'low' | 'medium' | 'high'
        tenant_id: when set, restrict detection to a single tenant. In a
            multi-agent DB where several agents share one physical
            ``memory.db`` (scoped via the ``tenant_memories`` view), omitting
            this would compare notes across tenants and produce cross-tenant
            false positives. The resolver reads ``tenant_memories``, so the
            detector must be scoped identically.

    Returns:
        list of contradiction dicts with keys:
        source, target, type, confidence, evidence_a, evidence_b
    """
    memory_dir = Path(memory_dir)
    db_path = memory_dir / "memory.db"

    if not db_path.exists():
        logger.error("Database not found at %s", db_path)
        return []

    _tenant_clause = "AND tenant_id = ?" if tenant_id else ""
    _tenant_params = (tenant_id,) if tenant_id else ()
    db = connection_pool.get(str(db_path))
    try:
        db.execute("PRAGMA busy_timeout = 30000;")
        rows = db.execute(
            "SELECT id, content, source_file FROM memories "
            "WHERE (valid_to IS NULL OR valid_to = '') "
            "AND (deleted_at IS NULL OR deleted_at = '') "
            "AND content IS NOT NULL "
            f"{_tenant_clause}",
            _tenant_params,
        ).fetchall()
    finally:
        safe_close_db(db)

    if not rows:
        return []

    # Notes are bucketed by significant word so pairs are only compared
    # when they share at least one significant word — O(N * avg_bucket_size)
    # instead of O(N^2). significant_words() is computed once per note
    # (not once per pair), which was the dominant cost on large corpora:
    # 1406 notes previously meant ~1M redundant regex extractions and a
    # 72s+ runtime per detection pass (2026-07-31 runaway incident).
    contradictions = []
    seen_pairs = set()

    note_words: list[set[str]] = []
    note_neg: list[tuple[set[str], set[str]]] = []
    word_index: dict[str, list[int]] = {}
    for k, (_, content, _src) in enumerate(rows):
        words = significant_words(content)
        note_words.append(words)
        for w in words:
            word_index.setdefault(w, []).append(k)
        # Negation-token presence (substring test — the word-boundary regex
        # below remains authoritative, this gate only avoids running it for
        # pairs that cannot match). On a 1406-note corpus only 6% of notes
        # carry any negation token, cutting regex scans from ~1M to ~3K.
        low = content.lower()
        pos_forms = {p for p, _ in NEGATION_PAIRS if p in low}
        neg_forms = {n for _, n in NEGATION_PAIRS if n in low}
        note_neg.append((pos_forms, neg_forms))

    # Safety valve: hard cap on negation-regex pair scans so a pathological
    # corpus can never hang an in-process cron/worker run.
    pairs_checked = 0
    capped = False
    for i, (nid1, content1, source1) in enumerate(rows):
        words1 = note_words[i]
        if not words1:
            continue
        checked: set[int] = set()
        for w in words1:
            for j in word_index.get(w, ()):
                if j <= i or j in checked:
                    continue
                checked.add(j)
                nid2, content2, source2 = rows[j]
                words2 = note_words[j]
                pair_key = tuple(sorted([nid1, nid2]))
                if pair_key in seen_pairs:
                    continue

                # Subject-overlap gate: notes must share significant vocabulary
                if not (words1 & words2):
                    continue

                # Negation-presence gate: skip pairs whose token sets cannot
                # produce a pos/neg cross-match (the per-pair regex scan is
                # the expensive part, so only run it on qualifying pairs).
                pos1, neg1 = note_neg[i]
                pos2, neg2 = note_neg[j]
                if not ((pos1 and neg2) or (neg1 and pos2)):
                    continue

                pairs_checked += 1
                if pairs_checked > _MAX_PHRASE_PAIRS:
                    capped = True
                    logger.warning(
                        "detect_contradictions: hit pair cap %d over %d notes; "
                        "returning partial results",
                        _MAX_PHRASE_PAIRS,
                        len(rows),
                    )
                    break

                # Check negation pairs with word-boundary matching
                c1_lower = content1.lower()
                c2_lower = content2.lower()

                for pos, neg in NEGATION_PAIRS:
                    # Word-boundary matching to avoid partial-word matches
                    pos_pattern = r"\b" + re.escape(pos) + r"\b"
                    neg_pattern = r"\b" + re.escape(neg) + r"\b"

                    c1_has_pos = bool(re.search(pos_pattern, c1_lower))
                    c1_has_neg = bool(re.search(neg_pattern, c1_lower))
                    c2_has_pos = bool(re.search(pos_pattern, c2_lower))
                    c2_has_neg = bool(re.search(neg_pattern, c2_lower))

                    # Both phrases must be present across the two notes
                    # AND they must appear in the same sentence
                    if (c1_has_pos and c2_has_neg) or (c1_has_neg and c2_has_pos):
                        # Find the sentences containing the phrases
                        if c1_has_pos:
                            sent1 = _find_sentence_containing(content1, pos)
                            sent2 = _find_sentence_containing(content2, neg)
                        else:
                            sent1 = _find_sentence_containing(content1, neg)
                            sent2 = _find_sentence_containing(content2, pos)

                        if sent1 and sent2:
                            seen_pairs.add(pair_key)
                            confidence = "medium"  # phrase-based is medium confidence
                            if len(words1 & words2) >= 5:
                                confidence = "high"
                            contradictions.append(
                                {
                                    "source": nid1,
                                    "target": nid2,
                                    "type": "phrase_negation",
                                    "confidence": confidence,
                                    "evidence_a": sent1[:200],
                                    "evidence_b": sent2[:200],
                                    "source_file": source1,
                                }
                            )
                            break  # one contradiction per pair is enough
            if capped:
                break
        if capped:
            break

    # Filter by min_confidence
    rank = {"low": 0, "medium": 1, "high": 2}
    return [
        c
        for c in contradictions
        if rank[c.get("confidence", "low")] >= rank[min_confidence]
    ]


# ---------------------------------------------------------------------------
# Phase 2: Antonym-aware semantic polarity detection
# ---------------------------------------------------------------------------

# Technical antonym pairs for detecting semantic polarity flips.
# Each pair represents words with opposite meanings in technical contexts.
# Format: {word: {antonym1, antonym2, ...}} - multi-valued so all pairs
# survive Python dict literal evaluation (unlike a flat dict where
# duplicate keys silently lose earlier entries).
TECHNICAL_ANTONYMS: dict[str, set[str]] = {
    # Performance
    "fast": {"slow", "sluggish"},
    "slow": {"fast"},
    "sluggish": {"fast", "blazing"},
    "blazing": {"sluggish"},
    "rapid": {"gradual"},
    "gradual": {"rapid"},
    "quick": {"delayed"},
    "delayed": {"quick"},
    "snappy": {"laggy"},
    "laggy": {"snappy"},
    # Size/Scale
    "high": {"low"},
    "low": {"high"},
    "large": {"small"},
    "small": {"large"},
    "big": {"tiny"},
    "tiny": {"big"},
    "massive": {"minimal"},
    "minimal": {"massive"},
    "huge": {"negligible"},
    "negligible": {"huge"},
    # State
    "active": {"inactive"},
    "inactive": {"active"},
    "enabled": {"disabled"},
    "disabled": {"enabled"},
    "online": {"offline"},
    "offline": {"online"},
    "running": {"stopped"},
    "stopped": {"running"},
    # Quality
    "good": {"bad"},
    "bad": {"good"},
    "excellent": {"poor"},
    "poor": {"excellent"},
    "great": {"terrible"},
    "terrible": {"great"},
    "best": {"worst"},
    "worst": {"best"},
    "optimal": {"suboptimal"},
    "suboptimal": {"optimal"},
    "ideal": {"nonideal"},
    "nonideal": {"ideal"},
    # Quantity
    "more": {"less"},
    "less": {"more"},
    "increase": {"decrease"},
    "decrease": {"increase"},
    "maximum": {"minimum"},
    "minimum": {"maximum"},
    "max": {"min"},
    "min": {"max"},
    "majority": {"minority"},
    "minority": {"majority"},
    # Timing
    "early": {"late"},
    "late": {"early"},
    "before": {"after"},
    "after": {"before"},
    "start": {"end"},
    "end": {"start"},
    "begin": {"finish"},
    "finish": {"begin"},
    # Logic
    "true": {"false"},
    "false": {"true"},
    "yes": {"no"},
    "no": {"yes"},
    "always": {"never"},
    "never": {"always"},
    "all": {"none"},
    "none": {"all"},
    # Access
    "allow": {"block"},
    "block": {"allow"},
    "permit": {"deny"},
    "deny": {"permit"},
    "grant": {"revoke"},
    "revoke": {"grant"},
    "open": {"closed"},
    "closed": {"open"},
    "public": {"private"},
    "private": {"public"},
    # Direction
    "upstream": {"downstream"},
    "downstream": {"upstream"},
    "forward": {"backward"},
    "backward": {"forward"},
    "top": {"bottom"},
    "bottom": {"top"},
    # Connection
    "connect": {"disconnect"},
    "disconnect": {"connect"},
    "attach": {"detach"},
    "detach": {"attach"},
    "link": {"unlink"},
    "unlink": {"link"},
    "pair": {"unpair"},
    "unpair": {"pair"},
    # Creation
    "create": {"destroy"},
    "destroy": {"create"},
    "build": {"tear"},
    "tear": {"build"},
    "add": {"remove"},
    "remove": {"add"},
    "insert": {"delete"},
    "delete": {"insert"},
    "install": {"uninstall"},
    "uninstall": {"install"},
    # Status
    "success": {"failure"},
    "failure": {"success"},
    "pass": {"fail"},
    "fail": {"pass"},
    "valid": {"invalid"},
    "invalid": {"valid"},
    "correct": {"incorrect"},
    "incorrect": {"correct"},
    "right": {"wrong"},
    "wrong": {"right"},
    # Mode
    "serial": {"parallel"},
    "parallel": {"serial"},
    "synchronous": {"asynchronous"},
    "asynchronous": {"synchronous"},
    "sync": {"async"},
    "async": {"sync"},
    "blocking": {"nonblocking"},
    "nonblocking": {"blocking"},
    # Memory/Storage
    "alloc": {"free"},
    "free": {"alloc"},
    "retain": {"release"},
    "release": {"retain"},
    "lock": {"unlock"},
    "unlock": {"lock"},
    # Data
    "encode": {"decode"},
    "decode": {"encode"},
    "encrypt": {"decrypt"},
    "decrypt": {"encrypt"},
    "compress": {"decompress"},
    "decompress": {"compress"},
    "serialize": {"deserialize"},
    "deserialize": {"serialize"},
    # Network
    "client": {"server"},
    "server": {"client"},
    "request": {"response"},
    "response": {"request"},
    "send": {"receive"},
    "receive": {"send"},
    "upload": {"download"},
    "download": {"upload"},
    "push": {"pull"},
    "pull": {"push"},
    # UI
    "show": {"hide"},
    "hide": {"show"},
    "visible": {"hidden"},
    "hidden": {"visible"},
    "expand": {"collapse"},
    "collapse": {"expand"},
    "maximize": {"minimize"},
    "minimize": {"maximize"},
    "focus": {"blur"},
    "blur": {"focus"},
    # Process
    "stop": {"start"},
    "resume": {"pause"},
    "pause": {"resume"},
    "run": {"halt"},
    "halt": {"run"},
    # Auth
    "login": {"logout"},
    "logout": {"login"},
    "signin": {"signout"},
    "signout": {"signin"},
    # Misc
    "include": {"exclude"},
    "exclude": {"include"},
    "accept": {"reject"},
    "reject": {"accept"},
    "approve": {"deny"},
    "enable": {"disable"},
    "disable": {"enable"},
    "support": {"oppose"},
    "oppose": {"support"},
    "pro": {"con"},
    "con": {"pro"},
}


def _get_antonym(word: str) -> set[str] | None:
    return TECHNICAL_ANTONYMS.get(word.lower())


def _check_antonym_polarity(
    claim_a: str, claim_b: str
) -> tuple[bool, list[tuple[str, str, str]]]:
    """Check if two claims contain matching antonyms that indicate polarity flip.

    Returns:
        (is_contradiction, list_of_evidence) where evidence is
        [(word_a, antonym, word_b), ...] for matching antonym pairs found.
        Position 0 is the word from claim_a, position 2 is the word from
        claim_b that is the antonym of position 0.
    """
    words_a = set(re.findall(r"\b[\w']+\b", claim_a.lower()))
    words_b = set(re.findall(r"\b[\w']+\b", claim_b.lower()))

    evidence = []

    # Check each word in claim_a; if any of its antonyms appears in claim_b,
    # record the pair (word_a, antonym, matched_word_b).
    for word_a in words_a:
        antonyms = _get_antonym(word_a)
        if antonyms:
            for antonym in antonyms:
                if antonym in words_b:
                    evidence.append((word_a, antonym, antonym))

    # Also check each word in claim_b; if any of its antonyms appears in
    # claim_a, record the pair (matched_word_a, antonym, word_b).
    for word_b in words_b:
        antonyms = _get_antonym(word_b)
        if antonyms:
            for antonym in antonyms:
                if antonym in words_a:
                    # Avoid duplicates: skip if (word_b's direction) covered
                    if not any(e[0] == antonym and e[2] == word_b for e in evidence):
                        evidence.append((antonym, antonym, word_b))

    # Deduplicate evidence by the (word_a, word_b) pair
    seen = set()
    unique_evidence = []
    for e in evidence:
        key = tuple(sorted([e[0], e[2]]))
        if key not in seen:
            seen.add(key)
            unique_evidence.append(e)

    return len(unique_evidence) > 0, unique_evidence


# ---------------------------------------------------------------------------
# Semantic contradiction detector (embedding-based)
# ---------------------------------------------------------------------------

SEMANTIC_THRESHOLD = get_config().semantic_threshold if get_config is not None else 0.65
SEMANTIC_HIGH_THRESHOLD = 0.78  # very high cosine => "near-paraphrase"
SLIDING_WINDOW_SIZE = 500

NEGATION_CUES = frozenset(
    {
        "not",
        "no",
        "never",
        "neither",
        "none",
        "cannot",
        "n't",
        "false",
        "wrong",
        "incorrect",
        "deprecated",
        "removed",
        "disabled",
        "unavailable",
        "broken",
        "fails",
        "doesn't",
        "don't",
        "didn't",
        "won't",
        "isn't",
        "aren't",
        "wasn't",
        "weren't",
        "hasn't",
        "haven't",
        "hadn't",
        "wouldn't",
        "shouldn't",
        "couldn't",
        "mustn't",
        "lack",
        "lacks",
        "lacking",
        "without",
        "absent",
    }
)

AFFIRMATION_CUES = frozenset(
    {
        "true",
        "correct",
        "enabled",
        "available",
        "works",
        "supported",
        "always",
        "every",
        "all",
        "must",
        "should",
        "do",
        "does",
        "yes",
        "ok",
        "okay",
        "fine",
        "valid",
    }
)


def _extract_claims(text, min_words=5, max_words=50):
    if not text:
        return []
    # Strip frontmatter
    text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)
    # Strip code fences
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    # Strip inline code
    text = re.sub(r"`[^`]+`", "", text)
    # Split on sentence boundaries
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])|\n\n+", text)
    claims = []
    for p in parts:
        p = p.strip()
        p = re.sub(r"^[-*+]\s+", "", p)
        p = re.sub(r"^\d+\.\s+", "", p)
        p = re.sub(r"^#+\s*", "", p)
        if not p or len(p) < 10:
            continue
        words = re.findall(r"\b[\w@#\.\-]+\b", p)
        if not (min_words <= len(words) <= max_words):
            continue
        claims.append(p)
    return claims


def _claim_polarity(claim: str) -> tuple[int, int]:
    """Return (negation_count, affirmation_count) for the claim.

    Includes sarcasm/irony detection: if positive qualifiers (e.g. "blazing fast",
    "so stable") are coupled with negative outcomes (e.g. "crashed", "failed", "broke")
    in the same claim, we invert the polarity by incrementing negation count.
    """
    words = set(re.findall(r"\b[\w']+\b", claim.lower()))
    neg = len(NEGATION_CUES & words)
    aff = len(AFFIRMATION_CUES & words)

    lower_claim = claim.lower()
    positive_indicators = [
        "blazing fast",
        "lightning fast",
        "incredibly fast",
        "so stable",
        "super stable",
        "highly stable",
        "perfectly stable",
        "works perfectly",
        "works fine",
        "works great",
        "perfectly fine",
        "amazing",
        "wonderful",
    ]
    negative_indicators = [
        "crashed",
        "crashes",
        "hung",
        "hangs",
        "failed",
        "fails",
        "broke",
        "breaks",
        "timed out",
        "timeout",
        "garbage",
        "useless",
        "terrible",
        "took 10 seconds",
        "took forever",
        "took a long time",
        "slow as",
    ]

    has_pos = any(pos in lower_claim for pos in positive_indicators)
    has_neg = any(neg_ind in lower_claim for neg_ind in negative_indicators)

    if has_pos and has_neg:
        neg += 1

    return neg, aff


def _cosine_sim(a, b) -> float:
    import numpy as np

    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def detect_contradictions_semantic(memory_dir, threshold=SEMANTIC_THRESHOLD, tenant_id=None):
    """Scan memory for paraphrased contradictions using model2vec.

    Extracts atomic claims, embeds each with model2vec, and reports
    claim pairs with cosine > threshold whose polarity differs.

    Now includes antonym-aware detection: if two claims contain matching
    antonyms and have high semantic similarity, they are flagged as
    contradictions even without explicit negation cues.

    Args:
        memory_dir: path to memory directory containing memory.db
        threshold: cosine threshold for "same topic" (default 0.65)

    Returns:
        list of contradiction dicts with keys:
        source, target, type, confidence, evidence_a, evidence_b, cosine
    """
    try:
        import numpy as np
    except ImportError:
        logger.error("numpy required for semantic detection")
        return []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from infra.embedding_search import get_embedding_search
    except ImportError as e:
        logger.error("embedding_search.py not found on path: %s", e)
        return []
    try:
        search = get_embedding_search()
        if not search.wait_for_model(timeout_s=60.0):
            logger.error("Embedding model not loaded (model2vec may not be installed)")
            return []
        model = search.model
    except Exception as e:
        logger.error("Failed to load embedding model: %s", e)
        return []

    memory_dir = Path(memory_dir)
    db_path = memory_dir / "memory.db"
    if not db_path.exists():
        logger.error("Database not found at %s", db_path)
        return []

    db = connection_pool.get(str(db_path))
    try:
        db.execute("PRAGMA busy_timeout = 30000;")
        _tenant_clause = "AND tenant_id = ?" if tenant_id else ""
        _tenant_params = (tenant_id,) if tenant_id else ()
        rows = db.execute(
            "SELECT id, content FROM memories "
            "WHERE (valid_to IS NULL OR valid_to = '') "
            "AND (deleted_at IS NULL OR deleted_at = '') "
            "AND content IS NOT NULL "
            f"{_tenant_clause}",
            _tenant_params,
        ).fetchall()
    finally:
        safe_close_db(db)

    if not rows:
        return []

    # Extract claims and track which note they came from
    note_claims = []
    claim_texts = []
    for nid, content in rows:
        claims = _extract_claims(content)
        for i, c in enumerate(claims):
            note_claims.append((nid, c, i))
            claim_texts.append(c)

    if not claim_texts:
        return []

    _MAX_CLAIMS_SEMANTIC = (
        get_config().max_claims_semantic if get_config is not None else 10000
    )
    # Hard ceiling: embedding + per-claim pair checks must stay well under
    # the 300s worker task timeout. The 10000 config ceiling produced
    # ~10K claims on a ~1400-note corpus and multi-minute runs that
    # repeatedly hit the watchdog (2026-07-31 runaway incident).
    _MAX_CLAIMS_SEMANTIC = min(_MAX_CLAIMS_SEMANTIC, 3000)
    if len(claim_texts) > _MAX_CLAIMS_SEMANTIC:
        logger.warning(
            "Too many claims for semantic detection (%d), capping at %d",
            len(claim_texts),
            _MAX_CLAIMS_SEMANTIC,
        )
        claim_texts = claim_texts[:_MAX_CLAIMS_SEMANTIC]
        note_claims = note_claims[:_MAX_CLAIMS_SEMANTIC]

    try:
        embeddings = model.encode(claim_texts, show_progress_bar=False)
    except Exception as e:
        logger.error("Embedding failed: %s", e)
        return []
    embeddings = np.asarray(embeddings)

    contradictions = []
    n = len(claim_texts)
    seen_pairs = set()

    # Try using usearch ANN index for claim pair contradiction detection
    usearch_success = False
    try:
        from usearch.index import Index
        dim = embeddings.shape[1]
        index = Index(ndim=dim, metric="cos")
        index.add(np.arange(n), embeddings)

        for i in range(n):
            nid_a, claim_a, _ = note_claims[i]
            res = index.search(embeddings[i], 50)
            for j, dist in zip(res.keys, res.distances):
                j = int(j)
                if i == j:
                    continue
                sim = 1.0 - float(dist)
                if sim < threshold:
                    continue

                nid_b, claim_b, _ = note_claims[j]
                if nid_a == nid_b:
                    continue
                pair_key = tuple(sorted([nid_a, nid_b]))
                if pair_key in seen_pairs:
                    continue

                # Check 1: Negation polarity flip
                neg_a, aff_a = _claim_polarity(claim_a)
                neg_b, aff_b = _claim_polarity(claim_b)
                polarity_flipped = (neg_a > 0 and neg_b == 0) or (neg_b > 0 and neg_a == 0)

                # Check 2: Antonym polarity flip
                has_antonym, antonym_evidence = _check_antonym_polarity(claim_a, claim_b)

                if not polarity_flipped and not has_antonym:
                    continue

                seen_pairs.add(pair_key)
                confidence = "high" if sim >= SEMANTIC_HIGH_THRESHOLD else "medium"

                if has_antonym:
                    antonym_str = ", ".join(f"{e[0]}<->{e[1]}" for e in antonym_evidence)
                    contradictions.append(
                        {
                            "source": nid_a,
                            "target": nid_b,
                            "type": "semantic_antonym",
                            "confidence": confidence,
                            "evidence_a": claim_a[:200],
                            "evidence_b": claim_b[:200],
                            "cosine": round(sim, 3),
                            "polarity": f"antonyms=[{antonym_str}] a_neg={neg_a} a_aff={aff_a} | b_neg={neg_b} b_aff={aff_b}",
                        }
                    )
                else:
                    contradictions.append(
                        {
                            "source": nid_a,
                            "target": nid_b,
                            "type": "semantic_negation",
                            "confidence": confidence,
                            "evidence_a": claim_a[:200],
                            "evidence_b": claim_b[:200],
                            "cosine": round(sim, 3),
                            "polarity": f"a_neg={neg_a} a_aff={aff_a} | b_neg={neg_b} b_aff={aff_b}",
                        }
                    )
        usearch_success = True
    except Exception as e:
        logger.warning("usearch ANN index failed or not available, falling back to numpy sliding window: %s", e)

    if not usearch_success:
        # Sliding window for large corpora
        if n > SLIDING_WINDOW_SIZE:
            mat = embeddings - embeddings.mean(axis=0)
            _, _, Vt = np.linalg.svd(mat, full_matrices=False)
            pc1_dir = Vt[0]
            sort_key = embeddings @ pc1_dir
        else:
            sort_key = np.arange(n, dtype=np.float64)
        sorted_idx = np.argsort(sort_key).tolist()
        window = min(SLIDING_WINDOW_SIZE, n)

        # B16 fix: vectorize pairwise cosine sim.  Normalize rows once,
        # then within each sliding window compute the full matrix product.
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normed = embeddings / norms

        for i_pos in range(n):
            end = min(i_pos + window, n)
            if end <= i_pos + 1:
                continue
            # Vectorized cosine within the window
            i = sorted_idx[i_pos]
            js = sorted_idx[i_pos + 1 : end]
            sims = normed[js] @ normed[i]  # (window-1,) vector
            nid_a, claim_a, _ = note_claims[i]
            for j_local, j in enumerate(js):
                sim = float(sims[j_local])
                if sim < threshold:
                    continue
                nid_b, claim_b, _ = note_claims[j]
                if nid_a == nid_b:
                    continue
                pair_key = tuple(sorted([nid_a, nid_b]))
                if pair_key in seen_pairs:
                    continue

                # Check 1: Negation polarity flip
                neg_a, aff_a = _claim_polarity(claim_a)
                neg_b, aff_b = _claim_polarity(claim_b)
                polarity_flipped = (neg_a > 0 and neg_b == 0) or (neg_b > 0 and neg_a == 0)

                # Check 2: Antonym polarity flip
                has_antonym, antonym_evidence = _check_antonym_polarity(claim_a, claim_b)

                if not polarity_flipped and not has_antonym:
                    continue

                seen_pairs.add(pair_key)
                confidence = "high" if sim >= SEMANTIC_HIGH_THRESHOLD else "medium"
                neg_a, aff_a = _claim_polarity(claim_a)
                neg_b, aff_b = _claim_polarity(claim_b)

                if has_antonym:
                    antonym_str = ", ".join(f"{e[0]}<->{e[1]}" for e in antonym_evidence)
                    contradictions.append(
                        {
                            "source": nid_a,
                            "target": nid_b,
                            "type": "semantic_antonym",
                            "confidence": confidence,
                            "evidence_a": claim_a[:200],
                            "evidence_b": claim_b[:200],
                            "cosine": round(sim, 3),
                            "polarity": f"antonyms=[{antonym_str}] a_neg={neg_a} a_aff={aff_a} | b_neg={neg_b} b_aff={aff_b}",
                        }
                    )
                else:
                    contradictions.append(
                        {
                            "source": nid_a,
                            "target": nid_b,
                            "type": "semantic_negation",
                            "confidence": confidence,
                            "evidence_a": claim_a[:200],
                            "evidence_b": claim_b[:200],
                            "cosine": round(sim, 3),
                            "polarity": f"a_neg={neg_a} a_aff={aff_a} | b_neg={neg_b} b_aff={aff_b}",
                        }
                    )

    return contradictions


def detect_contradictions_all(
    memory_dir,
    min_confidence="low",
    mode="both",
    semantic_threshold=SEMANTIC_THRESHOLD,
    tenant_id=None,
):
    """Run contradiction detection across one or both detectors.

    Args:
        memory_dir: path to memory directory
        min_confidence: 'low' | 'medium' | 'high'
        mode: 'phrase' (existing detector only), 'semantic' (new only),
              or 'both' (default — merge and dedupe)
        semantic_threshold: cosine threshold for semantic detector

    Returns:
        list of contradiction dicts, with each entry tagged with
        'detector' (phrase | semantic) for traceability.
    """
    results = []
    if mode in ("phrase", "both"):
        for c in detect_contradictions(memory_dir, min_confidence, tenant_id=tenant_id):
            c = dict(c)
            c["detector"] = "phrase"
            results.append(c)
    if mode in ("semantic", "both"):
        for c in detect_contradictions_semantic(memory_dir, semantic_threshold, tenant_id=tenant_id):
            c = dict(c)
            rank = {"low": 0, "medium": 1, "high": 2}
            if rank[c.get("confidence", "low")] >= rank[min_confidence]:
                results.append(c)
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: contradiction_detector.py <memory_dir> [new_content_file] [--min-confidence=low|medium|high]"
        )
        print("  No second arg: scan for contradictions")
        print("  With second arg: classify write operation")
        print("  --min-confidence: filter results by minimum confidence (default: low)")
        sys.exit(1)

    min_confidence = "low"
    mode = "both"
    semantic_threshold = SEMANTIC_THRESHOLD
    tenant_id = os.environ.get("MEMORY_CRON_TENANT_ID")
    positional = []
    for arg in sys.argv[1:]:
        if arg.startswith("--min-confidence="):
            min_confidence = arg.split("=", 1)[1]
        elif arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
        elif arg.startswith("--threshold="):
            semantic_threshold = float(arg.split("=", 1)[1])
        elif arg.startswith("--tenant="):
            tenant_id = arg.split("=", 1)[1]
        else:
            positional.append(arg)

    memory_dir = positional[0]

    if len(positional) > 2:
        new_content = Path(positional[1]).read_text()
        root = find_project_root(Path.cwd())
        if root is None:
            logger.error(
                "No project root found (no memory/, .git/, or CLAUDE.md marker).",
            )
            sys.exit(1)
        db_path = root / "memory" / "memory.db"
        db = connection_pool.get(str(db_path))
        try:
            existing = db.execute("SELECT content FROM memories LIMIT 1").fetchone()
            existing_content = existing[0] if existing else ""
        finally:
            safe_close_db(db)
        operation, reason = classify_operation(new_content, existing_content)
        logger.info("Operation: %s", operation)
        logger.info("Reason: %s", reason)
    else:
        contradictions = detect_contradictions_all(
            memory_dir,
            min_confidence=min_confidence,
            mode=mode,
            semantic_threshold=semantic_threshold,
            tenant_id=tenant_id,
        )
        if contradictions:
            logger.info("Found %d contradictions:", len(contradictions))
            for c in contradictions:
                detector = c.get("detector", "unknown")
                logger.info("  [%s] %s -> %s (%s)", detector, c['source'], c['target'], c['type'])
                if "evidence_a" in c:
                    logger.info("    A: %s", c['evidence_a'][:100])
                if "evidence_b" in c:
                    logger.info("    B: %s", c['evidence_b'][:100])
        else:
            logger.info("No contradictions found.")
