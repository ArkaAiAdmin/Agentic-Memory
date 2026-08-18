"""Canonical 14-Phase Search Pipeline Registry.

Provides structured phase definitions and module exports for the 14-phase search orchestrator:

  - Phase 1: Query Parsing & Intent Normalization (search/query_parser.py)
  - Phase 2: Exact & FTS5 Keyword Candidate Retrieval (search/phases/retrieve.py)
  - Phase 3: Dense Vector Embedding Candidate Retrieval (search/phases/retrieve.py)
  - Phase 4: Sparse SPLADE Expansion (search/splade_index.py)
  - Phase 5: ColBERT MaxSim Token-Level Alignment (search/colbert_rerank.py)
  - Phase 6: Recency & Temporal Assertion Scoping (search/orchestrator.py)
  - Phase 7: Reciprocal Rank Fusion (RRF Hybrid Fusion) (search/phases/fusion.py)
  - Phase 8: Envelope Filtering (Category, Tag, Tenant) (search/phases/envelope.py)
  - Phase 9: Concept & Entity Centrality KG Boost (search/phases/kg_traversal.py)
  - Phase 10: Multi-Hop Graph-RAG Traversal (1, 2, 3 Hops) (search/phases/kg_traversal.py)
  - Phase 11: Cross-Encoder Relevance Reranking (search/rerankers.py)
  - Phase 12: Contradiction Engine & Fact Supersession (search/phases/contradiction_engine.py)
  - Phase 13: Post-Processing Quality Gates & Safety Demotion (search/phases/postprocess.py)
  - Phase 14: Answer Synthesis & Deterministic Solvers (search/synthesis.py,
              search/phases/math_aggregator.py,
              search/phases/temporal_delta_solver.py,
              search/phases/attribute_extractor.py)
"""

from __future__ import annotations

from search.phases.attribute_extractor import extract_entity_attribute
from search.phases.contradiction_engine import resolve_candidate_contradictions
from search.phases.math_aggregator import extract_and_aggregate_quantities
from search.phases.sequence_solver import solve_sequence_order
from search.phases.temporal_delta_solver import calculate_temporal_delta

__all__ = [
    "resolve_candidate_contradictions",
    "extract_and_aggregate_quantities",
    "calculate_temporal_delta",
    "extract_entity_attribute",
    "solve_sequence_order",
]
