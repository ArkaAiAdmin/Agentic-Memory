# Four-Architecture-Upgrade: Implementation Plan

## Context
This plan integrates four advanced memory architectures into agentic-memory:
1. **Fact/Belief Separation** (Hindsight's belief_core pattern)
2. **Agent Self-Editing** (Letta's agent-manages-own-memory model)
3. **Knowledge Compilation** (Pinecone Nexus - reasoning upstream, not at query time)
4. **Graph Analytics** (Zep/Hindsight - community detection, rich traversal, influence metrics)

## Branch & Worktree
- **Branch:** `feat/four-architecture-upgrade`
- **Worktree:** `/Users/arka/.config/agentic-memory-wt`
- **Base:** `f14ca1db` (Temporal KG Sprints 2-5)

## Decisions
1. **Background worker with task queue** for all compilation work. NOT on-save blocking.
2. **Louvain** for community detection (cutting edge standard, pure-Python impl, no new dep).
3. **Brandes algorithm** for betweenness centrality (pure-Python, O(V·E), benchmark at graph sizes 1k/10k/50k edges).
4. **Additive patch API** for memory amend (`additions` + `deletions` lists + `rationale`).
5. **Agent-driven review cadence** — `memory_review_beliefs` is a CORE tool called by the agent. Not cron-driven.

## Sprint Order
```
Sprint 0 (plumbing) ──┬──▶ Sprint 1 (belief layer) ──▶ Sprint 3 (compilation)
Sprint 4 (graph) ─────┘

Sprint 2 (self-editing) ──▶ depends on Sprint 1
```

### Sprint 0: Save-Pipeline Plumbing
- Extend `SaveRequest` with `epistemic_source`, `belief_status`, `asserting_agent_id`, `evidence_chain`
- Add `belief_status` + `epistemic_source` to `kg_facts` (migration 025)
- Add `embedding BLOB` column to `kg_facts`
- Tag auto_save/hook/import/cron writes with source
- Fix `fact_schema.py` ON DELETE SET NULL inconsistency

### Sprint 1: Fact/Belief Separation
- New `belief_assertions` table (belief_status, confidence, epistemic_source, evidence_chain, certainty_tier, rationale, last_reviewed_at, review_count)
- `kg_facts.fact_type` taxonomy: observation | agent_inference | external_stated | hypothesis | derived
- Confidence split: `extraction_confidence` (existing, renamed) vs `belief_confidence` (new, in belief_assertions)
- Vector search on facts (hybrid FTS5 + embedding)
- Evidence chain staleness background task

### Sprint 2: Agent Self-Editing
- `memory_note(action="patch")` — additive segments + rationale, patch_history in metadata
- `memory_review_beliefs` CORE tool (agent-driven review)
- `memory_note(action="revert_supersede")` + `memory_revision_log` table
- Rationale required on supersede/delete/amend
- `memory_curate_autosave` tool

### Sprint 3: Knowledge Compilation
- `reasoning/compile.py` — background task: `handle_concept_compilation`, `handle_entailment_chains`
- `entailment_chains` table (transitive, conjunctive, analogical)
- `concepts/` corpus (synthesized markdown per topic)
- Procedural-to-declarative skill enrichment
- Concept-driven search reranking

### Sprint 4: Graph Analytics
- `kg/graph_communities.py` — connected components + Louvain modularity
- Brandes betweenness centrality in `kg/graph_analytics.py`
- `centrality_boost` in `search/scoring.py`
- `memory_graph_insights` MCP tool
- Community-aware graph-RAG expansion
- `graph_snapshots` table + `memory_graph_evolution` tool
- Benchmark betweenness at 1k/10k/50k edges

### Sprint 5: Integration & Behavioral Testing
- Behavioral eval files per sprint
- Performance budgets enforced
- Feature flags gating each sprint
- Fresh merge to main per sprint with passing tests

## Performance Budgets
- memory_save: < 200ms
- memory_search facts hybrid: < 300ms
- concept_compilation per batch: < 5s
- graph_communities 50k edges: < 10s
- memory_graph_insights: < 2s
- memory_review_beliefs: < 500ms

## Key Files
- `save_pipeline.py` — SaveRequest extension
- `save/post_save_hooks.py` — epistemic_source tagging
- `fact/fact_schema.py` — schema fix
- `belief/belief_schema.py` — NEW (belief_assertions table)
- `reasoning/compile.py` — NEW (concept + entailment compilation)
- `kg/graph_communities.py` — NEW (Louvain)
- `kg/graph_analytics.py` — extended (Brandes betweenness)
- `search/scoring.py` — centrality_boost
- `mcp_maintenance.py` — new tools
- `mcp_kg.py` — graph_communities, graph_insights

## Acceptance Criteria (per sprint)
- All tests passing (3879 baseline + new tests)
- Zero failures
- Per-sprint merge to main
- Behavioral tests verify agent-facing correctness, not just unit tests
