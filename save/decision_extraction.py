"""Sprint 4: Heuristic-only decision extraction from saved memory notes.

No LLM calls — pattern matching only.  LLM enrichment deferred to
Sprint 6 behind ``MEMORY_SESSION_DECISION_LLM=1``.

Patterns detected:
  * First-person decision verbs:  ``I/we/the team decided/chose/picked/went with``
  * ADR markers:                  ``# ADR``, ``## Decision``
  * RFC markers:                  ``## RFC``, ``# RFC``
  * Tradeoff table headers:       ``| Option |``
  * Resolution markers:           ``**Decision:**``, ``→ chosen``, ``→ selected``
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DecisionCandidate:
    """One candidate decision/thread extracted from a memory note."""

    title: str
    claim: str
    event_type: str = "decision"  # claim | evidence | decision | question | pivot
    confidence: float = 0.5
    thread_slug: str = ""  # stable slug used to match threads across sessions
    alternatives: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_FIRST_PERSON_DECISION_RE = re.compile(
    r"(?:I|we|the team)\s+(?:decided|chose|picked|went with|opted for|settled on)\b",
    re.IGNORECASE,
)

_ADR_MARKER_RE = re.compile(r"^#+\s*ADR", re.IGNORECASE | re.MULTILINE)
_DECISION_HEADING_RE = re.compile(r"^##+\s*Decision\b", re.IGNORECASE | re.MULTILINE)
_RFC_HEADING_RE = re.compile(r"^##+\s*RFC\b", re.IGNORECASE | re.MULTILINE)

_TRADEOFF_TABLE_RE = re.compile(r"^\|\s*Option\s*\|", re.IGNORECASE | re.MULTILINE)
_RESOLUTION_MARKER_RE = re.compile(
    r"\*\*Decision:\*\*|→\s*(?:chosen|selected|picked|decided)\b",
    re.IGNORECASE,
)

# Stable slug from title: lowercase, spaces→-, non-alphanum removed
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:60]


def _first_sentence(text: str, max_len: int = 200) -> str:
    """Return the first sentence of *text*, truncated to max_len."""
    for sep in (". ", ".\n", "! ", "? ", "\n\n"):
        idx = text.find(sep)
        if idx != -1:
            return text[: idx + 1].strip()[:max_len]
    return text.strip()[:max_len]


def _extract_alternatives(text: str) -> List[str]:
    """Pull out alternativess from a tradeoff-style list."""
    alts: List[str] = []
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s*(.+)", line)
        if m:
            candidate = m.group(1).strip()
            if candidate and len(candidate) < 100:
                alts.append(candidate)
    return alts[:5]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

DECISION_CATEGORIES = {"decisions", "lessons", "projects", "architecture"}


def _extract_decision_candidates(
    content: str,
    category: str,
) -> List[DecisionCandidate]:
    """Return heuristic decision candidates from *content*.

    Only runs for categories in DECISION_CATEGORIES.  Each hit produces
    one DecisionCandidate.  Multiple patterns on the same text produce
    candidates with the same thread_slug so they merge downstream.
    """
    if category not in DECISION_CATEGORIES:
        return []

    if not content or not content.strip():
        return []

    candidates: List[DecisionCandidate] = []

    def _add(
        title: str,
        claim: str,
        etype: str,
        conf: float,
        alternatives: Optional[List[str]] = None,
    ) -> None:
        slug = _slugify(title) if title else _slugify(claim[:40])
        candidates.append(
            DecisionCandidate(
                title=title.strip()[:120] if title else claim.strip()[:120],
                claim=claim.strip()[:500],
                event_type=etype,
                confidence=conf,
                thread_slug=slug,
                alternatives=alternatives or [],
            )
        )

    # Pattern 1: first-person decision verb
    for m in _FIRST_PERSON_DECISION_RE.finditer(content):
        start = m.start()
        line_start = content.rfind("\n", 0, start) + 1
        sentence = content[line_start : line_start + 400]
        title = _first_sentence(sentence, 120)
        _add(title, sentence, "decision", 0.85)

    # Pattern 2: ADR marker
    if _ADR_MARKER_RE.search(content):
        lines = content.splitlines()
        adr_title = "ADR"
        for line in lines:
            if _ADR_MARKER_RE.match(line):
                adr_title = line.strip("# ").strip()[:120]
                break
        claim = _first_sentence(content, 500)
        alts = _extract_alternatives(content)
        _add(adr_title, claim, "decision", 0.9, alts)

    # Pattern 3: RFC heading
    if _RFC_HEADING_RE.search(content):
        lines = content.splitlines()
        rfc_title = "RFC"
        for line in lines:
            if _RFC_HEADING_RE.match(line):
                rfc_title = line.strip("# ").strip()[:120]
                break
        claim = _first_sentence(content, 500)
        alts = _extract_alternatives(content)
        _add(rfc_title, claim, "question", 0.7, alts)

    # Pattern 4: tradeoff table header
    if _TRADEOFF_TABLE_RE.search(content):
        title = "Tradeoff evaluation"
        for line in content.splitlines():
            if _TRADEOFF_TABLE_RE.match(line):
                title = line.strip("|").strip()[:120]
                break
        claim = _first_sentence(content, 500)
        alts = _extract_alternatives(content)
        _add(title, claim, "evidence", 0.65, alts)

    # Pattern 5: explicit resolution marker
    if _RESOLUTION_MARKER_RE.search(content):
        title = "Decision"
        for line in content.splitlines():
            if _RESOLUTION_MARKER_RE.search(line):
                title = line.strip("* ").strip()[:120]
                break
        claim = _first_sentence(content, 500)
        _add(title, claim, "decision", 0.8)

    # Deduplicate by thread_slug
    seen: dict = {}
    for c in candidates:
        if c.thread_slug not in seen:
            seen[c.thread_slug] = c
    return list(seen.values())


# ---------------------------------------------------------------------------
# Sprint 6: LLM enrichment (opt-in, gated by session_decision_llm flag)
# ---------------------------------------------------------------------------


def _enrich_candidates_with_llm(
    candidates: list[DecisionCandidate],
    content: str,
) -> list[DecisionCandidate]:
    """Optionally enrich heuristic candidates via LLM.

    Gated by ``session_decision_llm`` config flag (default: off).
    On any failure (flag off, LLM unavailable, timeout, parse error)
    the original *candidates* list is returned unchanged.

    Time budget: ~5s. Token budget: 200 output tokens.
    """
    if not candidates:
        return candidates
    try:
        from config import get_config

        if not get_config().session_decision_llm:
            return candidates
    except Exception as exc:
        logger.debug("session_decision_llm config unavailable: %s", exc)
        return candidates

    try:
        from llm_extraction import is_llm_extraction_available, _get_extractor
    except ImportError:
        return candidates

    if not is_llm_extraction_available():
        return candidates

    try:
        extractor = _get_extractor()
        if not extractor.is_loaded:
            return candidates

        prompt = (
            "From the following note, extract structured decisions as JSON. "
            "Each decision has: title (short string), claim (1-2 sentences), "
            "alternatives (list of strings, may be empty), confidence (0-1).\n\n"
            f"Note:\n{content[:4000]}\n\n"
            "Return JSON array only, no explanation."
        )
        result = extractor.extract(prompt, max_tokens=200)
        llm_decisions = result.get("decisions") or result.get("facts") or []
        if not llm_decisions:
            return candidates

        for d in llm_decisions[:3]:
            if isinstance(d, dict):
                title = str(d.get("title", ""))[:120]
                claim = str(d.get("claim", ""))[:500]
                alts = d.get("alternatives", [])
                if isinstance(alts, list):
                    alts = [str(a) for a in alts][:5]
                else:
                    alts = []
                try:
                    conf = float(d.get("confidence", 0.5))
                except (TypeError, ValueError):
                    conf = 0.5
                if title or claim:
                    slug = _slugify(title) if title else _slugify(claim[:40])
                    candidates.append(
                        DecisionCandidate(
                            title=title or claim[:120],
                            claim=claim,
                            event_type="decision",
                            confidence=max(0.0, min(1.0, conf)),
                            thread_slug=slug,
                            alternatives=alts,
                        )
                    )
    except Exception as exc:
        logger.warning("decision_extraction: failed to persist candidate: %s", exc)

    return candidates
