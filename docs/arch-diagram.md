# Agentic Memory — Complete Architecture

> Schema v21 · 85 MCP tools (15 CORE + 70 ADMIN) · ~51 SQLite tables (~31 user-visible) · ~27 cron schedule entries · 4 hooks · 102 production modules · ~56,799 LOC production + ~69,155 LOC tests

---

## 1. System Topology (Big Picture)

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                         HOST AGENT PROCESS (opencode)                               │
│                                                                                     │
│  ┌──────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐   │
│  │   PreToolUse Hook    │    │  SessionStart Hook   │    │  Auto-Save Hook      │   │
│  │ proactive-context.py │    │ session-start.py     │    │ auto_save.py         │   │
│  │ "search before tool" │    │ "cold-start briefing"│    │ "save every tool"    │   │
│  └────────┬─────────────┘    └────────┬─────────────┘    └──────────┬───────────┘   │
│           │                           │                             │               │
│           ▼                           ▼                             ▼               │
│  ┌─────────────────────────────────────────────────────────────────────────────┐    │
│  │                          MCP SERVER (memory_mcp.py)                          │    │
│  │  Thin orchestrator — delegates to 26 mcp_*.py domain modules                │    │
│  │                                                                              │    │
│  │  ┌── CORE (15 tools) ────────────────────────────────────────────────┐       │    │
│  │  │ memory_save │ search │ semantic_search │ facts_search │ graph_search│      │    │
│  │  │ recall_context │ session_start │ user_profile │ delete │ restore  │      │    │
│  │  │ check_contradictions │ scan_injection │ rebuild │ supersede       │      │    │
│  │  │ profile_access                                                    │      │    │
│  │  └───────────────────────────────────────────────────────────────────┘       │    │
│  │                                                                              │    │
│  │  ┌── ADMIN (64 tools via memory_maintenance operation="...") ─────────┐      │    │
│  │  │ tier_stats │ audit │ consolidate │ arc_stats │ review_schedule     │      │    │
│  │  │ quality_stats │ facts_stats │ profile_stats │ retention_stats      │      │    │
│  │  │ summarization │ compact │ duplicates │ merge_suggestions           │      │    │
│  │  │ backfill_all │ detect_contradictions │ check_integrity             │      │    │
│  │  │ crdt_sync │ crdt_status │ okf_export │ okf_import                 │      │    │
│  │  │ circuit_breaker_status │ temporal_contradictions │ temporal_query  │      │    │
│  │  │ ... (64 total)                                                    │      │    │
│  │  └───────────────────────────────────────────────────────────────────┘       │    │
│  └─────────────────────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────┬───────────────────────────────────────────┘
                                         │
                   ┌─────────────────────┼──────────────────────┐
                   │                     │                      │
                   ▼                     ▼                      ▼
┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│    SAVE PIPELINE     │  │   SEARCH PIPELINE    │  │     AUTO-SAVE        │
│   save_pipeline.py   │  │ search/orchestrator  │  │   auto_save.py       │
│     + save/          │  │ .py + search/        │  │   daemon process     │
│       (1,359+1,251   │  │   (4,223 LOC)        │  │   (2,469 LOC)        │
│        LOC)          │  │                      │  │                      │
│                      │  │                      │  │  ┌────────────────┐  │
│  ┌────────────────┐  │  │  ┌────────────────┐  │  │  │ .auto_save_    │  │
│  │  markdown → DB │  │  │  │ FTS5 BM25      │  │  │  │ inbox.jsonl   │  │
│  │  → FTS5         │  │  │  │ → Vector       │  │  │  │ (async queue)  │  │
│  │  → chunks       │  │  │  │ → KG facts     │  │  │  └────────────────┘  │
│  │  → embeddings   │  │  │  │ → RRF fusion   │  │  │                      │
│  │  → KG entities  │  │  │  │ → Rerank       │  │  │  batch flush 500ms  │
│  │  → KG facts     │  │  │  │ → Synthesis    │  │  │  or 50 entries      │
│  │  → backlinks    │  │  │  └────────────────┘  │  └──────────────────────┘
│  │  → tier assign  │  │  │                      │
│  └────────────────┘  │  └──────────────────────┘
└──────────────────────┘
│
├─── [Local Memory] ~/.config/agentic-memory/memory/
│    │
│    ├── memory.db           ← SQLite (v21 schema, ~51 tables)
│    ├── memory.db-wal       ← WAL journal
│    ├── <category>/<slug>.md  ← Markdown files (source of truth)
│    └── .auto_save_inbox.jsonl ← Async auto-save queue
│
├─── [Global Memory]  ~/.config/agentic-memory/memory/global/
│    └── (shared across projects, blended via RRF)
│
└─── [Sync Server]  optional, HTTP + CRDT sync
     sync_server.py :9877 (TLS/mTLS optional)
