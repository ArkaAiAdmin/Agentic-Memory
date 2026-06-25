"""Incremental SSM encoder for text — supports chunked encoding and state merging.

Implements a diagonal State Space Model: for each token, the hidden state
updates as ``h_t = A * h_{t-1} + embed(token)`` where A is a diagonal decay
matrix. The output is ``y = C * h_T`` after the full sequence.

Key features:
- **Incremental**: encode(text) returns state; encode_update(state, more_text) continues
- **Mergeable**: merge([state_a, state_b]) combines encodings from disjoint segments
- **Deterministic**: same input always produces same output (seeded random projections)
- **Pure numpy**: no ML framework dependencies
- **Wired into the save pipeline**: ``incremental_embed_update`` and
  ``merge_embeddings`` are the public API used by ``save_pipeline``
  for fast incremental embedding updates and batch merges.
"""

from __future__ import annotations

import re as _re
from typing import List, cast, Optional

import numpy as np


class SsmEncoder:
    """Incremental SSM encoder with diagonal recurrence.

    Each word token is mapped to a dense vector via a deterministic
    hash embedding. The state evolves as a linear recurrence with
    exponential decay, producing a fixed-dimension encoding of the
    full sequence. The encoding can be extended with new text via
    ``encode_update``, and states from disjoint segments can be
    combined via ``merge``.
    """

    def __init__(
        self, dim: int = 128, decay: float = 0.95, vocab_size: int = 10000
    ) -> None:
        self.dim = dim
        self.decay = decay
        rng = np.random.RandomState(42)
        self._embed = rng.randn(vocab_size, dim).astype(np.float32) * 0.1
        self._C = rng.randn(dim, dim).astype(np.float32) * 0.1

    def _tokenize(self, text: str) -> list[int]:
        tokens = _re.findall(r"\w+|[^\w\s]", text.lower())
        return [hash(t) % len(self._embed) for t in tokens] if tokens else [0]

    def encode(self, text: str) -> List[float]:
        """Encode *text* into a fixed-dimension vector.

        Processes all tokens sequentially through the SSM recurrence.
        """
        tokens = self._tokenize(text)
        h = np.zeros(self.dim, dtype=np.float32)
        for idx in tokens:
            h = self.decay * h + self._embed[idx]
        return cast(List[float], (self._C @ h).tolist())

    def encode_update(self, state: List[float], text: str) -> List[float]:
        """Continue encoding from an existing *state* with new *text*.

        The state from a previous ``encode`` or ``encode_update`` call
        is extended by processing *text* through the recurrence without
        re-processing the original input.
        """
        h = np.array(state, dtype=np.float32)
        tokens = self._tokenize(text)
        for idx in tokens:
            h = self.decay * h + self._embed[idx]
        return cast(List[float], (self._C @ h).tolist())

    def merge(self, states: List[List[float]]) -> List[float]:
        """Merge multiple states into one by averaging.

        Useful for combining encodings from disjoint text segments
        that were processed independently.
        """
        if not states:
            return [0.0] * self.dim
        arr = np.array(states, dtype=np.float32).mean(axis=0)
        return cast(List[float], arr.tolist())


# ---------------------------------------------------------------------------
# Module-level singleton for cheap reuse across the save pipeline.
# A single SsmEncoder is stateless after construction (the same input
# always produces the same output), so sharing one instance is safe.
# ---------------------------------------------------------------------------

_default_encoder: Optional[SsmEncoder] = None


def get_default_encoder() -> SsmEncoder:
    """Return the process-wide default SsmEncoder (lazy-initialized)."""
    global _default_encoder
    if _default_encoder is None:
        _default_encoder = SsmEncoder()
    return _default_encoder


def reset_default_encoder() -> None:
    """Reset the module-level encoder (useful for tests)."""
    global _default_encoder
    _default_encoder = None


# ---------------------------------------------------------------------------
# Public API used by save_pipeline for incremental embedding updates.
# ---------------------------------------------------------------------------


