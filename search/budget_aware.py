"""Budget-aware search cascade for the search pipeline.

Phase 7: Tracks which pipeline stages ran and enforces a compute budget.
When the budget is tight, expensive stages (ColBERT, answer rerank, deep CE)
are skipped to stay under the latency target.

Stages and their approximate costs:
  - FTS retrieval:      ~5 ms
  - Semantic retrieval: ~50 ms (embedding model)
  - SPLADE retrieval:   ~30 ms (sparse encoding)
  - Chunk FTS:          ~10 ms
  - Weak CE:            ~20 ms
  - Chunk CE:           ~100 ms
  - Deep CE:            ~500 ms
  - ColBERT rerank:     ~100 ms
  - Answer rerank:      ~50 ms (with cache hit)
  - Enrichment:         ~5 ms

Budget tiers:
  - < 50 ms:  FTS only (skip all reranking)
  - < 100 ms: FTS + weak CE (skip chunk CE, ColBERT, answer rerank)
  - < 300 ms: FTS + CE + ColBERT (skip answer rerank, deep CE)
  - ≥ 300 ms: Full pipeline
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Default budget thresholds (ms) — elapsed time above which a stage is skipped.
_BUDGET_THRESHOLD_COLBERT = 300
_BUDGET_THRESHOLD_ANSWER_RERANK = 200
_BUDGET_THRESHOLD_CHUNK_CE = 100
_BUDGET_THRESHOLD_CE = 50


@dataclass
class SearchBudget:
    """Tracks compute budget for a single search call."""

    budget_ms: float
    start_time: float = field(default_factory=time.time)
    stages_run: list[str] = field(default_factory=list)
    stages_skipped: list[str] = field(default_factory=list)

    @property
    def elapsed_ms(self) -> float:
        return (time.time() - self.start_time) * 1000

    @property
    def remaining_ms(self) -> float:
        return max(0, self.budget_ms - self.elapsed_ms)

    def should_run(self, stage: str, estimated_cost_ms: float) -> bool:
        """Check if a stage should run based on remaining budget."""
        if self.budget_ms <= 0:
            # No budget constraint
            self.stages_run.append(stage)
            return True

        if self.remaining_ms < estimated_cost_ms:
            self.stages_skipped.append(stage)
            logger.debug(
                "budget: skip %s (remaining %.0f ms < estimated %.0f ms)",
                stage, self.remaining_ms, estimated_cost_ms,
            )
            return False

        self.stages_run.append(stage)
        return True

    def to_dict(self) -> dict:
        """Return budget status as a dict for the result envelope."""
        return {
            "budget_ms": self.budget_ms,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "remaining_ms": round(self.remaining_ms, 1),
            "stages_run": self.stages_run,
            "stages_skipped": self.stages_skipped,
        }


def get_search_budget() -> SearchBudget:
    """Create a SearchBudget from the typed ``SearchConfig``.

    Reads ``search_compute_budget_ms`` via :func:`search.config.get_search_config`,
    which resolves ``MEMORY_SEARCH_COMPUTE_BUDGET_MS`` env var, ``memory.toml``,
    and the code default (200 ms). Set to 0 for unlimited (legacy behavior).

    Returns a SearchBudget with budget_ms from config.
    """
    try:
        from search.config import get_search_config

        budget_ms = float(get_search_config().search_compute_budget_ms)
    except (ValueError, TypeError, ImportError):
        budget_ms = 200.0

    return SearchBudget(budget_ms=budget_ms)


def compute_adaptive_overfetch(
    corpus_size: int,
    base_overfetch: int = 3,
    query_type: str = "general",
) -> int:
    """Compute adaptive overfetch factor based on corpus size and query type.

    For small corpora, overfetch is unnecessary.  For large corpora,
    overfetch scales with log10(corpus_size) to maintain recall.

    Args:
        corpus_size: Number of memories in the database.
        base_overfetch: Default overfetch multiplier (default 3).
        query_type: Query type for type-specific adjustments.

    Returns:
        Overfetch multiplier (1 = no overfetch).
    """
    if corpus_size <= 0:
        return base_overfetch

    import math

    # Base scaling: log10(corpus_size), clamped to [1, 5]
    log_scale = math.log10(max(1, corpus_size))
    scaled = max(1, min(5, int(log_scale)))

    # Query-type adjustments
    type_adjustments = {
        "code": -1,      # Code queries are precise, less overfetch needed
        "temporal": 2,   # Temporal queries benefit from more candidates
        "multihop": 3,   # Multihop needs more candidates for evidence
        "factual": 2,    # Factual queries benefit from some overfetch
        "general": 0,    # No adjustment
    }
    adjustment = type_adjustments.get(query_type, 0)

    # Final overfetch: base × scaled × type adjustment, clamped to [1, 10]
    overfetch = max(1, min(10, base_overfetch + scaled + adjustment))

    return overfetch
