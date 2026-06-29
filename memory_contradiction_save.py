#!/usr/bin/env python3
"""Save-time contradiction detection for Agentic Memory.

When ``memory_save`` is about to commit a new note, this module scans
recent + semantically similar notes and returns any contradictions.

Detection pipeline (fast → slow):
1. **Phrase scan**: rule-based antonym/negation matching against the
   candidate set (cheap, no embedding lookups).
2. **Embedding augmentation**: when the usearch vector index is available,
   semantically similar notes are added to the candidate pool, catching
   contradictions with older related notes (moderate cost).
3. **LLM validation (optional, off by default)**: if
   ``MEMORY_CONTRADICTION_LLM=1`` is set, the top findings are sent to
   ``claude -p`` for a second-opinion check (highest cost, gated).

Design constraints
------------------
* **Cheap** (default path): no embedding lookups, no LLM calls. Just
  the phrase-mode detector applied to recent notes plus optional
  embedding-augmented candidates.
* **Top-N bounded**: total candidates never exceeds ``top_n`` (split
  between recency and semantic budgets).
* **Safe to fail**: any exception is logged and ``[]`` is returned.
  The save path must not break because the warning layer is broken.
* **Dynamic import**: the detector is imported inside the call site,
  not at module load, so a move / rename does not break the package.

Why the spec's `detect_contradictions_phrase(notes_a, notes_b, ...)`
wrapper is not used
------------------------------------------------
That exact symbol does not exist in ``contradiction_detector.py`` at
the time of writing — the public phrase-mode entry point is
``detect_contradictions(memory_dir, ...)`` which scans the whole DB
itself. The 2-note phrase-mode check is performed inline here by
calling the module's actual phrase helpers
(``NEGATION_PAIRS``, ``split_segments``, ``find_phrase_in_sentence``,
``subject_overlap``) directly. Behaviour matches the spec: same
confidence tiers, same subject-overlap gate, same word-bounded phrase
matching — but the candidate set is bounded by the caller (this
module) rather than by the detector.
"""

import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Truncate each note's content to this many chars before feeding to the
# phrase detector. The detector is a string-match scan, not a full NLP
# pass, but a multi-MB note would still be slow to tokenise and offers
# no extra signal beyond the first ~1k chars.
_MAX_CONTENT_CHARS_FOR_DETECTOR = 1000

# Map the detector's "low < medium < high" string labels to numeric
# ranks where higher = more confident. This is the SAME convention
# contradiction_detector.py uses (line 342), kept here as a local
# constant so this module does not depend on the detector being
# importable just to sort findings.
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# The detector's MIN_SUBJECT_OVERLAP gate is 5.0 (raw count of shared
# significant words across the full notes). For the "low" tier we use
# a much weaker floor — at least ONE shared significant word — so we
# still filter out completely unrelated notes ("Apple pie is true" vs
# "Samsung battery is false") while surfacing weak signal pairs that
# the strict medium tier would miss.
_LOW_OVERLAP_FLOOR = 1


# ---------------------------------------------------------------------------
# Snippet helper
# ---------------------------------------------------------------------------


