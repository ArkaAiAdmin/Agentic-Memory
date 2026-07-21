"""Auto-Summarization for agentic-memory.

Extractive summarization of long notes using TF-IDF sentence scoring.
No LLM required — pure algorithmic.

Opt-in via MEMORY_SUMMARIZATION=1.
"""

from __future__ import annotations

import json
import logging

import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from config import resolve_db_path
from infra.memory_config import GLOBAL_MEM_DIR

logger = logging.getLogger(__name__)


def is_llm_summarization_available() -> bool:
    """Check if LLM summarization is active and available."""
    try:
        from infra._lazy_imports import get_config
        cfg = get_config()
        # Enable if llm_summarization is True or either env var is set to 1
        if getattr(cfg, "llm_summarization", False):
            return True
    except Exception as e:
        logger.warning("is_llm_summarization_available failed: %s", e)

    if os.environ.get("MEMORY_LLM_SUMMARIZATION") == "1" or os.environ.get("MEMORY_LLM_EXTRACTION") == "1":
        try:
            from fact.llm_providers import get_provider
            return get_provider() is not None
        except Exception as e:
            logger.warning("is_llm_summarization_available failed: %s", e)
            return False
    return False


def summarize_text_via_llm(text: str) -> Optional[str]:
    """Summarize text using the configured LLM provider (abstractive summarization)."""
    if not is_llm_summarization_available():
        return None
        
    try:
        from fact.llm_providers import get_provider
        provider = get_provider()
        if not provider:
            return None
            
        prompt = (
            "You are a helpful assistant. Write a concise, 1-3 sentence abstractive summary "
            "of the following text, focusing on the key takeaways, decisions, or preferences:\n\n"
            f"{text.strip()}\n\n"
            "Summary:"
        )
        
        summary = provider.generate(prompt, max_tokens=150, temperature=0.3)
        if summary:
            return summary.strip()
    except Exception as e:
        logger.debug("LLM summarization failed: %s", e)
        
    return None

__all__ = [
    "SUMMARIZATION_ENABLED",  # noqa: F822 — dynamically resolved via __getattr__
    "summarize_text",
    "summarize_note",
    "auto_summarize_long",
    "summarization_stats",
]

# SUMMARIZATION_ENABLED is dynamically resolved via __getattr__

_MAX_SUMMARY_SENTENCES = 5
_MIN_CONTENT_LENGTH = 500  # only summarize notes longer than this
_STOP_WORDS = frozenset(
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
        "shall",
        "can",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "because",
        "but",
        "and",
        "or",
        "if",
        "while",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
    }
)


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s.strip() for s in sentences if len(s.strip()) > 10]


