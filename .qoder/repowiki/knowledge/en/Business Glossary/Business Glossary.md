---
kind: business_term
name: Business Glossary
category: business_term
scope:
    - '**'
---

### Saga
- Definition：The saga pattern implementation in `infra/saga.py` that wraps each write across three stores (SQLite row, usearch vec_key mapping, .md file) so that if any step fails, prior steps are rolled back in reverse order. Every save goes through this saga; the read/search path does not.
- Aliases：saga rollback、saga mode

### tenant_memories
- Definition：A TEMP VIEW created on every pooled connection that filters the raw `memories` table by the current tenant's id, providing the canonical tenant-scoped read surface. All search phases should join against this view rather than `memories` directly.
- Aliases：tenant view

### LoCoMo
- Definition：The LoCoMo benchmark suite used to evaluate long-context multi-modal memory systems. Agentic-memory reports 92.2% recall@10 overall (with a weaker temporal subset at 72.92%). Used as the primary cross-system comparison metric against Mem0, Zep/Graphiti, Hindsight, Cognee.
- Aliases：LocoMo

### LongMemEval
- Definition：The LongMemEval benchmark suite measuring long-term memory retention across days/weeks. Agentic-memory reports 95.32% `recall_all@10` (all gold docs in top-k), stricter than competitors' `recall_any` variant.
- Aliases：longmemeval

### BEAM
- Definition：The BEAM benchmark suite (1M and 10M scale variants) measuring accuracy on large-corpus retrieval. Agentic-memory uses `fact_lookup` mode for these runs (skips embedding/CE/KG channels), yielding 94.12% at 1M and 87.50% at 10M.
- Aliases：beam benchmark

### CRDT sync
- Definition：The conflict-free replicated data type layer built on per-field version vectors (`crdt/crdt_merge.py`) that enables offline-first, peer-to-peer sync of markdown memories between instances. Each field carries a version vector with deterministic tiebreak for merges.
- Aliases：CRDT、version vector sync

### hybrid search
- Definition：The 14-phase read pipeline combining FTS5 BM25 keyword matching, usearch ANN vector similarity, ColBERT cross-encoder reranking, temporal decay scoring, neural forget curve, KG traversal, chunk enhancement, and RRF fusion into a single ranked result set.
- Aliases：14-phase pipeline、hybrid retrieval

### memory.toml
- Definition：The local configuration file format used by the system. Operators can place tokens here but they are world-readable by default — no warning exists when secrets are added to it. Secrets are preferred via environment variables.
- Aliases：config file、memory config