```

---

## 2. Write Path — Save Pipeline

```
                AGENT CALLS memory_save(content, category, tags...)
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                     save_pipeline.save_memory()                           │
│                     (1,359 LOC shim → save/ submodules)                   │
└───────────────────────────────────┬──────────────────────────────────────┘
                                    │
                                    ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │                           SAGA TRANSACTION                            │
 │                                                                       │
 │  ┌─────────────────────────────────────────────────────────────────┐ │
 │  │ 1. FLOCK ACQUIRE          │  File lock (prevents concurrent W)  │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 2. MARKDOWN WRITE         │  atomic_write() → .md file           │ │
 │  │                           │  Conflict-preserving: .conflict-PID  │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 3. DB UPSERT              │  INSERT/UPDATE into `memories`       │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 4. FTS5 INDEX             │  Sync trigger → memories_fts         │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 5. CHUNKING              │  Content split → memory_chunks       │ │
 │  │                           │  + memory_chunks_fts                 │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 6. EMBEDDING             │  model2vec(256d) → memory_embeddings  │ │
 │  │                           │  + memory_vec_keys (HNSW)            │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 7. KG EXTRACTION         │  Pattern NER → kg_entities            │ │
 │  │                           │  Relation extraction → kg_edges      │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 8. FACT EXTRACTION       │  Regex SPO triples → kg_facts        │ │
 │  │  (if temporal KG ON)     │  + event_time, valid_at, invalid_at   │ │
 │  │                           │  + contradiction detection           │ │
  │  │                           │  + kg_facts_fts (v20) + kg_entity_crdt/kg_edge_crdt (v21)  │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 9. BACKLINKS             │  Wiki-link resolution → backlinks     │ │
 │  │                           │  FTS semantic backlinks              │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 10. TIER ASSIGNMENT      │  Hot/Warm/Cold based on importance    │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 11. POST-SAVE HOOKS      │  7 named hooks (decomposed 2026-06-22)│ │
 │  │  ┌─────────────────────┐ │                                       │ │
 │  │  │ fitness recalc       │ │ ← recalculate relevance scores       │ │
 │  │  │ tier migration       │ │ ← promote/demote between tiers       │ │
 │  │  │ field_crdt sync     │ │ ← per-field LWWES CRDT state         │ │
 │  │  │ audit flush         │ │ ← write to memory_audit_log          │ │
 │  │  │ backlink update     │ │ ← outgoing link resolution            │ │
 │  │  │ FTS5 sync           │ │ ← ensure FTS indexes are current      │ │
 │  │  │ embedding refresh   │ │ ← re-embed if content changed         │ │
 │  │  └─────────────────────┘ │                                       │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 12. ENRICHMENT           │  FTS overlap search, contextual links │ │
 │  ├───────────────────────────┼──────────────────────────────────────┤ │
 │  │ 13. BACKGROUND TASKS     │  Enqueue → task_queue (SQLite-backed) │ │
 │  └───────────────────────────┼──────────────────────────────────────┘ │
 │                              │                                        │
 │  ON FAILURE: saga.undo_upsert()                                       │
 │  → cleanup_memory_relations() (kg_facts, kg_edges, backlinks)         │
 │  → rollback .md with safe_atomic_write                                │
 └──────────────────────────────────────────────────────────────────────┘
```

### Write Path Modules

| Module | Lines | Purpose |
|--------|-------|---------|
| `save_pipeline.py` | 1,359 | Saga orchestration, save_memory entry |
| `save/crdt_helpers.py` | — | CRDT snapshot/version extraction |
| `save/indexers.py` | — | FTS5, embedding, chunk, KG, fact, retention index writes |
| `save/backlinks.py` | — | Auto-backlink computation (FTS + semantic) |
| `save/post_save_hooks.py` | — | 7 hooks: fitness, tier, CRDT, audit, backlinks, FTS5, embeddings |
| `save/saga.py` | — | Transactional rollback with conflict detection |
| `save/cleanup.py` | — | KG/fact/backlink dead-row cleanup on rollback |

---

## 3. Read Path — Search Pipeline

```
                AGENT CALLS memory_search("query")
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────────┐
│              search/orchestrator.py — search_memories()                   │
│              (1,811 LOC, 28 helpers, decomposed from 551→244 lines)      │
└───────────────────────────────────┬──────────────────────────────────────┘
                                    │
         ┌──────────────────────────┼───────────────────────────┐
         │                          │                           │
         ▼                          ▼                           ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│    query_parser  │    │   chunk_index    │    │  instrumentation │
│ ──────────────── │    │ ──────────────── │    │ ──────────────── │
│ Query type det.  │    │ Chunk search     │    │ Timing/logging   │
│ Query expansion  │    │ Graph-RAG expand │    │ Observability    │
│ FTS5 BM25 search │    │ QW5 boundary     │    │ Profiling        │
│ Zero-result help │    │ Keyword extract  │    │                  │
└────────┬─────────┘    └────────┬─────────┘    └──────────────────┘
         │                       │
         ▼                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                         RRF FUSION (k=60)                         │
