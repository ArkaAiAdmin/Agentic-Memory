"""QW5 chunking primitives for the search pipeline.

Extracted from search_pipeline.py (2026-06-20) as part of the
god-module decomposition. Contains:

- _qw5_extract_keywords: keyword extraction for topic comparison
- _qw5_keyword_similarity: Jaccard similarity between keyword sets
- _qw5_is_topic_boundary: topic boundary detection
- _qw5_chunk_content: topic-aware chunk splitting
- _qw5_ensure_schema: idempotent chunks schema setup
- _qw5_index_chunks_for: replace chunks for a parent

The QW5 chunking system splits long memories into overlapping chunks
with topic-aware boundaries, then indexes them in a separate FTS5
table for chunk-level search.

Behavior is identical to the inline versions. Re-exported from
search_pipeline for backward compat.
"""

from __future__ import annotations

import logging

import re

logger = logging.getLogger(__name__)

_QW5_CHUNK_THRESHOLD = 2000
_QW5_CHUNK_TARGET_SIZE = 600
_QW5_CHUNK_OVERLAP = 81
_QW5_CHUNK_MAX_SIZE = 1200
_QW5_TOPIC_SIMILARITY_THRESHOLD = 0.15


def _get_topic_similarity_threshold() -> float:
    try:
        from infra._lazy_imports import get_config

        return float(get_config().topic_similarity_threshold)
    except Exception as e:
        logger.warning("_get_topic_similarity_threshold failed: %s", e)
        return _QW5_TOPIC_SIMILARITY_THRESHOLD


_QW5_SENT_BOUNDARY = re.compile("(?<=[.!?])\\s+|\\n+")
_QW5_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
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
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
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
        "each",
        "every",
        "both",
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
        "s",
        "t",
        "just",
        "don",
        "now",
    }
)

_QW5_CHUNKS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id TEXT NOT NULL,
    chunk_idx INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(parent_id, chunk_idx)
);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_parent_id ON memory_chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_parent ON memory_chunks(parent_id, chunk_idx);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
    content, parent_id, chunk_idx,
    content=memory_chunks,
    content_rowid=id,
    tokenize='porter unicode61'
);
"""
_QW5_CHUNKS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS memory_chunks_ai AFTER INSERT ON memory_chunks BEGIN
    INSERT INTO memory_chunks_fts(rowid, content, parent_id, chunk_idx)
    VALUES (new.id, new.content, new.parent_id, new.chunk_idx);
END;
CREATE TRIGGER IF NOT EXISTS memory_chunks_ad AFTER DELETE ON memory_chunks BEGIN
    INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, content, parent_id, chunk_idx)
    VALUES ('delete', old.id, old.content, old.parent_id, old.chunk_idx);
END;
CREATE TRIGGER IF NOT EXISTS memory_chunks_au AFTER UPDATE ON memory_chunks BEGIN
    INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, content, parent_id, chunk_idx)
    VALUES ('delete', old.id, old.content, old.parent_id, old.chunk_idx);
    INSERT INTO memory_chunks_fts(rowid, content, parent_id, chunk_idx)
    VALUES (new.id, new.content, new.parent_id, new.chunk_idx);
END;
"""


def _qw5_extract_keywords(text: str | None) -> set[str]:
    """Extract keywords from text for topic similarity comparison.

    Returns a set of lowercase words (3+ chars, excluding stopwords).
    """
    if not text:
        return set()
    words = re.findall("[a-z][a-z0-9]{2,}", text.lower())
    return {w for w in words if w not in _QW5_STOPWORDS}


def _qw5_keyword_similarity(text1: str, text2: str) -> float:
    """Compute Jaccard similarity between keyword sets of two texts.

    Returns a value in [0, 1] where 1 = identical keywords, 0 = no overlap.
    """
    kw1 = _qw5_extract_keywords(text1)
    kw2 = _qw5_extract_keywords(text2)
    if not kw1 or not kw2:
        return 0.0
    intersection = len(kw1 & kw2)
    union = len(kw1 | kw2)
    return intersection / union if union > 0 else 0.0


