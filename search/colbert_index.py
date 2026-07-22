"""ColBERT token indexing for the search pipeline.

Stores per-token embeddings from ColBERT-v2 into the colbert_tokens table.
Reuses the QW5 chunker (search/chunk_index.py) to split documents before
encoding, so each chunk produces its own set of token vectors.

Indexing is idempotent: re-indexing a memory replaces its old rows.
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Any

logger = logging.getLogger(__name__)


def _ensure_colbert_schema(conn: Any) -> None:
    """Create colbert_tokens table if it doesn't exist (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS colbert_tokens (
            id          INTEGER PRIMARY KEY,
            memory_id   TEXT NOT NULL,
            chunk_id    INTEGER NOT NULL DEFAULT 0,
            position    INTEGER NOT NULL DEFAULT 0,
            token_text  TEXT NOT NULL DEFAULT '',
            vec         BLOB NOT NULL,
            created_at  REAL NOT NULL DEFAULT (unixepoch())
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_memory ON colbert_tokens(memory_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_chunk ON colbert_tokens(memory_id, chunk_id)"
    )


def _vec_to_blob(vec: list[float]) -> bytes:
    """Pack a float vector into a compact BLOB (float32 little-endian)."""
    return struct.pack(f"<{len(vec)}f", *vec)


def index_memory_colbert(
    conn: Any,
    memory_id: str,
    content: str,
) -> int:
    """Index ColBERT token embeddings for a single memory.

    Splits content into chunks using QW5-style splitting, encodes each
    chunk via ColBERT-v2, and stores per-token vectors.

    Returns the number of token rows inserted.
    """
    from infra.colbert_encoder import encode_tokens, _get_colbert_model
    from search.chunk_index import _qw5_chunk_content

    _cm, _ct, _cp = _get_colbert_model()
    if _cm is None:
        return 0

    chunk_tuples = _qw5_chunk_content(content)
    chunks: list[str] = []
    for c in chunk_tuples:
        if isinstance(c, tuple) and len(c) >= 3:
            chunks.append(str(c[2]))
        else:
            chunks.append(str(c))
    if not chunks:
        chunks = [content]

    # Delete old rows for this memory
    conn.execute("DELETE FROM colbert_tokens WHERE memory_id = ?", (memory_id,))

    total = 0
    now = time.time()
    for chunk_idx, chunk_text in enumerate(chunks):
        token_list = encode_tokens(chunk_text)
        if not token_list:
            continue
        for pos, (tok_text, vec) in enumerate(token_list):
            blob = _vec_to_blob(vec)
            conn.execute(
                "INSERT INTO colbert_tokens (memory_id, chunk_id, position, token_text, vec, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (memory_id, chunk_idx, pos, tok_text, blob, now),
            )
            total += 1
    return total


def index_memory_colbert_batch(
    conn: Any,
    batch: list[tuple[str, str]],
) -> int:
    """Index ColBERT token embeddings for multiple memories in one forward pass.

    Args:
        conn: Database connection.
        batch: List of (memory_id, content) tuples.

    Returns the total number of token rows inserted.
    """
    from infra.colbert_encoder import encode_tokens_batch, _get_colbert_model
    from search.chunk_index import _qw5_chunk_content

    _cm, _ct, _cp = _get_colbert_model()
    if _cm is None:
        return 0

    total = 0
    now = time.time()

    all_chunks: list[tuple[str, int, str]] = []  # (memory_id, chunk_idx, text)

    for memory_id, content in batch:
        chunk_tuples = _qw5_chunk_content(content)
        chunks: list[str] = []
        for c in chunk_tuples:
            if isinstance(c, tuple) and len(c) >= 3:
                chunks.append(str(c[2]))
            else:
                chunks.append(str(c))
        if not chunks:
            chunks = [content]
        for chunk_idx, chunk_text in enumerate(chunks):
            all_chunks.append((memory_id, chunk_idx, chunk_text))

    for i in range(0, len(all_chunks), 32):
        chunk_batch = all_chunks[i:i+32]
        texts = [c[2] for c in chunk_batch]
        encoded = encode_tokens_batch(texts)
        if encoded is None:
            continue
        for j, token_list in enumerate(encoded):
            if not token_list:
                continue
            mid, cidx = chunk_batch[j][0], chunk_batch[j][1]
            if j == 0 or mid != chunk_batch[j-1][0]:
                conn.execute("DELETE FROM colbert_tokens WHERE memory_id = ?", (mid,))
            for pos, (tok_text, vec) in enumerate(token_list):
                blob = _vec_to_blob(vec)
                conn.execute(
                    "INSERT INTO colbert_tokens (memory_id, chunk_id, position, token_text, vec, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mid, cidx, pos, tok_text, blob, now),
                )
                total += 1

    return total


def get_memory_token_count(conn: Any, memory_id: str) -> int:
    """Return the number of ColBERT token rows for a memory."""
    row = conn.execute(
        "SELECT COUNT(*) FROM colbert_tokens WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    return row[0] if row else 0


def delete_memory_colbert(conn: Any, memory_id: str) -> int:
    """Delete all ColBERT tokens for a memory. Returns rows deleted."""
    cur = conn.execute(
        "DELETE FROM colbert_tokens WHERE memory_id = ?", (memory_id,)
    )
    return int(cur.rowcount)


def get_indexed_memory_ids(conn: Any, limit: int = 1000) -> list[str]:
    """Return memory_ids that have ColBERT tokens indexed."""
    rows = conn.execute(
        "SELECT DISTINCT memory_id FROM colbert_tokens LIMIT ?", (limit,)
    ).fetchall()
    return [r[0] for r in rows]
