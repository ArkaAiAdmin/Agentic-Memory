"""BB1 / BB2 synthesis primitives for the search pipeline.

Extracted from search_pipeline.py (2026-06-20) as part of the god-module
decomposition. Contains:

- BB1 (sentence-level answer synthesis): _bb1_split_sentences, _bb1_synthesize
- BB2 (conversational reference resolution): _bb2_extract_terms,
  _bb2_is_reference_query, _bb2_resolve, _bb2_record_turn, _bb2_clear_history

The BB2 turn history (``_BB2_TURNS``) is module-level state. To keep
a single source of truth, it lives here and is re-exported through
search_pipeline so any code that reads/writes
``search_pipeline._BB2_TURNS`` still sees the same list object.

Behavior is identical to the inline versions. Re-exported from
search_pipeline for backward compat.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_BB1_SENT_SPLIT = re.compile("(?<=[.!?])\\s+(?=[A-Z\\\"'])|\\n+")
_BB1_DEFAULT_MAX_SENTENCES = 5
_BB1_CONTEXT_SENTENCES = 1

_BB2_HISTORY_MAX = 20
_BB2_TURNS: list = []
_BB2_LOCK = threading.Lock()
_BB2_PRONOUNS = frozenset(
    {"it", "its", "that", "this", "these", "those", "they", "them", "their"}
)
_BB2_REF_PHRASES = (
    "previous one",
    "previous",
    "earlier",
    "above",
    "before",
    "last one",
    "same",
    "that one",
    "this one",
    "the one",
    "those ones",
    "again",
    "more on",
    "more about",
    "more details",
    "elaborate",
    "expand on",
    "what about",
    "how about",
    "related to",
    "re:",
    "f:",
)
_BB2_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "of",
        "for",
        "to",
        "in",
        "on",
        "with",
        "and",
        "or",
        "but",
        "i",
        "you",
        "we",
        "it",
        "that",
        "this",
        "what",
        "how",
        "why",
        "when",
        "where",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "be",
        "been",
        "as",
        "by",
        "at",
        "from",
        "or",
        "if",
        "so",
        "not",
        "no",
    }
)


def _bb1_split_sentences(text: str) -> list:
    """BB1: split text into sentences with their starting offset.

    Returns a list of (start_offset, end_offset, sentence_text) tuples.
    The regex is conservative — it only splits on `[.!?]` followed by
    whitespace and an uppercase letter / quote, so URLs, abbreviations
    like "e.g.", and decimals are usually not split.
    """
    if not text:
        return []
    spans = []
    pos = 0
    for m in _BB1_SENT_SPLIT.finditer(text):
        s_end = m.start()
        if s_end > pos:
            spans.append((pos, s_end, text[pos:s_end]))
        pos = m.end()
    if pos < len(text):
        spans.append((pos, len(text), text[pos:]))
    if not spans:
        return [(0, len(text), text)]
    return spans


def _bb1_synthesize(
    query: str,
    results: list,
    max_sentences: int = _BB1_DEFAULT_MAX_SENTENCES,
    display_scores: Optional[dict] = None,
) -> dict:
    """BB1: build a synthesized answer from the top search results.

    Args:
        query: the original search query (for CE scoring of sentences)
        results: list of (note_id, content, source_file, final_score, ...) tuples
                 (must contain at least the first 6 fields)
        max_sentences: cap on the number of sentences in the synthesis
        display_scores: Optional mapping ``{note_id: display_score}`` from the
            post-rank enrichment envelope. When provided, the per-result
            baseline used to weight sentences is the *enriched* score (concept
            / centrality / neural-forget surprise folded in) instead of the
            raw final_score, so synthesis reflects the same enriched signal
            as answer-rerank. Recency is already inside final_score and is
            excluded from display_score (no double-count).

    Returns:
        dict with:
          "answer": the synthesized text
          "sentences": list of {note_id, source_file, sentence, score, start}
          "sources": list of unique note_ids that contributed
          "skipped_low_relevance": count of results that scored 0
    """
    # Imported here to avoid a module-level import cycle: search.rerankers
    # is imported by search_pipeline, and search_pipeline imports
    # search.synthesis. So at synthesis-import time, rerankers may not
    # be fully wired yet. A function-local import is the safe pattern.
    from search.rerankers import _cross_encoder_score

    if not results or not query:
        return {
            "answer": "",
            "sentences": [],
            "sources": [],
            "skipped_low_relevance": 0,
        }
    per_result_top = []
    skipped = 0
    for r in results:
        note_id = r[0]
        content = r[1] if len(r) > 1 else ""
        source_file = r[2] if len(r) > 2 else ""
        if display_scores and note_id in display_scores:
            try:
                content_score = float(display_scores[note_id])
            except (TypeError, ValueError):
                content_score = r[6] if len(r) > 6 else 1.0
        else:
            content_score = r[6] if len(r) > 6 else 1.0
        sentences = _bb1_split_sentences(content or "")
        if not sentences:
            continue
        best = None
        for s_off, e_off, sent_text in sentences:
            ce = _cross_encoder_score(query, sent_text)
            if ce <= 0.0:
                continue
            if best is None or ce > best[0]:
                best = (ce, s_off, e_off, sent_text, sentences)
        if best is None:
            skipped += 1
            continue
        ce, s_off, e_off, sent_text, sentences = best
        per_result_top.append(
            (
                note_id,
                source_file,
                ce,
                s_off,
                e_off,
                sent_text,
                sentences,
                content_score,
            )
        )
    if not per_result_top:
        return {
            "answer": "",
            "sentences": [],
            "sources": [],
            "skipped_low_relevance": skipped,
        }
    per_result_top.sort(key=lambda x: x[2] * x[7], reverse=True)
    top = per_result_top[:max_sentences]
    answer_parts = []
    sentences_out = []
    sources_seen = set()
    sources_order = []
    for (
        note_id,
        source_file,
        ce,
        s_off,
        e_off,
        sent_text,
        sentences,
        content_score,
    ) in top:
        sources_seen.add(note_id)
        if note_id not in sources_order:
            sources_order.append(note_id)
        idx = next((i for i, (so, eo, t) in enumerate(sentences) if so == s_off), 0)
        before = max(0, idx - _BB1_CONTEXT_SENTENCES)
        after = min(len(sentences), idx + _BB1_CONTEXT_SENTENCES + 1)
        block = sentences[before:after]
        block_text = " ".join((t for _, _, t in block))
        answer_parts.append(f"[From {note_id}]\n{block_text}")
        sentences_out.append(
            {
                "note_id": note_id,
                "source_file": source_file,
                "sentence": sent_text,
                "score": ce,
                "start": s_off,
                "end": e_off,
            }
        )
    answer_text = "\n\n".join(answer_parts)
    solver_answers = []
    try:
        from search.phases.math_aggregator import extract_and_aggregate_quantities
        m_sum = extract_and_aggregate_quantities(query, results)
        if m_sum:
            solver_answers.append(f"Calculated Total: {m_sum}")
    except Exception:
        pass

    try:
        from search.phases.temporal_delta_solver import calculate_temporal_delta
        t_delta = calculate_temporal_delta(query, results)
        if t_delta:
            solver_answers.append(f"Time Interval: {t_delta}")
    except Exception:
        pass

    try:
        from search.phases.attribute_extractor import extract_entity_attribute
        attr_v = extract_entity_attribute(query, results)
        if attr_v:
            solver_answers.append(f"Attribute Value: {attr_v}")
    except Exception:
        pass

    if solver_answers:
        answer_text = " | ".join(solver_answers) + "\n\n" + answer_text

    return {
        "answer": answer_text,
        "sentences": sentences_out,
        "sources": sources_order,
        "skipped_low_relevance": skipped,
    }


def _bb2_extract_terms(text: str) -> list:
    """BB2: extract retrieval terms from text, dropping stopwords.

    Returns up to 8 terms. Order is preserved (earlier = more salient).
    """
    if not text:
        return []
    seen = set()
    out = []
    for tok in re.findall("[A-Za-z][A-Za-z0-9_]+", text.lower()):
        if len(tok) < 3:
            continue
        if tok in _BB2_STOPWORDS:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= 8:
            break
    return out


def _bb2_is_reference_query(query: str) -> bool:
    """BB2: True if the query has pronouns or reference phrases that
    need prior context to make sense as a literal FTS5 query.
    """
    if not query:
        return False
    q = query.strip().lower()
    tokens = q.split()
    if not tokens:
        return False
    if len(tokens) <= 4:
        pronoun_count = sum(1 for t in tokens if t.strip(".,?!:;()[]\"'") in _BB2_PRONOUNS)
        if pronoun_count >= 1:
            return True
    for phrase in _BB2_REF_PHRASES:
        if phrase in q:
            return True
    return False


def _bb2_resolve(query: str) -> dict:
    """BB2: resolve a reference query using the prior-turn history.

    Returns a dict:
        "expanded_query": the original query with prior terms appended
        "origin_turn":    index of the turn whose terms were reused
        "added_terms":    list of terms that were appended
        "reused":         bool — True if any terms were added
    """
    if not _bb2_is_reference_query(query):
        return {
            "expanded_query": query,
            "origin_turn": -1,
            "added_terms": [],
            "reused": False,
        }
    with _BB2_LOCK:
        if not _BB2_TURNS:
            return {
                "expanded_query": query,
                "origin_turn": -1,
                "added_terms": [],
                "reused": False,
            }
        last = _BB2_TURNS[-1]
        current_lower = set(re.findall("[A-Za-z][A-Za-z0-9_]+", query.lower()))
        candidates = [t for t in last.get("terms", []) if t not in current_lower]
        if not candidates:
            return {
                "expanded_query": query,
                "origin_turn": -1,
                "added_terms": [],
                "reused": False,
            }
        added = candidates[:5]
        expanded = f"{query} {' '.join(added)}"
        turn_idx = len(_BB2_TURNS) - 1
    return {
        "expanded_query": expanded,
        "origin_turn": turn_idx,
        "added_terms": added,
        "reused": True,
    }


def _bb2_record_turn(query: str, top_results: list) -> None:
    """BB2: record a (query, terms) pair for future reference resolution.

    `top_results` is the list of result tuples from search_memories. We
    extract terms from the *query* (since that's what we'd need to
    reproduce) and from the first few result IDs (so a follow-up
    "re-lookup" can find them again).

    The buffer is bounded to ``_BB2_HISTORY_MAX`` entries; older turns
    are evicted FIFO.
    """
    if not query:
        return
    terms = _bb2_extract_terms(query)
    ids = []
    for r in (top_results or [])[:3]:
        if r and len(r) > 0:
            ids.append(r[0])
    with _BB2_LOCK:
        _BB2_TURNS.append(
            {
                "query": query,
                "terms": terms,
                "ids": ids,
                "ts": __import__("time").time(),
            }
        )
        while len(_BB2_TURNS) > _BB2_HISTORY_MAX:
            _BB2_TURNS.pop(0)


def _bb2_clear_history() -> None:
    """BB2: clear the in-memory turn buffer. Test-only."""
    with _BB2_LOCK:
        _BB2_TURNS.clear()