│                                                                   │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐         │
│  │ FTS5 Channel │    │ Vec Channel  │    │  KG Channel  │         │
│  │ (weight:0.5) │    │ (weight:0.3) │    │ (weight:0.2) │         │
│  │              │    │              │    │              │         │
│  │ memories_fts │    │ model2vec    │    │ kg_facts_fts │         │
│  │ chunks_fts   │    │ cosine sim   │    │ kg_entities  │         │
│  │ entities_fts │    │              │    │ Graph-RAG    │         │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘         │
│         │                   │                    │                │
│         └───────────────────┼────────────────────┘                │
│                             ▼                                     │
│                    Reciprocal Rank Fusion                          │
│                    score = Σ 1/(k + rank_i)                        │
└─────────────────────────────┬─────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                         RERANKING                                  │
│                                                                   │
│  ┌──────────────────────┐    ┌──────────────────────┐             │
│  │ Cross-Encoder Rerank │    │ Late Interaction     │             │
│  │ (blend: 0.6)         │    │ (blend: 0.3)         │             │
│  │ Optional Qwen3-0.6B  │    │ Semantic similarity  │             │
│  │ or BGE-m3 deep model │    │ token-level scoring  │             │
│  └──────────┬───────────┘    └──────────┬───────────┘             │
│             └───────────────────────────┘                         │
│                             ▼                                     │
│                    Blended rerank score                            │
└─────────────────────────────┬─────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                      SCORING & FILTERING                           │
│                                                                   │
│  ┌──────────────────────┐  ┌──────────────────────┐               │
│  │ Temporal Decay       │  │ Quality Gates        │               │
│  │ (half-life: 180d)    │  │ min_relevance: 0.1    │               │
│  │ exp(-age/halflife)   │  │ max_duplicate: 0.90   │               │
│  ├──────────────────────┤  ├──────────────────────┤               │
│  │ Neural Forget Curve  │  │ Injection Demotion    │               │
│  │ (Ebbinghaus 30d HL)  │  │ (BLK-1, default ON)  │               │
│  ├──────────────────────┤  ├──────────────────────┤               │
│  │ CTR Channel Weights  │  │ Concept Drift Check   │               │
│  │ (feedback-adjusted)  │  │ (centroid drift warn) │               │
│  ├──────────────────────┤  ├──────────────────────┤               │
│  │ Pinned Boost         │  │ Include_global blend  │               │
│  │ Recency Boost (0.1)  │  │ (local + global RRF)  │               │
│  └──────────────────────┘  └──────────────────────┘               │
│                                                                   │
│              compute_final_score() → sorted top-k                  │
└─────────────────────────────┬─────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                         SYNTHESIS                                  │
│                                                                   │
│  ┌──────────────────────┐  ┌──────────────────────┐               │
│  │ BB1 Synthesis        │  │ BB2 Multi-turn       │               │
│  │ Sentence-level        │  │ Conversation history │               │
│  │ contextual summary    │  │ resolution            │               │
│  └──────────────────────┘  └──────────────────────┘               │
│                                                                   │
│              Return: ranked snippets + metadata                    │
└──────────────────────────────────────────────────────────────────┘
```

### Search Pipeline Modules

| Module | LOC | Purpose |
|--------|-----|---------|
| `search/orchestrator.py` | 1,811 | Top-level search_memories + 28 helpers |
| `search/query_parser.py` | — | Query type detection, expansion, FTS execution, zero-result |
| `search/rerankers.py` | — | Cross-encoder (Qwen3/BGE-m3) + late interaction reranking |
| `search/scoring.py` | — | RRF fusion, temporal decay, neural forget, CTR weights |
| `search/synthesis.py` | — | BB1 sentence synthesis, BB2 multi-turn history |
| `search/chunk_index.py` | — | Chunk-based search, Graph-RAG expansion |
| `search/instrumentation.py` | — | Timing, logging, profiling observability |

---

## 4. Knowledge Graph & Fact Subsystem

```
                                MEMORY CONTENT
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          │                           │                           │
          ▼                           ▼                           ▼
┌──────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│  ENTITY NER      │    │  FACT EXTRACTION     │    │  TEMPORAL KG         │
│  (regex+LLM)     │    │  (regex+LLM)         │    │  (MEMORY_TEMPORAL_KG)│
│                  │    │                      │    │                      │
│  extract entities│    │  extract SPO triples  │    │  event_time extract  │
│  → kg_entities   │    │  → kg_facts          │    │  contradiction det.  │
│                  │    │  confidence scoring  │    │  supersession chain  │
│                  │    │                      │    │  edit invalidation   │
└────────┬─────────┘    └──────────┬───────────┘    └──────────┬───────────┘
         │                         │                           │
         ▼                         ▼                           ▼
┌────────────────────────────────────────────────────────────────────────┐
│                          KG DEDUP ENGINE                                 │
│                                                                         │
│  ┌────────────────────┐    ┌───────────────────────────────────┐        │
│  │ Exact Dedup        │    │ Semantic Dedup                     │        │
│  │ (normalize+hash)   │    │ (cosine sim > 0.92 → merge)        │        │
│  └────────────────────┘    └───────────────────────────────────┘        │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        KG STORAGE (SQLite)                            │
│                                                                       │
│  kg_entities          kg_edges              kg_facts                  │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐    │
│  │ id           │    │ source_id    │    │ id                    │    │
│  │ name         │    │ target_id    │    │ subject + object     │    │
│  │ type         │    │ relation     │    │ predicate             │    │
│  │ mention_count│    │ weight       │    │ confidence            │    │
│  │ is_stopword  │    │ evidence     │    │ event_time (v18)      │    │
│  │              │    │              │    │ valid_at / invalid_at │    │
│  │ FTS5 ✓       │    │              │    │ superseded_by         │    │
│  └──────────────┘    └──────────────┘    │ contradiction_score   │    │
│                                          │ locked (escape hatch) │    │
 │  kg_facts_fts (v20)                      │ FTS5 ✓ (v20)          │    │
 │  kg_entity_crdt (v21)                   │ CRDT ✓ (v21)          │    │
 │  kg_edge_crdt (v21)                     │ CRDT ✓ (v21)          │    │
 │  ┌──────────────┐                        └───────────────────────┘    │
│  │ FTS5 virtual │                                                     │
│  │ 3 sync trig. │   backlinks                kg_extraction_stats      │
│  │ (ai/ad/au)   │   ┌──────────────┐        ┌──────────────────┐     │
│  └──────────────┘   │ source_id    │        │ extraction quality│     │
│                     │ target_id    │        │ coverage metrics  │     │
│                     │ link_type    │        └──────────────────┘     │
│                     └──────────────┘                                  │
└──────────────────────────────────────────────────────────────────────┘
```

### Temporal KG (v18–v21)

```
          MEMORY SAVED
               │
               ▼
    ┌──────────────────────┐
    │  extract_event_time()│  ← 12 regex patterns (T2)
    │  "as of 2023-03"     │
    │  "since 2020"         │
    │  "during Q2 2024"     │
    └──────────┬───────────┘
               │
               ▼
    ┌──────────────────────────┐
    │ detect_fact_contradiction│  ← SPO match check (T3)
    │ same (S, P, O)?          │
    │ different event_time?     │
    └──────────┬───────────────┘
               │      YES
               ▼
    ┌──────────────────────────┐
    │  supersede_fact()        │  ← old.invalid_at = now
    │  old → superseded_by     │     new.supersedes = old
    │  new → valid_as_of now  │
    └──────────┬───────────────┘
               │
               ▼
    ┌──────────────────────────┐
    │  query_facts_at_time(t)  │  ← time-aware queries (T4)
    │  query_supersession_chain│
    │  query_facts_changed_since│
    └──────────────────────────┘

    Feature flag: MEMORY_TEMPORAL_KG=1 (default ON, T8)
    Escape hatch:  MEMORY_TEMPORAL_KG=0  →  plain facts, no temporal logic
    Per-fact lock: kg_facts.locked = 1  →  immutable, never superseded
