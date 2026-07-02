"""Belief layer — fact/belief separation for agentic-memory.

The belief layer distinguishes "what is true" (kg_facts) from
"what I believe" (belief_assertions) and the evidence that connects them.

Public API:
    ensure_beliefs_schema — idempotent schema setup
    ensure_belief_assertion — create/update a belief_assertion from a fact
    get_beliefs_for_fact — retrieve belief assertions for a given fact
    get_active_beliefs — list active beliefs with optional filters
    update_belief_status — change belief status (retract, deprecate, reinforce)
    handle_evidence_chain_staleness — background task to mark stale beliefs
    retract_dependent_beliefs — cascade retraction through evidence chains
"""

from .belief_schema import ensure_beliefs_schema
from .belief_lifecycle import (
    ensure_belief_assertion,
    get_beliefs_for_fact,
    get_active_beliefs,
    update_belief_status,
    handle_evidence_chain_staleness,
    retract_dependent_beliefs,
)

__all__ = [
    "ensure_beliefs_schema",
    "ensure_belief_assertion",
    "get_beliefs_for_fact",
    "get_active_beliefs",
    "update_belief_status",
    "handle_evidence_chain_staleness",
    "retract_dependent_beliefs",
]
