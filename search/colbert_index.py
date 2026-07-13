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
    chunk_size: int = 600,
    chunk_overlap: int = 81,
) -> int:
    """Index ColBERT token embeddings for a single memory.

    Splits content into chunks using QW5-style splitting, encodes each
    chunk via ColBERT-v2, and stores per-token vectors.

    Returns the number of token rows inserted.
    """
    from infra.colbert_encoder import encode_tokens
    from search.chunk_index import _qw5_chunk_content

    tokens_result = encode_tokens("")
    if tokens_result is None:
        # Model not available — skip silently
        return 0

    chunks = _qw5_chunk_content(content, chunk_size, chunk_overlap)
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
    return cur.rowcount


def get_indexed_memory_ids(conn: Any, limit: int = 1000) -> list[str]:
    """Return memory_ids that have ColBERT tokens indexed."""
    rows = conn.execute(
        "SELECT DISTINCT memory_id FROM colbert_tokens LIMIT ?", (limit,)
    ).fetchall()
    return [r[0] for r in rows]