```

---

## 5. Session Recall — Agent Cold-Start Flow

```
        AGENT SESSION STARTS
                │
    ┌───────────┼───────────┐
    │           │           │
    ▼           ▼           ▼
┌────────┐ ┌────────┐ ┌──────────────┐
│ Hook 1 │ │ Hook 2 │ │ memory_recall│
│Session │ │Proactive│ │_context()   │
│Start   │ │Context  │ │(MCP tool)   │
└───┬────┘ └───┬────┘ └──────┬───────┘
    │          │              │
    ▼          ▼              ▼
┌──────────────────────────────────────────────────────┐
│              memory_recall_context()                  │
│                                                       │
│  ┌─────────────────────────────────────────────────┐ │
│  │ 1. PINNED NOTES          │ pinned=true           │ │
│  │ 2. RECENT DIGESTS        │ <7d daily summaries   │ │
│  │ 3. HIGH IMPORTANCE       │ importance ≥ 4        │ │
│  │ 4. RELEVANT MEMORIES     │ FTS5+vector search    │ │
│  │ 5. USER PROFILE          │ top categories/tags   │ │
│  │ 6. DEEP RERANK (opt)     │ Qwen3-0.6B rerank     │ │
│  │ 7. SPACED REPETITION     │ due-for-review list   │ │
│  └─────────────────────────────────────────────────┘ │
│                                                       │
│  Output: Structured briefing → agent context           │
└──────────────────────────────────────────────────────┘
```

---

## 6. Hook System

```
┌──────────────────────────────────────────────────────────────────────┐
│                     OPECODE LIFECYCLE HOOKS                           │
│                                                                       │
│  SessionStart                    SessionEnd                          │
│  ┌────────────────────┐         ┌────────────────────┐               │
│  │ memory-session-    │         │ context_monitor.py │               │
│  │ start.py            │         │ end                 │               │
│  │                    │         │                    │               │
│  │ → recall_context() │         │ → session save     │               │
│  │ → pinned + recent  │         │ → compact trigger  │               │
│  │ → high-importance  │         │                    │               │
│  └────────────────────┘         └────────────────────┘               │
│                                                                       │
│  PreToolUse                      ToolComplete                        │
│  ┌────────────────────┐         ┌────────────────────┐               │
│  │ memory-proactive-  │         │ auto_save.py       │               │
│  │ context.py          │         │ tool-complete      │               │
│  │                    │         │                    │               │
│  │ → extract query    │         │ → enqueue JSONL    │               │
│  │ → search_memories  │         │ → daemon flushes   │               │
│  │ → push context     │         │ → ~2-5ms async     │               │
│  │ (result_limit=3)   │         │                    │               │
│  └────────────────────┘         └────────────────────┘               │
│                                                                       │
│  Pre-Compaction                                                  │
│  ┌────────────────────┐                                            │
│  │ context_monitor.py │                                            │
│  │ compact             │                                            │
│  │                    │                                            │
│  │ → memory save      │                                            │
│  │ → compact context  │                                            │
│  └────────────────────┘                                            │
└──────────────────────────────────────────────────────────────────────┘
```

### Hook Wiring

| Hook File | Event | Purpose | Limit |
|-----------|-------|---------|-------|
| `memory-session-start.py` | SessionStart | Cold-start context load | 5 results (MEMORY_HOOK_RESULT_LIMIT) |
| `memory-proactive-context.py` | PreToolUse | Search before each tool | 3 results (MEMORY_HOOK_RESULT_LIMIT) |
| `auto_save.py tool-complete` | ToolComplete (opencode.jsonc) | Save every tool invocation | Async daemon |
| `context_monitor.py` | PreCompaction + SessionEnd | Session persistence | — |
| `memory-search-on-demand.py` | Manual CLI | Search helper | — |

---

## 7. Async Auto-Save Architecture

```
 ┌──────────────────────────────────────────────────────────────┐
 │                   OPECODE PROCESS                             │
 │                                                               │
 │  tool completed  ──►  auto_save hook fires                    │
 │                          │                                    │
 │                          ▼                                    │
 │                   ┌──────────────────┐                        │
 │                   │  allowlist check  │  ← only 8 tools       │
 │                   │  denylist check   │  ← skip system tools  │
 │                   │  injection scan   │  ← safety gate        │
 │                   └────────┬─────────┘                        │
 │                            │                                  │
 │                            ▼                                  │
 │          ┌─────────────────────────────────┐                  │
 │          │  enqueue JSONL to inbox file    │  ← ~2-5ms        │
 │          │  POSIX atomic append            │                  │
 │          │  <memory>/.auto_save_inbox.jsonl│                  │
 │          └─────────────┬───────────────────┘                  │
 │                        │                                      │
 └────────────────────────┼──────────────────────────────────────┘
                          │
                          ▼
     ┌─────────────────────────────────────────────────────────┐
     │              AUTO-SAVE DAEMON (separate process)         │
     │              auto_save.py daemon                         │
     │                                                          │
     │  ┌─────────────────────────────────────────────────┐     │
     │  │  flock  ← single-daemon guarantee                │     │
     │  │  PID file  ← liveness check (.auto_save_daemon) │     │
     │  │  SIGTERM/SIGINT → flush + exit                  │     │
     │  └─────────────────────────────────────────────────┘     │
     │                                                          │
     │  ┌─────────────────────────────────────────────────┐     │
     │  │  tail inbox.jsonl                                │     │
     │  │  batch collect (50 entries or 500ms)              │     │
     │  │  flush → save_pipeline.save_memory()              │     │
     │  │  idle timeout → exit (1h of inbox silence)       │     │
     │  └─────────────────────────────────────────────────┘     │
     │                                                          │
     │  ┌─────────────────────────────────────────────────┐     │
     │  │  Fallback: if enqueue fails → sync path          │     │
     │  │  Fallback: if inbox > 100 MB → sync path        │     │
     │  │  Fallback: if MEMORY_ASYNC_AUTOSAVE=0 → sync    │     │
     │  └─────────────────────────────────────────────────┘     │
     └──────────────────────────────────────────────────────────┘
