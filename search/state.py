"""Typed pipeline state for the search orchestrator.

Phases take and return a ``PipelineState`` instance, making the dataflow
legible: you can read which phase depends on what by inspecting the
state attributes it accesses and mutates.

Historically the pipeline passed ``result_items``, ``output``,
``results_to_display``, and ``backlinks_map`` as separate mutable lists
— four positional args threaded through every postprocess call.
``PipelineState`` collapses that into a single typed object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PipelineState:
    """Mutable state threaded through search pipeline phases.

    Attributes are grouped by lifecycle:
      - **Immutable inputs**: set once at pipeline start, never mutated.
      - **Derived scalars**: computed in early phases, read later.
      - **Mutable result sets**: mutated by phases in place.
    """

    # ── Immutable inputs (set once) ──────────────────────────────
    db_path: Path
    query: str
    limit: int
    rerank: bool
    boost_pinned: bool
    recency_weight: float
    include_invalid: bool
    hybrid: bool
    deep_rerank: bool
    safety_wiring: bool
    light: bool
    as_of: float | None
    tenant_id: str
    category: str
    shared_with_me: bool

    # ── Derived scalars (set in early phases) ────────────────────
    db: Any = None
    normalized_query: str = ""
    fts_query: str = ""
    has_fitness: bool = False
    repo_filter: str = ""
    effective_rerank: bool = True
    query_id: str = ""

    # ── Mutable result sets (mutated by phases in place) ─────────
    results: list = field(default_factory=list)
    results_to_display: list = field(default_factory=list)
    result_items: list = field(default_factory=list)
    output: list[str] = field(default_factory=list)
    backlinks_map: dict = field(default_factory=dict)
    related_facts: list[dict] = field(default_factory=list)
    session_boost_ids: set = field(default_factory=set)
    ctr_weights: dict | None = None