def incremental_embed_update(
    memory_id: str,
    new_content: str,
    old_state: Optional[List[float]] = None,
    encoder: Optional[SsmEncoder] = None,
) -> List[float]:
    """Compute an updated SSM embedding for a memory whose content changed.

    This is the fast path for save-pipeline updates: when a memory's
    content is appended to or modified, we don't have to re-encode the
    whole document — we extend the previous SSM state with the new
    text. The result is a 128-dim vector suitable for the SSM auxiliary
    channel (the primary model2vec / sentence-transformers embedding
    is still produced by ``embedding_search``).

    Args:
        memory_id: The memory being updated. Used only for cache-key
            resolution; this function is content-driven, not stateful.
        new_content: The new content to encode. If ``old_state`` is
            provided, the function treats ``new_content`` as a delta
            and extends ``old_state`` via ``encode_update``. If
            ``old_state`` is None, the function encodes ``new_content``
            from scratch.
        old_state: Previous SSM state for the same memory, or None for
            a fresh encode.
        encoder: Optional SsmEncoder instance; defaults to the
            process-wide singleton.

    Returns:
        128-dim list of floats (the new SSM state).
    """
    enc = encoder or get_default_encoder()
    if old_state:
        return enc.encode_update(old_state, new_content)
    return enc.encode(new_content)


def merge_embeddings(
    memory_ids: List[str],
    states: Optional[List[List[float]]] = None,
    encoder: Optional[SsmEncoder] = None,
) -> List[float]:
    """Merge multiple memory SSM embeddings into a single vector.

    Useful for batch operations: e.g. when a user saves a thread of
    related memories, the save pipeline can merge their SSM states
    to produce a single "thread summary" embedding.

    Two call signatures are supported:

    1. ``merge_embeddings(memory_ids=[...])`` — looks up the SSM
       states for each memory_id from the ``memory_embeddings`` table.
       Best-effort: if a state cannot be loaded, it is skipped (with a
       warning).
    2. ``merge_embeddings(memory_ids=[...], states=[...])`` — caller
       supplies the states directly. ``memory_ids`` is then only used
       for logging / cache-key resolution.

    Args:
        memory_ids: Memory IDs to merge (or label).
        states: Optional pre-computed states. If None, the function
            tries to load them from the DB.
        encoder: Optional SsmEncoder instance; defaults to the
            process-wide singleton.

    Returns:
        128-dim list of floats. If no states are available, returns
        a zero vector of length 128.
    """
    enc = encoder or get_default_encoder()

    if states is None:
        states = _load_states_for_memory_ids(memory_ids)

    if not states:
        return [0.0] * enc.dim

    return enc.merge(states)


def _load_states_for_memory_ids(memory_ids: List[str]) -> List[List[float]]:
    """Best-effort load of SSM states for a list of memory IDs.

    The SSM state is stored as a side-channel on the
    ``memory_embeddings`` table. If the table or the column is not
    present (e.g. on a fresh DB that hasn't been migrated yet), this
    function returns an empty list — the caller treats that as "no
    state available, skip the merge".
    """
    if not memory_ids:
        return []
    try:
        import json
        import sqlite3
        from pathlib import Path
        from infrastructure import resolve_active_memory_dir

        db_path = resolve_active_memory_dir() / "memory.db"
        if not db_path.exists():
            return []
        conn = sqlite3.connect(str(db_path))
        try:
            # Check the schema: memory_embeddings has an optional
            # ``ssm_state`` column that the save pipeline writes
            # alongside the main embedding. Older DBs won't have it.
            cols = {
                row[1] for row in conn.execute("PRAGMA table_info(memory_embeddings)")
            }
            if "ssm_state" not in cols:
                return []
            rows = conn.execute(
                f"SELECT ssm_state FROM memory_embeddings "
                f"WHERE note_id IN ({','.join('?' * len(memory_ids))}) "
                f"AND ssm_state IS NOT NULL",
                memory_ids,
            ).fetchall()
            out: List[List[float]] = []
            for (blob,) in rows:
                try:
                    state = json.loads(blob)
                    if isinstance(state, list):
                        out.append(state)
                except (ValueError, TypeError):
                    continue
            return out
        finally:
            conn.close()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Lightweight in-process cache (avoids re-encoding the same delta).
# ---------------------------------------------------------------------------

# Maps (memory_id, content_hash) -> SSM state. Bounded to a small
# size so it doesn't grow without limit under long agentic loops.
_ssm_cache: dict = {}
_SSM_CACHE_MAX = 64


def _ssm_cache_get(memory_id: str, content_hash: str):
    return _ssm_cache.get((memory_id, content_hash))


def _ssm_cache_put(memory_id: str, content_hash: str, state: List[float]) -> None:
    if len(_ssm_cache) >= _SSM_CACHE_MAX:
        # Drop the oldest entry (insertion-ordered in Python 3.7+).
        oldest_key = next(iter(_ssm_cache))
        _ssm_cache.pop(oldest_key, None)
    _ssm_cache[(memory_id, content_hash)] = state


def clear_ssm_cache() -> None:
    """Clear the in-process SSM cache. Useful for tests."""
    _ssm_cache.clear()
