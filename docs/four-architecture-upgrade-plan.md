# Four Architecture Upgrades — Sprint Plan

**Session**: 2026-07-03  
**Status**: Sprint 0 (Plumbing) done → Starting Sprint 1 (Fact/Belief Separation)  
**Worktree**: `/Users/arka/.config/agentic-memory-wt` on `feat/four-architecture-upgrade`

## Sprint Overview

### Sprint 0 ✅ (DONE — `c51151ca`)
- Extended SaveRequest with `epistemic_source`, `belief_status`
- Migration 025 added columns to kg_facts: belief_status, epistemic_source, asserting_agent_id, evidence_chain, embedding
- Tagged auto_save/hook/cron callers with appropriate epistemic_source
- Fixed FK inconsistency in ensure_facts_schema

### Sprint 1 🔨 (CURRENT — Fact/Belief Separation)
- Create belief/ subpackage: belief_schema, belief_lifecycle
- Migration 026: belief_assertions table
- Update SaveRequest: asserting_agent_id, evidence_chain, fact_type
- Thread through deferred background path
- Update ensure_facts_schema for belief columns
- Feature flags in memory.toml
- fact_type taxonomy on fact extraction
- Evidence chain staleness handler
- Vector search on facts (hybrid mode)
- Belief-aware search scoring/filtering
- Behavioral tests

### Sprint 2 (Agent Self-Editing)
- memory_note action="patch" with patch_history
- memory_review_beliefs MCP tool
- Revert supersession
- Rationale capture on corrections
- memory_revision_log table

### Sprint 3 (Knowledge Compilation)
- reasoning/compile.py module
- Cross-memory entailment chains
- Declarative knowledge compilation → concepts/ corpus
- Concept-driven search reranking

### Sprint 4 (Graph Analytics Upgrade)
- Community detection (Louvain)
- Centrality in search scoring
- Graph quality metrics (memory_graph_insights)
- Temporal graph snapshots
- Betweenness centrality

## Key Design Decisions
1. Background worker with task queue for concept compilation
2. Louvain for community detection (cutting-edge, pure Python)
3. Brandes for betweenness (O(V·E), benchmarked)
4. Additive segments for patch API (additions + deletions lists)
5. Agent-driven review cadence
