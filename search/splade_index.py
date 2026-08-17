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
    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(iterable, *args, **kwargs):
            return iterable

    total = 0
    now = time.time()
    batch_size = 32

    for i in tqdm(range(0, len(batch), batch_size), desc="SPLADE Sparse Vectors", disable=len(batch) < 50):
        sub_batch = batch[i:i + batch_size]
        texts = [content for _, content in sub_batch]
        sparse_results = encode_sparse_batch(texts)
        if sparse_results is None:
            continue
        for (memory_id, _), sparse in zip(sub_batch, sparse_results):
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
    return int(cur.rowcount)


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
    """Search for memories using SPLADE maxsim scoring.

    For each query token, finds the maximum-weight matching document
    token, then sums those maxima.  This is more accurate than dot
    product for sparse retrieval — a document only needs to match the
    query tokens it's best at.

    Args:
        conn: Database connection with splade_tokens populated.
        query_sparse: List of (vocab_id, weight) pairs from query encoding.
        top_k: Maximum results to return.

    Returns:
        List of (memory_id, score) tuples sorted by descending score.
    """
    if not query_sparse:
        return []

    vocab_ids = [vid for vid, _ in query_sparse]
    weight_map = {vid: w for vid, w in query_sparse}

    ph = ",".join("?" * len(vocab_ids))
    rows = conn.execute(
        f"SELECT memory_id, vocab_id, weight FROM splade_tokens WHERE vocab_id IN ({ph})",
        vocab_ids,
    ).fetchall()

    # Maxsim: for each (memory_id, query_vocab_id), track max doc weight
    max_weights: dict[str, dict[int, float]] = {}
    for mid, vid, doc_weight in rows:
        if mid not in max_weights:
            max_weights[mid] = {}
        qw = weight_map.get(vid, 0.0)
        # Maxsim = max over doc tokens of (query_weight * doc_weight)
        combined = qw * doc_weight
        if vid not in max_weights[mid] or combined > max_weights[mid][vid]:
            max_weights[mid][vid] = combined

    # Sum max weights per memory
    scores: dict[str, float] = {}
    for mid, vid_scores in max_weights.items():
        scores[mid] = sum(vid_scores.values())

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked[:top_k]