```

---

## 8. Background & Cron Infrastructure

```
┌──────────────────────────────────────────────────────────────────┐
│                     CRON SCHEDULER (26 jobs)                       │
│                     bash cron/install_crontab.sh                   │
│                     Per-cron flock lock (no overlap)               │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
    ┌────────────────────────────┼──────────────────────────────┐
    │                            │                              │
    ▼                            ▼                              ▼
┌─────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│ background_worker│    │   EVERY 15 MIN       │    │   DAILY              │
│ (every 15 min)   │    │                      │    │                      │
│                  │    │ task_queue worker     │    │ cron_auto_summarize  │
│ vec index repair │    │ db integrity check    │    │ cron_backup (02:00)  │
│ WAL checkpoint   │    │ WAL checkpoint        │    │ cron_integrity_check │
│ drift detection  │    │                      │    │ cron_heartbeat (03:00│
│ tier migration   │    └──────────────────────┘    │ cron_embedding_recomp│
│ circuit breaker  │                                │ cron_auto_share      │
│ watchdog         │                                │ cron_rebuild_fts     │
└─────────────────┘                                │ cron_tier_migration  │
                                                   └──────────────────────┘
    ┌──────────────────────────────────────────────────────────────────┐
    │                          WEEKLY (Sunday)                          │
    │                                                                   │
    │ cron_concept_drift (06:00)    cron_cross_session_learn (Mon 04:15│
    │ cron_consolidate (04:00)      cron_retention_stats (Mon 08:00)    │
    │ cron_kg_backfill (03:30)      cron_skill_extraction (Mon 03:45)   │
    │ cron_pinned_decay (05:00)    cron_rewrite_links (04:30)          │
    │ cron_integrity_check (01:00)                                      │
    └──────────────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────────────┐
    │                          MONTHLY (1st)                            │
    │                                                                   │
    │ cron_compact (01:00) — full rebuild: FTS, vec, KG, cross-session  │
    │ cron_purge_expired (06:30) — hard-delete tombstones >30d          │
    └──────────────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────────────┐
    │                      HOURLY (:05, :15)                            │
    │                                                                   │
    │ cron_sync.py — single-peer two-way sync                          │
    │ cron_crdt_sync.py — multi-peer CRDT sync (staggered +10m)        │
    └──────────────────────────────────────────────────────────────────┘
```

---

## 9. Storage Layer — Database Schema (~51 tables, v21)

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │                          SQLite (WAL mode, FK ON)                          │
 │                                                                           │
 │  ┌──────────────────────────────────────────────────────────────────┐     │
 │  │                   PRIMARY DOMAIN (15 tables)                       │     │
 │  │                                                                    │     │
 │  │  memories ──────► memories_fts (FTS5)                             │     │
 │  │  memory_chunks ─► memory_chunks_fts (FTS5)                        │     │
 │  │  memory_embeddings (256d vectors, ssm_state v15)                  │     │
 │  │  memory_vec_idx / memory_vec_keys (usearch HNSW)                  │     │
 │  │  memory_skills (cached skill extraction)                          │     │
 │  │  memory_audit_log (per-call observability)                        │     │
 │  │  memory_field_crdt (per-field LWWES CRDT, v13)                    │     │
 │  │  memory_ctr_feedback (search relevance feedback)                  │     │
 │  │  backlinks (wiki-style links)                                     │     │
 │  │  file_mtimes (incremental index tracking)                         │     │
 │  │  task_queue (SQLite-backed async queue)                           │     │
 │  └──────────────────────────────────────────────────────────────────┘     │
 │                                                                           │
 │  ┌──────────────────────────────────────────────────────────────────┐     │
 │  │               KNOWLEDGE GRAPH (7 tables)                          │     │
 │  │                                                                    │     │
 │  │  kg_entities ─────► kg_entities_fts (FTS5, v15)                  │     │
 │  │  kg_edges (FK→kg_entities, ON DELETE SET NULL, v17)               │     │
 │  │  kg_facts (SPO triples, temporal cols v18, FK→entities v19)      │     │
  │  │  kg_facts_fts (FTS5 v20); kg_entity_crdt, kg_edge_crdt (CRDT v21)    │     │
 │  │  kg_extraction_stats (quality metrics, v12)                       │     │
 │  │  drift_alarms (per-memory concept drift, v15)                     │     │
 │  │  concept_drift (centroid drift events, v16)                       │     │
 │  └──────────────────────────────────────────────────────────────────┘     │
 │                                                                           │
 │  ┌──────────────────────────────────────────────────────────────────┐     │
 │  │               LIFECYCLE & MAINTENANCE (5 tables)                  │     │
 │  │                                                                    │     │
 │  │  review_schedule (SM-2 spaced repetition)                        │     │
 │  │  user_access_log / user_profile_access_log (user profiling)       │     │
 │  │  arc_ghosts / arc_stats (ARC eviction, v14)                      │     │
 │  └──────────────────────────────────────────────────────────────────┘     │
 │                                                                           │
 │  ┌──────────────────────────────────────────────────────────────────┐     │
 │  │               MULTI-AGENT (3 tables)                               │     │
 │  │                                                                    │     │
 │  │  shared_memories (cross-agent pool)                               │     │
 │  │  sync_log (agent-level sync tracking)                             │     │
 │  │  schema_version (migration tracking, current=20)                  │     │
 │  └──────────────────────────────────────────────────────────────────┘     │
 │                                                                           │
 │  FTS5 Internals: 4 virtual tables × 4-5 internal tables each = 18 total   │
 │  (data, idx, config, content, docsize per FTS)                            │
 └──────────────────────────────────────────────────────────────────────────┘
```

---

## 10. Data Flow — End-to-End Lifecycle

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                           DATA LIFECYCLE                                 │
 │                                                                          │
 │                                                                          │
 │  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐           │
 │  │   INGEST     │      │    INDEX     │      │    SEARCH    │           │
 │  │              │      │              │      │              │           │
 │  │ Agent saves  │ ───► │ Markdown .md │ ───► │ FTS5 BM25    │           │
 │  │ Auto-save    │      │ SQLite upsert│      │ Vector (cos) │           │
 │  │ MCP tool     │      │ FTS5 index   │      │ KG facts     │           │
 │  │ CRDT sync    │      │ Chunks       │      │ RRF blend    │           │
 │  │ OKF import   │      │ Embeddings   │      │ Rerank       │           │
 │  │ File ingest  │      │ KG + facts   │      │ Quality gate │           │
 │  │ URL ingest   │      │ Backlinks    │      │ BB1/BB2 syn. │           │
 │  │              │      │ Tier assign  │      │              │           │
 │  └──────────────┘      └──────────────┘      └──────────────┘           │
 │                                                                          │
 │  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐           │
 │  │   MAINTAIN   │      │   REVIEW     │      │    EXPIRE    │           │
 │  │              │      │              │      │              │           │
 │  │ Consolidation│      │ SM-2 schedule│      │ Tier cold    │           │
 │  │ Dedup        │      │ Repetition   │      │ Pinned decay │           │
 │  │ Contradiction│      │ Importance   │      │ Purge expired│           │
 │  │ Concept drift│      │ Profile      │      │ Delete (30d) │           │
 │  │ Integrity     │      │ CTR feedback │      │ ARC eviction │           │
 │  │ Compact       │      │ Summarize    │      │              │           │
 │  └──────────────┘      └──────────────┘      └──────────────┘           │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

## 11. Safety & Concurrency Model

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │                         CONCURRENCY                                   │
 │                                                                       │
 │  ┌────────────────────┐    ┌───────────────────────────────────┐     │
 │  │  FLOCK (file lock)  │    │  SQLite WAL Mode                  │     │
 │  │  ────────────────   │    │  ─────────────────                │     │
 │  │  Single-writer      │    │  Multiple concurrent readers       │     │
 │  │  per memory dir     │    │  Write serialization: BEGIN IMMED  │     │
 │  │  Lock BEFORE conn   │    │  No external deps (no Redis)      │     │
 │  │  (P0-2)            │    │  Connection pool per DB path       │     │
 │  └────────────────────┘    └───────────────────────────────────┘     │
 │                                                                       │
 │  ┌────────────────────────────────────────────────────────────┐      │
 │  │                     SAFETY GATES                             │      │
 │  │                                                             │      │
 │  │  Injection Detection    memory_injection.py                 │      │
 │  │  → scan before save     risk_score ≥ 0.5 → silent reject   │      │
 │  │  → demote in search     BLK-1 result demotion               │      │
 │  │                                                             │      │
 │  │  Contradiction Check    contradiction_detector.py           │      │
 │  │  → pre-save phrase scan memory_check_contradictions()       │      │
 │  │  → periodic corpus audit memory_detect_contradictions()     │      │
 │  │                                                             │      │
 │  │  Circuit Breaker        auto_save.py                        │      │
 │  │  → >3 failures / 60s → open for 300s                       │      │
 │  │  → persisted to audit_log (cross-restart visible)           │      │
 │  │  → state queryable via circuit_breaker_status()             │      │
 │  │                                                             │      │
 │  │  Saga Rollback          save/saga.py                        │      │
 │  │  → undo_upsert on failure                                  │      │
 │  │  → cleanup_memory_relations (KG, facts, backlinks)          │      │
 │  │  → conflict detection via initial_file_content              │      │
 │  │  → .conflict-PID-TS loser-preserving writes                 │      │
 │  └────────────────────────────────────────────────────────────┘      │
 └──────────────────────────────────────────────────────────────────────┘
```

---

## 12. Sync & Multi-Agent

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │                      MULTI-AGENT ARCHITECTURE                          │
 │                                                                       │
 │  ┌───────────────────┐    ┌───────────────────┐                       │
 │  │   Agent A          │    │   Agent B          │                       │
 │  │   (localhost)      │    │   (localhost)      │                       │
 │  │                    │    │                    │                       │
 │  │  local memory.db   │    │  local memory.db   │                       │
 │  │  ┌───────────┐    │    │  ┌───────────┐    │                       │
 │  │  │ CRDT merge│    │    │  │ CRDT merge│    │                       │
 │  │  │ v13+v14   │    │    │  │ v13+v14   │    │                       │
 │  │  └─────┬─────┘    │    │  └─────┬─────┘    │                       │
 │  │        │          │    │        │          │                       │
 │  └────────┼──────────┘    └────────┼──────────┘                       │
 │           │                        │                                   │
 │           └────────┬───────────────┘                                   │
 │                    │                                                   │
 │                    ▼                                                   │
 │  ┌──────────────────────────────────────────────────────────┐         │
 │  │              SYNC SERVER (sync_server.py)                  │         │
 │  │                                                            │         │
 │  │  HTTP :9877                                                │         │
 │  │  TLS/mTLS optional (MEMORY_SYNC_TLS_CERT/KEY/CLIENT_CA)    │         │
 │  │  Bearer token auth (MEMORY_SYNC_TOKEN)                     │         │
 │  │  HMAC-SHA256 body signing (MEMORY_SYNC_HMAC_SECRET)        │         │
 │  │  Max timestamp age: 300s (MEMORY_SYNC_MAX_AGE)             │         │
 │  │  Strict CORS (empty=no wildcard, SEC-1 fix)                │         │
 │  │                                                            │         │
 │  │  Sync formats:                                             │         │
 │  │  ┌──────────────────────────────────────────────────┐     │         │
 │  │  │ CRDT sync (version vectors, LWWES)               │     │         │
 │  │  │ OKF export/import (Open Knowledge Format)        │     │         │
 │  │  │ shared_memories pool (cross-agent, TTL 30d)     │     │         │
 │  │  └──────────────────────────────────────────────────┘     │         │
 │  └──────────────────────────────────────────────────────────┘         │
 └──────────────────────────────────────────────────────────────────────┘
```

---

## 13. Feature Flag Map

| Flag | Subsystem | Default | What it controls |
|------|-----------|---------|------------------|
| `MEMORY_TEMPORAL_KG` | Temporal KG | ON | Event-time extraction, contradiction detection, supersession, edit invalidation |
| `MEMORY_KNOWLEDGE_GRAPH` | KG | ON | Entity/relation extraction + Graph-RAG |
| `MEMORY_TEMPORAL_TIERS` | Tiers | ON | Hot/Warm/Cold tier management |
| `MEMORY_CONTEXTUAL_ENRICHMENT` | Write | ON | FTS5 overlap search on save |
| `MEMORY_QUALITY_GATES` | Search | ON | Dedup + min relevance filtering |
| `MEMORY_SUMMARIZATION` | Lifecycle | ON | TF-IDF auto-summarize |
| `MEMORY_CONSOLIDATION` | Lifecycle | ON | SHA-256 + n-gram Jaccard dedup, contradiction |
| `MEMORY_MULTI_AGENT` | Sync | ON | Cross-agent sharing via shared_memories |
| `MEMORY_ADAPTIVE_RETENTION` | Lifecycle | ON | Psi-formula half-life + neural forget |
| `MEMORY_SELF_DIRECTED` | Lifecycle | ON | Heartbeat, tier assignment, archive |
| `MEMORY_USER_PROFILE` | Profile | ON | Preference profiling from access history |
| `MEMORY_FORGETTING_CURVE` | Search | ON | Ebbinghaus decay |
| `MEMORY_CONTEXTUAL_RETRIEVAL` | Search | ON | Category+tags prepended to embeddings |
| `MEMORY_LATE_INTERACTION` | Search | ON | Late interaction reranking |
| `MEMORY_FTS5_CACHE` | Search | ON | FTS5 LRU result cache (30s TTL) |
| `MEMORY_SAGA_ENABLED` | Write | ON | Transactional save (DB + vec + file) |
| `MEMORY_RERANKER_DISABLED` | Search | OFF | Qwen3-0.6B / BGE-m3 deep reranker disable |
| `MEMORY_CRDT_ENABLED` | Sync | ON | Version vector tracking + conflict resolution |
| `MEMORY_ASYNC_AUTOSAVE` | Hook | ON | Async inbox+daemon auto-save |

---

## 14. Tool Surface Map

```
 ┌────────────────────────────────────────────────────────────────────┐
  │                       85 MCP TOOLS                                  │
 │                                                                     │
 │  CORE (15) — Always exposed                                         │
 │  ┌──────────────────────────────────────────────────────────────┐  │
 │  │ save · search · semantic_search · facts_search · graph_search│  │
 │  │ recall_context · session_start · user_profile · delete       │  │
 │  │ restore · check_contradictions · scan_injection · rebuild    │  │
 │  │ supersede · profile_access                                   │  │
 │  └──────────────────────────────────────────────────────────────┘  │
 │                                                                     │
 │  ADMIN (64) — Routed via memory_maintenance(operation="...")        │
 │  ┌──────────────────────────────────────────────────────────────┐  │
 │  │ STATS:       tier_stats arc_stats quality_stats graph_stats  │  │
 │  │              facts_stats profile_stats retention_stats       │  │
 │  │              summarization_stats shared_stats audit_stats    │  │
 │  │                                                              │  │
 │  │ LIFECYCLE:   consolidate compact rebuild backfill_all         │  │
 │  │              heartbeat adaptive_retention run_tier_migration  │  │
 │  │              purge_expired purge_auto_saves pinned_decay     │  │
 │  │                                                              │  │
 │  │ QUALITY:     detect_contradictions check_concept_drift       │  │
 │  │              check_integrity quality_filter duplicates       │  │
 │  │              merge_suggestions list_drift_alarms             │  │
 │  │                                                              │  │
 │  │ KG/TEMPORAL: temporal_contradictions temporal_query          │  │
 │  │              facts_list graph_traverse graph_shortest_path   │  │
 │  │                                                              │  │
 │  │ MULTI-AGENT: crdt_sync crdt_status share shared_list         │  │
 │  │              shared_import auto_share                        │  │
 │  │                                                              │  │
 │  │ I/O:         okf_export okf_import ingest_file ingest_url    │  │
 │  │              auto_summarize daily_digest summarize           │  │
 │  │                                                              │  │
 │  │ META:        audit audit_query circuit_breaker_status        │  │
 │  │              auto_save_status compile_skill strip_provenance │  │
 │  │              llm_unload metrics_server dashboard             │  │
 │  │              agent_init/clear/list   sdk_demo                │  │
 │  │              check_embedding_model incremental_update        │  │
 │  │              merge_embeddings rewrite_links record_ctr       │  │
 │  │              review_schedule extract_skills list_skills      │  │
 │  │              arc_reset auto_save_hook                        │  │
 │  └──────────────────────────────────────────────────────────────┘  │
 └────────────────────────────────────────────────────────────────────┘
```

---

## 15. Module Map — Complete File Structure

```
agentic-memory/                              ← repo root
├── memory_mcp.py                           ← MCP server entry (thin orchestrator)
├── memory.toml                              ← centralized config + feature flags
├── config.py                                ← config singleton dataclass
├── tool_registry.py                         ← 15 CORE + 70 ADMIN (single source of truth)
│
├── WRITE PATH ──────────────────────────────────────────────────────────────
├── save_pipeline.py                         ← write path shim (1,359 LOC) → save/
├── save/
│   ├── __init__.py                          ← public API re-exports
│   ├── saga.py                              ← transactional rollback + conflict
│   ├── indexers.py                          ← FTS5, embedding, chunk, KG, fact writes
│   ├── backlinks.py                         ← auto-backlink computation
│   ├── post_save_hooks.py                   ← 7 named hooks (fitness, tier, CRDT, audit...)
│   ├── crdt_helpers.py                      ← CRDT version vector helpers
│   └── cleanup.py                           ← dead-row cleanup on rollback
├── knowledge_graph.py                       ← entity extraction (regex + LLM)
├── fact_extraction.py                       ← SPO triple extraction (regex + LLM)
├── kg_dedup.py                              ← exact + semantic entity dedup
│
├── READ PATH ───────────────────────────────────────────────────────────────
├── search_pipeline.py                       ← read path shim → search/
├── search/
│   ├── __init__.py                          ← public API re-exports
│   ├── orchestrator.py                      ← search_memories() + 28 helpers (1,811 LOC)
│   ├── query_parser.py                      ← query type detection, expansion, FTS
│   ├── rerankers.py                         ← cross-encoder + late interaction
│   ├── scoring.py                           ← RRF fusion, temporal decay, CTR, forget
│   ├── synthesis.py                         ← BB1 sentence synthesis, BB2 multi-turn
│   ├── chunk_index.py                       ← chunk search, Graph-RAG expansion
│   └── instrumentation.py                  ← timing, logging, observability
├── embedding_search.py                      ← model2vec semantic search
│
├── HOOKS ────────────────────────────────────────────────────────────────────
├── auto_save.py                             ← tool-call auto-save + async daemon (2,469 LOC)
├── hooks/
│   ├── memory-proactive-context.py          ← PreToolUse hook
│   ├── memory-session-start.py              ← SessionStart hook
│   ├── memory-search-on-demand.py           ← CLI search helper
│   ├── memory-recall-session.py             ← manual recall trigger
│   └── _log_error.py                        ← shared logging module
├── context_monitor.py                       ← compaction + idle + session-end
│
├── INFRA ────────────────────────────────────────────────────────────────────
├── db.py                                    ← connection pool + re-entrancy guard
├── memory_common.py                        ← shared utilities + atomic_write
├── infrastructure.py                        ← _err, audit decorators, path resolution
├── cache.py                                 ← search result cache (LRU + TTL)
├── migration_runner.py                      ← schema migrations (current v21)
├── background_queue.py                      ← SQLite-backed async task queue
├── background_worker.py                     ← task queue worker (flock-protected)
├── crdt_merge.py                            ← CRDT merge logic
├── crdt_field.py                            ← per-field CRDT (v13)
│
├── SAFETY ───────────────────────────────────────────────────────────────────
├── memory_injection.py                      ← prompt injection detection
├── contradiction_detector.py               ← conflict detection
├── memory_integrity.py                      ← file/DB drift + FTS5 + KG orphan repair
│
├── LIFECYCLE ────────────────────────────────────────────────────────────────
├── tier_migration.py                        ← hot/warm/cold tier management
├── spaced_repetition.py                     ← SM-2 spaced repetition
├── cross_session_learn.py                   ← pattern extraction from sessions
├── backfill_all.py                          ← audit pipeline shim → backfill/
├── backfill/
│   ├── index_backfills.py                   ← FTS, embedding, chunk, backlink, vec
│   └── kg_backfills.py                     ← KG facts, KG graph, entity filter
│
├── SYNC ─────────────────────────────────────────────────────────────────────
├── sync_server.py                           ← HTTP sync server (TLS/mTLS optional)
├── sync_client.py                           ← sync client
├── memory_sharing.py                        ← shared memory pool
├── okf.py                                   ← Open Knowledge Format export/import
│
├── MCP DOMAIN MODULES ───────────────────────────────────────────────────────
├── mcp_memory.py    mcp_search.py    mcp_kg.py         mcp_safety.py
├── mcp_retention.py mcp_quality.py   mcp_profile.py    mcp_audit.py
├── mcp_maintenance.py    mcp_maintenance_ops.py    mcp_common.py
├── mcp_rebuild.py  mcp_agent.py     mcp_crdt.py       mcp_okf.py
├── mcp_tools.py    mcp_sharing.py   mcp_multi_modal.py
├── mcp_ctr_drift.py mcp_summarization.py mcp_async.py
├── mcp_instance.py mcp_kg_traversal.py  mcp_metrics.py
├── mcp_dashboard.py mcp_sdk.py
│
├── CRON ─────────────────────────────────────────────────────────────────────
├── cron/                                     ← 25 scripts + install_crontab.sh
├── background_worker.py                      ← also a cron entry
│
├── EVAL ─────────────────────────────────────────────────────────────────────
├── eval/                                     ← 183 test files, 3,494 test functions
│
└── DOCS ─────────────────────────────────────────────────────────────────────
    ├── docs/architecture.md
    ├── docs/arch-diagram.md                  ← this file
    ├── docs/concepts/temporal-kg.md
    ├── docs/concepts/knowledge-graph.md
    └── ... (reference, how-to, explanation)
```

---

*Generated: 2026-06-25 · Schema v21 · 102 modules · ~126,954 LOC total (56,799 production + 69,155 tests)*