def _tokenize_words(text: str) -> list[str]:
    """Tokenize text into lowercase word tokens."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return [w for w in text.split() if w not in _STOP_WORDS and len(w) > 1]


def _compute_tfidf(sentences: list[str]) -> list[dict]:
    """Compute TF-IDF scores for each sentence."""
    if not sentences:
        return []

    # Build document frequency
    doc_freq: Counter[str] = Counter()
    sent_tokens = []
    for s in sentences:
        tokens = _tokenize_words(s)
        sent_tokens.append(tokens)
        unique = set(tokens)
        for t in unique:
            doc_freq[t] += 1

    n_docs = len(sentences)
    scores = []
    for idx, tokens in enumerate(sent_tokens):
        if not tokens:
            scores.append({"sentence": idx, "score": 0.0})
            continue
        tf = Counter(tokens)
        total = len(tokens)
        score = 0.0
        for word, count in tf.items():
            idf = math.log((n_docs + 1) / (doc_freq.get(word, 0) + 1)) + 1
            score += (count / total) * idf
        scores.append({"sentence": idx, "score": score})

    return scores


def summarize_text(text: str, max_sentences: int = _MAX_SUMMARY_SENTENCES) -> str:
    """Summarize text using LLM if available; otherwise, pick top TF-IDF sentences."""
    if not text or len(text.strip()) < _MIN_CONTENT_LENGTH:
        return text

    # Try LLM abstractive summarization first
    llm_sum = summarize_text_via_llm(text)
    if llm_sum:
        return llm_sum

    # Fallback to TF-IDF extractive summarization
    sentences = _split_sentences(text)
    if len(sentences) <= max_sentences:
        return text

    scores = _compute_tfidf(sentences)
    top = sorted(scores, key=lambda x: x["score"], reverse=True)[:max_sentences]
    top.sort(key=lambda x: x["sentence"])  # restore original order
    return " ".join(sentences[t["sentence"]] for t in top)


def summarize_note(
    note_id: str,
    db_path: str | None = None,
    max_sentences: int = _MAX_SUMMARY_SENTENCES,
) -> str:
    """Summarize a specific note and store the summary.

    Args:
        note_id: the note ID (category/title_slug)
        db_path: optional path to memory.db
        max_sentences: max sentences in summary

    Returns:
        summary text, or empty string on error
    """
    import sys

    if not sys.modules[__name__].SUMMARIZATION_ENABLED:
        return ""

    if db_path is not None:
        local_mem = resolve_db_path(db_path).parent
        global_mem = Path(GLOBAL_MEM_DIR)
    else:
        try:
            from infra._lazy_imports import get_memory_paths

            _, local_mem, global_mem = get_memory_paths()
        except ImportError:
            return ""
    db = db_path if db_path is not None else str(local_mem / "memory.db")

    try:
        from infra.db_write_queue import sqlite_write_queue

        conn = sqlite_write_queue.start_session(Path(db))
        try:
            row = conn.execute(
                "SELECT content FROM memories WHERE id = ? AND deleted_at IS NULL",
                (note_id,),
            ).fetchone()
            if not row or not row[0]:
                return ""

            content = row[0]
            summary = summarize_text(content, max_sentences)
            try:
                meta_row = conn.execute(
                    "SELECT metadata FROM memories WHERE id = ?", (note_id,)
                ).fetchone()
                meta = json.loads(
                    meta_row[0] if meta_row is not None else None
                    or "{}"
                )
            except (json.JSONDecodeError, TypeError):
                meta = {}
            meta["auto_summary"] = summary
            meta["auto_summary_length"] = len(summary)
            conn.execute(
                "UPDATE memories SET metadata = ? WHERE id = ?",
                (json.dumps(meta), note_id),
            )
            conn.commit()
            return summary
        finally:
            conn.close()
    except Exception as e:
        logger.warning("summarize_note failed: %s", e)
        return ""


def auto_summarize_long(
    min_length: int = _MIN_CONTENT_LENGTH,
    max_sentences: int = _MAX_SUMMARY_SENTENCES,
    dry_run: bool = False,
    db_path: str | None = None,
) -> dict:
    """Summarize all notes exceeding min_length.

    Args:
        min_length: minimum content length to trigger summarization
        max_sentences: max sentences per summary
        dry_run: if True, don't write anything
        db_path: optional path to memory.db

    Returns:
        dict with stats
    """
    import sys

    if not sys.modules[__name__].SUMMARIZATION_ENABLED:
        return {"enabled": False}

    if db_path is not None:
        local_mem = resolve_db_path(db_path).parent
        global_mem = Path(GLOBAL_MEM_DIR)
        db = db_path
    else:
        try:
            from infra._lazy_imports import get_memory_paths

            _, local_mem, global_mem = get_memory_paths()
            db = str(local_mem / "memory.db")
        except ImportError:
            return {"enabled": True, "error": "memory_common not found"}

    try:
        from infra.db_write_queue import sqlite_write_queue

        conn = sqlite_write_queue.start_session(Path(db))
        try:
            rows = conn.execute(
                "SELECT id, content FROM memories WHERE deleted_at IS NULL "
                "AND LENGTH(content) > ?",
                (min_length,),
            ).fetchall()

            summarized = 0
            skipped = 0
            for note_id, content in rows:
                summary = summarize_text(content, max_sentences)
                if summary == content:
                    skipped += 1
                    continue
                if not dry_run:
                    try:
                        meta_row = conn.execute(
                            "SELECT metadata FROM memories WHERE id = ?", (note_id,)
                        ).fetchone()
                        meta = json.loads(
                            meta_row[0] if meta_row is not None else None
                            or "{}"
                        )
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                    meta["auto_summary"] = summary
                    meta["auto_summary_length"] = len(summary)
                    conn.execute(
                        "UPDATE memories SET metadata = ? WHERE id = ?",
                        (json.dumps(meta), note_id),
                    )
                summarized += 1

            if not dry_run:
                conn.commit()
            return {
                "enabled": True,
                "eligible": len(rows),
                "summarized": summarized,
                "skipped": skipped,
                "dry_run": dry_run,
            }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("auto_summarize_long failed: %s", e)
        return {"enabled": True, "error": str(e)}


def summarization_stats(db_path: str | None = None) -> dict:
    """Return summarization statistics.

    Args:
        db_path: optional path to memory.db

    Returns:
        dict with stats
    """
    import sys

    if not sys.modules[__name__].SUMMARIZATION_ENABLED:
        return {"enabled": False}

    if db_path is not None:
        local_mem = resolve_db_path(db_path).parent
        global_mem = Path(GLOBAL_MEM_DIR)
        db = db_path
    else:
        try:
            from infra._lazy_imports import get_memory_paths

            _, local_mem, global_mem = get_memory_paths()
            db = str(local_mem / "memory.db")
        except ImportError:
            return {"enabled": True, "error": "memory_common not found"}

    try:
        from infra.db import open_db
        with open_db(Path(db), pooled=True, write=False) as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
            ).fetchone()
            total = int(total_row[0]) if total_row is not None else 0
            long_notes_row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL "
                "AND LENGTH(content) > ?",
                (_MIN_CONTENT_LENGTH,),
            ).fetchone()
            long_notes = int(long_notes_row[0]) if long_notes_row is not None else 0
            summarized_row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL "
                "AND metadata LIKE '%auto_summary%'"
            ).fetchone()
            summarized = int(summarized_row[0]) if summarized_row is not None else 0
            return {
                "enabled": True,
                "total_notes": total,
                "eligible_long": long_notes,
                "already_summarized": summarized,
                "min_content_length": _MIN_CONTENT_LENGTH,
            }
    except Exception as e:
        logger.warning("summarization_stats failed: %s", e)
        return {"enabled": True, "error": "stats unavailable"}


from infra.memory_common import make_lazy_getattr

__getattr__ = make_lazy_getattr({"SUMMARIZATION_ENABLED": "summarization"})