def _qw5_is_topic_boundary(prev_sentence: str, next_sentence: str) -> bool:
    """Detect if there's a topic boundary between two sentences.

    Uses keyword Jaccard similarity: if similarity drops below threshold,
    it's likely a topic change.
    """
    similarity = _qw5_keyword_similarity(prev_sentence, next_sentence)
    return similarity < _get_topic_similarity_threshold()


def _qw5_chunk_content(content: str) -> list:
    """QW5: split `content` into overlapping chunks with topic-aware boundaries.

    Returns a list of (start_offset, end_offset, chunk_text) tuples.
    A note below `_QW5_CHUNK_THRESHOLD` returns a single chunk equal to
    the full content (start=0, end=len).  Chunks are aligned to
    sentence boundaries where possible; if a single sentence is longer
    than `_QW5_CHUNK_MAX_SIZE` we fall back to a hard split.

    Topic detection: when the keyword similarity between adjacent sentences
    drops below `_QW5_TOPIC_SIMILARITY_THRESHOLD`, a topic boundary is
    detected and a new chunk is started regardless of size.

    Overlap is taken from the tail of the previous chunk so a query
    that lands near a boundary still matches the next chunk too.
    """
    if not content:
        return [(0, 0, "")]
    if len(content) <= _QW5_CHUNK_THRESHOLD:
        return [(0, len(content), content)]
    chunks = []
    spans = []
    pos = 0
    for m in _QW5_SENT_BOUNDARY.finditer(content):
        s_end = m.start()
        if s_end > pos:
            spans.append((pos, s_end))
        pos = m.end()
    if pos < len(content):
        spans.append((pos, len(content)))
    # M28 fix: removed dead fallback — for non-empty content, at least one
    # span is always appended (pos=0, len(content)>0 → condition is true).
    cursor = 0
    while cursor < len(spans):
        cur_start, cur_end = spans[cursor]
        if cur_end - cur_start > _QW5_CHUNK_MAX_SIZE:
            i = cur_start
            while i < cur_end:
                end = min(i + _QW5_CHUNK_MAX_SIZE, cur_end)
                chunks.append((i, end, content[i:end]))
                i = end
            cursor += 1
            continue
        chunk_start = cur_start
        chunk_end = cur_end
        i = cursor + 1
        while i < len(spans):
            s, e = spans[i]
            if e - chunk_start > _QW5_CHUNK_MAX_SIZE:
                break
            last_sentence_text = (
                content[chunk_end - 100 : chunk_end]
                if chunk_end > 100
                else content[chunk_start:chunk_end]
            )
            next_sentence_text = content[s:e]
            if _qw5_is_topic_boundary(last_sentence_text, next_sentence_text):
                break
            if e - chunk_start <= _QW5_CHUNK_TARGET_SIZE:
                chunk_end = e
                i += 1
            else:
                break
        chunks.append((chunk_start, chunk_end, content[chunk_start:chunk_end]))
        consumed = chunk_end - chunk_start
        if consumed <= _QW5_CHUNK_OVERLAP:
            cursor = i
        else:
            backtrack_chars = min(_QW5_CHUNK_OVERLAP, consumed - 1)
            target = chunk_end - backtrack_chars
            j = cursor
            while j < i and spans[j][0] < target:
                j += 1
            cursor = max(j, cursor + 1)
    return chunks


def _qw5_ensure_schema(db) -> None:
    """QW5: create the chunks schema if it doesn't exist. Idempotent."""
    try:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_chunks'"
        ).fetchone()
        if not exists:
            db.executescript(_QW5_CHUNKS_SCHEMA_SQL)
            db.executescript(_QW5_CHUNKS_TRIGGERS_SQL)
    except Exception as e:
        logger.warning("Failed to create memory_chunks schema: %s", e)


def _qw5_index_chunks_for(db, parent_id: str, content: str) -> int:
    """QW5: replace the chunks for a parent with fresh ones derived
    from the given content. Returns the number of chunks written.
    """
    _qw5_ensure_schema(db)
    db.execute("DELETE FROM memory_chunks WHERE parent_id = ?", (parent_id,))
    chunks = _qw5_chunk_content(content)
    for idx, (s, e, text) in enumerate(chunks):
        db.execute(
            "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) VALUES (?,?,?,?,?)",
            (parent_id, idx, s, e, text),
        )
    return len(chunks)
