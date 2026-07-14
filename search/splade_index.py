"""SPLADE sparse vector indexing for the search pipeline.

Stores sparse vocabulary-weight pairs from SPLADE-v3 encoding into
the splade_tokens table.  Each memory produces a variable number of
(vocab_id, weight) pairs representing its learned sparse representation.

Indexing is idempotent: re-indexing a memory replaces its old rows.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def _ensure_splade_schema(conn: Any) -> None:
    """Create splade_tokens table if it doesn't exist (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS splade_tokens (
            id          INTEGER PRIMARY KEY,
            memory_id   TEXT NOT NULL,
            vocab_id    INTEGER NOT NULL,
            weight      REAL NOT NULL,
            created_at  REAL NOT NULL DEFAULT (unixepoch())
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_st_memory ON splade_tokens(memory_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_st_vocab ON splade_tokens(vocab_id)"
    )


def index_memory_splade(
    conn: Any,
    memory_id: str,
    content: str,
) -> int:
    """Index SPLADE sparse vector for a single memory.

    Encodes content via SPLADE-v3 and stores non-zero (vocab_id, weight)
    pairs.  Replaces any existing rows for this memory.

    Returns the number of sparse entries inserted.
    """
    from infra.splade_encoder import encode_sparse

    sparse = encode_sparse(content)
    if sparse is None:
        # Model not available — skip silently
        return 0

    # Delete old rows for this memory
    conn.execute("DELETE FROM splade_tokens WHERE memory_id = ?", (memory_id,))

    now = time.time()
    for vocab_id, weight in sparse:
        conn.execute(
            "INSERT INTO splade_tokens (memory_id, vocab_id, weight, created_at) "
            "VALUES (?, ?, ?, ?)",
            (memory_id, vocab_id, weight, now),
        )
    return len(sparse)


def index_memory_splade_batch(
    conn: Any,
    batch: list[tuple[str, str]],
) -> int:
    """Index SPLADE sparse vectors for multiple memories in one forward pass.

    Args:
        conn: Database connection.
        batch: List of (memory_id, content) tuples.

    Returns the total number of sparse entries inserted.
    """
    from infra.splade_encoder import encode_sparse_batch

    texts = [content for _, content in batch]
    sparse_results = encode_sparse_batch(texts)
    if sparse_results is None:
        return 0

    total = 0
    now = time.time()
    for (memory_id, _), sparse in zip(batch, sparse_results):
        if not sparse:
            continue
        conn.execute("DELETE FROM splade_tokens WHERE memory_id = ?", (memory_id,))
        for vocab_id, weight in sparse:
            conn.execute(
                "INSERT INTO splade_tokens (memory_id, vocab_id, weight, created_at) "
                "VALUES (?, ?, ?, ?)",
                (memory_id, vocab_id, weight, now),
            )
            total += 1
    return total


def get_memory_sparse_count(conn: Any, memory_id: str) -> int:
    """Return the number of SPLADE entries for a memory."""
    row = conn.execute(
        "SELECT COUNT(*) FROM splade_tokens WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    return row[0] if row else 0


def delete_memory_splade(conn: Any, memory_id: str) -> int:
    """Delete all SPLADE entries for a memory. Returns rows deleted."""
    cur = conn.execute(
        "DELETE FROM splade_tokens WHERE memory_id = ?", (memory_id,)
    )
    return cur.rowcount


def get_indexed_memory_ids(conn: Any, limit: int = 1000) -> list[str]:
    """Return memory_ids that have SPLADE vectors indexed."""
    rows = conn.execute(
        "SELECT DISTINCT memory_id FROM splade_tokens LIMIT ?", (limit,)
    ).fetchall()
    return [r[0] for r in rows]


def splade_search(
    conn: Any,
    query_sparse: list[tuple[int, float]],
    top_k: int = 100,
) -> list[tuple[str, float]]:
    """Search for memories using sparse dot product with query vector.

    Args:
        conn: Database connection with splade_tokens populated.
        query_sparse: List of (vocab_id, weight) pairs from query encoding.
        top_k: Maximum results to return.

    Returns:
        List of (memory_id, score) tuples sorted by descending score.
    """
    if not query_sparse:
        return []

    # Build WHERE clause for query vocab_ids
    vocab_ids = [vid for vid, _ in query_sparse]
    weight_map = {vid: w for vid, w in query_sparse}

    ph = ",".join("?" * len(vocab_ids))
    rows = conn.execute(
        f"SELECT memory_id, vocab_id, weight FROM splade_tokens WHERE vocab_id IN ({ph})",
        vocab_ids,
    ).fetchall()

    # Compute dot product per memory
    scores: dict[str, float] = {}
    for mid, vid, weight in rows:
        if mid not in scores:
            scores[mid] = 0.0
        scores[mid] += weight * weight_map.get(vid, 0.0)

    # Sort by score descending
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked[:top_k]