def _make_snippet(content: str, max_chars: int = 200) -> str:
    """Return the first ``max_chars`` chars of ``content``, ellipsized
    at the last word boundary if truncation actually happened.

    If truncation happened, ``"..."`` is appended (so the worst-case
    return length is ``max_chars + 3``).

    If there is no space in the first ``max_chars`` chars (e.g. a
    single very long word), the snippet is hard-truncated at
    ``max_chars`` rather than emitting a mid-word break.
    """
    if not content:
        return ""
    if len(content) <= max_chars:
        return content
    # Walk backwards from max_chars to find the last whitespace.
    cut = content[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > 0:
        truncated = cut[:last_space]
    else:
        # No space in window — fall back to hard cut.
        truncated = cut
    return truncated + "..."


# ---------------------------------------------------------------------------
# Dynamic detector import
# ---------------------------------------------------------------------------


def _import_detector_helpers():
    """Return ``(NEGATION_PAIRS, split_segments, find_phrase_in_sentence,
    subject_overlap)`` from ``contradiction_detector``, or ``None`` on
    ImportError.

    Import is done at call time so a missing / moved detector does not
    break module import. The four helpers are the only pieces we need
    to perform a phrase-mode check on a single 2-note pair; we do NOT
    use ``detect_contradictions`` itself because that function takes a
    memory directory and queries the DB itself, which conflicts with
    this module's "top-N candidates chosen by the caller" design.
    """
    try:
        from contradiction_detector import (
            NEGATION_PAIRS,
            split_segments,
            find_phrase_in_sentence,
            subject_overlap,
        )

        return NEGATION_PAIRS, split_segments, find_phrase_in_sentence, subject_overlap
    except ImportError as e:
        logger.warning(
            "check_contradictions_on_save: contradiction_detector import failed: %s",
            e,
        )
        return None


# ---------------------------------------------------------------------------
# Phrase-mode check on a single 2-note pair
# ---------------------------------------------------------------------------


def _phrase_check_pair(
    existing_id: str,
    existing_content: str,
    new_id: str,
    new_content: str,
    helpers,
) -> list:
    """Run the phrase-mode contradiction check on a single (existing,
    new) pair. Returns a list of finding dicts:

        {
            "pos": str,
            "neg": str,
            "confidence": "low" | "medium" | "high",
            "evidence_a": str,
            "evidence_b": str,
        }

    The confidence tiers mirror ``contradiction_detector.detect_contradictions``
    plus an extra "low" tier for cross-note pos+neg matches with weak
    (but non-zero) subject overlap. See module docstring for rationale.

    ``helpers`` is the 4-tuple returned by ``_import_detector_helpers``.
    """
    NEGATION_PAIRS, split_segments, find_phrase_in_sentence, subject_overlap = helpers

    segs_a = [s for s, _ in split_segments(existing_content)]
    segs_b = [s for s, _ in split_segments(new_content)]
    if not segs_a or not segs_b:
        return []

    # Short-circuit pathological long sentences: O(segs_a * segs_b) pair
    # check is wasted work when neither side can plausibly contain a
    # contradiction cue. Cheap fail-fast before entering the pair loop.
    if max(len(segs_a), len(segs_b)) > 50:
        return []

    findings = []

    for pos, neg in NEGATION_PAIRS:
        # (1) HIGH: pos in some segment of A AND neg in some segment of
        # B (or the other way) AND those two segments share enough
        # significant vocabulary.
        high_found = False
        for s1 in segs_a:
            if not find_phrase_in_sentence(s1, pos):
                continue
            for s2 in segs_b:
                if not find_phrase_in_sentence(s2, neg):
                    continue
                seg_overlap, _ = subject_overlap(s1, s2)
                if seg_overlap >= 1.5:  # MIN_SEGMENT_OVERLAP in detector
                    findings.append(
                        {
                            "pos": pos,
                            "neg": neg,
                            "confidence": "high",
                            "evidence_a": s1[:200],
                            "evidence_b": s2[:200],
                        }
                    )
                    high_found = True
                    break
            if high_found:
                break
        if high_found:
            continue

        # (2) MEDIUM: pos/neg split across the two notes, AND the
        # notes share enough overall subject vocabulary.
        pos_in_a = any(find_phrase_in_sentence(s, pos) for s in segs_a)
        neg_in_b = any(find_phrase_in_sentence(s, neg) for s in segs_b)
        pos_in_b = any(find_phrase_in_sentence(s, pos) for s in segs_b)
        neg_in_a = any(find_phrase_in_sentence(s, neg) for s in segs_a)
        if (pos_in_a and neg_in_b) or (pos_in_b and neg_in_a):
            overall_overlap, _ = subject_overlap(existing_content, new_content)
            if overall_overlap >= 5.0:  # MIN_SUBJECT_OVERLAP in detector
                findings.append(
                    {
                        "pos": pos,
                        "neg": neg,
                        "confidence": "medium",
                        "evidence_a": f'phrase "{pos}" found',
                        "evidence_b": f'phrase "{neg}" found',
                    }
                )
                continue
            # (3) LOW: pos+neg cross-note, but overlap too weak for
            # medium. Require at least _LOW_OVERLAP_FLOOR shared
            # significant word so we don't surface obvious junk like
            # "Apple pie is true" vs "Samsung battery is false".
            if overall_overlap >= _LOW_OVERLAP_FLOOR:
                findings.append(
                    {
                        "pos": pos,
                        "neg": neg,
                        "confidence": "low",
                        "evidence_a": f'phrase "{pos}" found',
                        "evidence_b": f'phrase "{neg}" found',
                    }
                )

    return findings


# ---------------------------------------------------------------------------
# Content-hash cache for LLM check deduplication
# ---------------------------------------------------------------------------

_llm_cache: dict[tuple[str, str], bool | None] = {}


def _llm_cache_key(content_a: str, content_b: str) -> tuple[str, str]:
    """Return a canonical cache key for a pair of note contents.

    Keys are sorted so that ``(A, B)`` and ``(B, A)`` produce the same
    lookup. Each content is hashed to a short digest so the cache dict
    does not hold copies of the full note body.
    """
    import hashlib

    ha = hashlib.sha256(content_a.encode("utf-8")).hexdigest()[:16]
    hb = hashlib.sha256(content_b.encode("utf-8")).hexdigest()[:16]
    return (ha, hb) if ha <= hb else (hb, ha)


def _llm_check_findings(findings: list[dict]) -> list[dict]:
    """Validate the top findings with ``claude -p`` (if available).

    Returns only the findings the LLM confirms as contradictions. Gated
    behind ``MEMORY_CONTRADICTION_LLM=1`` env var. Silently returns
    original findings if ``claude`` is unavailable or the call fails.

    Results are cached by content-hash pair so repeated checks on the
    same (or reverse-ordered) pair are instant.
    """
    import os

    if os.environ.get("MEMORY_CONTRADICTION_LLM") != "1":
        return findings

    # Limit to top findings to keep LLM cost bounded.
    top = sorted(
        findings,
        key=lambda f: _CONFIDENCE_RANK.get(f.get("confidence", "low"), 0),
        reverse=True,
    )[:3]
    if not top:
        return findings

    import json
    import subprocess

    validated: list[dict] = []
    for f in top:
        existing_snippet = f.get("existing_content_snippet", "")
        new_id_snippet = f.get("new_content_snippet", "")
        ct = f.get("contradiction_type", "")
        pair = f.get("pair", ("", ""))

        # Build a unique key for this pair of contents.
        cache_key = _llm_cache_key(existing_snippet, new_id_snippet or str(pair))
        cached = _llm_cache.get(cache_key)
        if cached is True:
            validated.append(f)
            continue
        if cached is False:
            continue  # LLM said not a contradiction

        prompt = (
            f"You are a contradiction detector. Two notes from a knowledge base are shown below. "
            f"Does the second note contradict the first? Answer with a single JSON object: "
            f'{{"is_contradiction": true|false, "reason": "..."}}\n\n'
            f"--- Note A ---\n{existing_snippet[:800]}\n\n"
            f"--- Note B ---\n{new_id_snippet[:800] if new_id_snippet else ct}\n\n"
            f"Is this a contradiction?"
        )
        try:
            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=15,
            )
            stdout = result.stdout.strip()
            if not stdout:
                _llm_cache[cache_key] = None
                validated.append(f)  # keep on empty response
                continue
            parsed = json.loads(stdout)
            if parsed.get("is_contradiction") is True:
                _llm_cache[cache_key] = True
                f["llm_validated"] = True
                f["llm_reason"] = parsed.get("reason", "")
                validated.append(f)
            else:
                _llm_cache[cache_key] = False
        except (
            FileNotFoundError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
            subprocess.CalledProcessError,
        ) as e:
            logger.debug("LLM contradiction check failed (non-fatal): %s", e)
            _llm_cache[cache_key] = None
            validated.append(f)  # keep on error

    # If LLM rejected everything, return empty list.
    # If LLM accepted some or errored on all, return those.
    return validated


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _safe_iso_to_epoch(value) -> int:
    """Best-effort parse of an ISO-8601 string to a unix epoch. Returns
    0 on any failure so a corrupt timestamp never breaks the sort.
    """
    if not value:
        return 0
    try:
        # Python's fromisoformat handles "2026-06-07T12:34:56" and
        # "2026-06-07T12:34:56.123456" without external deps.
        from datetime import datetime

        return int(datetime.fromisoformat(str(value)).timestamp())
    except Exception:
        logger.warning("Failed to parse memory timestamp: %s", value)
        return 0


