"""Backfill pipeline subpackage.

Extracted from the original monolithic backfill_all.py (2026-06-20)
as part of the god-module decomposition. The full backfill_all.py
re-exports the symbols here for backward compatibility — existing
callers that do ``from backfill_all import _backfill_fts`` keep
working unchanged.
"""

from backfill.index_backfills import (  # noqa: F401
    _backfill_memories_from_markdown,
    _backfill_fts,
    _backfill_embeddings,
    _backfill_chunks,
    _backfill_chunks_fts,
    _backfill_backlinks,
    _backfill_vec_index_raw,
    _backfill_crdt_vectors,
    _backfill_tiers,
)
from backfill.kg_backfills import (  # noqa: F401
    _is_stopword,
    _is_valid_entity,
    _backfill_kg_facts,
    _backfill_kg_graph,
    _ENTITY_STOPWORDS,
)

from backfill.orchestrator import (  # noqa: F401
    health_check,
    backfill_incremental,
    backfill_full,
    auto_backfill,
    backfill_all,
)

__all__ = [
    # index_backfills
    "_backfill_memories_from_markdown",
    "_backfill_fts",
    "_backfill_embeddings",
    "_backfill_chunks",
    "_backfill_chunks_fts",
    "_backfill_backlinks",
    "_backfill_vec_index_raw",
    "_backfill_crdt_vectors",
    "_backfill_tiers",
    # kg_backfills
    "_is_stopword",
    "_is_valid_entity",
    "_backfill_kg_facts",
    "_backfill_kg_graph",
    "_ENTITY_STOPWORDS",
    # orchestrator
    "health_check",
    "backfill_incremental",
    "backfill_full",
    "auto_backfill",
    "backfill_all",
]