def check_contradictions_on_save(
    db_path,
    new_content: str,
    new_id: str,
    top_n: int = 20,
    min_confidence: str = "low",
) -> list:
    """Scan recent notes for contradictions against ``new_content``.

    Cheap, phrase-mode, top-N-bounded. Returns a list of finding
    dicts; see module docstring for the schema. Always returns a list
    — never raises.

    Args:
        db_path: Path to the SQLite memory DB.
        new_content: Body of the new note being saved. May be empty
            (in which case the function short-circuits to ``[]``).
        new_id: Canonical id of the new note. Existing notes with the
            same id are excluded from the candidate set so a re-save
            does not produce a self-contradiction.
        top_n: Maximum number of existing notes to consider (most
            recent first by ``created_at``). Default 20.
        min_confidence: One of ``"low" | "medium" | "high"`` — minimum
            confidence to include in the returned findings. Default
            ``"low"`` (return everything the detector found).

    Returns:
        Sorted list of finding dicts. Empty list on no contradictions
        or on any internal error.
    """
    db_path = Path(db_path) if not isinstance(db_path, Path) else db_path
    new_id = new_id or "<new>"

    # Short-circuit on empty content — no detector pass needed.
    if not new_content or not new_content.strip():
        return []

    # Truncate the new content BEFORE doing anything else. The detector
    # is a string-match scan; we don't gain signal past ~1k chars and a
    # huge note would slow the scan down.
    new_content_trunc = new_content[:_MAX_CONTENT_CHARS_FOR_DETECTOR]

    # Validate min_confidence early so a typo doesn't silently downgrade.
    if min_confidence not in _CONFIDENCE_RANK:
        logger.warning(
            "check_contradictions_on_save: invalid min_confidence=%r, "
            "falling back to 'low'",
            min_confidence,
        )
        min_confidence = "low"
    min_rank = _CONFIDENCE_RANK[min_confidence]

    # Dynamic detector import. A failure here is logged inside the
    # helper AND re-logged here (with caller context) so the
    # ``check_contradictions_on_save`` skip is always attributable.
    helpers = _import_detector_helpers()
    if helpers is None:
        logger.warning(
            "check_contradictions_on_save: detector unavailable, "
            "skipping contradiction scan for new_id=%s",
            new_id,
        )
        return []

    # Candidate selection. Split budget between recency and semantic
    # search to catch contradictions with both recent and semantically
    # related notes. Total candidates never exceeds top_n.
    recency_budget = max(1, top_n // 2)
    semantic_budget = top_n - recency_budget
    try:
        from _lazy_imports import open_db

        with open_db(db_path, timeout=30, write=False) as conn:
            try:
                rows = conn.execute(
                    "SELECT id, content, created_at FROM memories "
                    "WHERE id != ? AND deleted_at IS NULL "
                    "ORDER BY created_at DESC LIMIT ?",
                    (new_id, recency_budget),
                ).fetchall()
            except Exception:
                logger.warning(
                    "deleted_at column not found, falling back to unfiltered recency query"
                )
                rows = conn.execute(
                    "SELECT id, content, created_at FROM memories "
                    "WHERE id != ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (new_id, recency_budget),
                ).fetchall()

            # Augment with semantically similar candidates from the
            # usearch index when available. This catches contradictions
            # with older but semantically related notes that the recency-
            # bounded query would miss.
            try:
                from _lazy_imports import get_embedding_search

                es = get_embedding_search()
                if es.model is not None:
                    sim_results = es.search(
                        new_content_trunc, str(db_path), limit=semantic_budget
                    )
                    if isinstance(sim_results, list):
                        seen_ids = {r[0] for r in rows}
                        for sr in sim_results:
                            mid = sr.get("id", "")
                            if mid and mid != new_id and mid not in seen_ids:
                                mem_row = conn.execute(
                                    "SELECT content, created_at FROM memories "
                                    "WHERE id = ? AND deleted_at IS NULL",
                                    (mid,),
                                ).fetchone()
                                if mem_row:
                                    seen_ids.add(mid)
                                    rows.append((mid, mem_row[0], mem_row[1]))
            except Exception:
                logger.debug("embedding augmentation skipped (not available)")
    except Exception as e:
        logger.warning("check_contradictions_on_save: DB read failed: %s", e)
        return []

    # Scan each candidate.
    raw_findings = []  # (confidence_rank, -created_at_epoch, finding_dict)
    for existing_id, existing_content_raw, created_at in rows:
        if not existing_id or not existing_content_raw:
            continue
        existing_content = existing_content_raw[:_MAX_CONTENT_CHARS_FOR_DETECTOR]
        if not existing_content.strip():
            continue
        try:
            pair_findings = _phrase_check_pair(
                existing_id,
                existing_content,
                new_id,
                new_content_trunc,
                helpers,
            )
        except Exception as e:
            # Per-pair errors must not break the whole scan.
            logger.warning(
                "check_contradictions_on_save: pair check failed for %s: %s",
                existing_id,
                e,
            )
            continue
        for f in pair_findings:
            conf = f["confidence"]
            if _CONFIDENCE_RANK.get(conf, 0) < min_rank:
                continue
            finding = {
                "existing_note_id": existing_id,
                "existing_content_snippet": _make_snippet(existing_content),
                "contradiction_type": f'"{f["pos"]}" vs "{f["neg"]}"',
                "confidence": conf,
                "pair": (existing_id, new_id),
            }
            raw_findings.append(
                (
                    -_CONFIDENCE_RANK.get(
                        conf, 0
                    ),  # high first (negative rank → ascending sort)
                    -_safe_iso_to_epoch(created_at),  # newer first
                    finding,
                )
            )

    raw_findings.sort(key=lambda t: (t[0], t[1]))
    results = [f for _, _, f in raw_findings]

    # Optional LLM validation gate. Only runs when MEMORY_CONTRADICTION_LLM=1.
    # This is intentionally a separate pass after the main scan so that the
    # LLM sees the same snippets the user sees and the phrase detector's work
    # is never wasted — the LLM only filters, it never adds detections.
    if results and os.environ.get("MEMORY_CONTRADICTION_LLM") == "1":
        results = _llm_check_findings(results)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list) -> int:
    """CLI: print findings as JSON so the user can sanity-check the
    detector against any DB.

    Usage:
        python memory_contradiction_save.py <db_path> <new_content> [--top-n N] [--min-confidence low|medium|high]
    """
    if len(argv) < 3:
        print(
            "Usage: python memory_contradiction_save.py <db_path> <new_content> "
            "[--top-n N] [--min-confidence low|medium|high]",
            file=sys.stderr,
        )
        return 2

    db_path = argv[1]
    new_content = argv[2]
    top_n = 20
    min_confidence = "low"
    for arg in argv[3:]:
        if arg.startswith("--top-n="):
            try:
                top_n = int(arg.split("=", 1)[1])
            except ValueError:
                print(f"Invalid --top-n: {arg}", file=sys.stderr)
                return 2
        elif arg.startswith("--min-confidence="):
            min_confidence = arg.split("=", 1)[1]
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            return 2

    findings = check_contradictions_on_save(
        db_path,
        new_content,
        new_id="<cli>",
        top_n=top_n,
        min_confidence=min_confidence,
    )
    # Tuples aren't JSON-native — convert pair to a list for clean output.
    out = []
    for f in findings:
        f2 = dict(f)
        if isinstance(f2.get("pair"), tuple):
            f2["pair"] = list(f2["pair"])
        out.append(f2)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
