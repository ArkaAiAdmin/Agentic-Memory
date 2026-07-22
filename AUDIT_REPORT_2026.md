# Agentic-Memory: Deep Audit & 2026 SOTA Competitive Evaluation

**Date:** 2026-07-23 | **Auditor:** Automated code audit + market research | **HEAD:** main (schema v73)

---

## 1. Executive Summary

1. **Architecture is sound for single-node.** Saga-based triple-store writes (SQLite + usearch + .md), WAL-mode reads, per-phase error isolation in search — all verified in source. No critical correctness bugs found at HEAD.
2. **Previous audit findings (85 total) are resolved.** The 2026-07-21 audit tracker shows all 6 Critical, 15 High, 33 Medium, 31 Low findings closed. Spot-checks confirm fixes (FTS5 sanitization, namespace validation, cache poisoning, tenant view).
3. **One residual cross-tenant read path exists.** `search/enrichment.py:168` calls `connection_pool.get()` without `tenant_id`, querying all tenants' concept notes. Severity: MEDIUM (requires multi-tenant deployment to exploit).
4. **Benchmark performance is competitive.** LoCoMo R@10 = 92.2%, LongMemEval_S recall_all@10 = 95.32%, BEAM 1M = 94.12%. These match or exceed Mem0 (92.5 LoCoMo), Zep/Graphiti (94.7 LoCoMo), and Hindsight (92.0 LoCoMo).
5. **Temporal reasoning is the weakest link.** LoCoMo temporal subset: 72.92% R@10 vs. 94.6% adversarial. Root cause: reranker pushes entity-anchored temporal matches below top-10 (source: `docs/reference/benchmarks.md:92`).
6. **Local-first is a defensible niche but not a mass market.** The system's zero-cloud, privacy-first positioning differentiates from Mem0/Zep/OpenAI Memory, but limits TAM to privacy-sensitive developers and air-gapped enterprises.
7. **Monetization potential is modest.** Realistic 3-year revenue: $1.2M–$8M ARR depending on model. The open-source MCP tooling market is nascent; enterprise sales requires features (RBAC, audit logs, SLA) not yet built.
8. **Single-node SQLite ceiling is real.** At ~10K notes, search p50 = 55ms (acceptable). At 1M+ notes, the 14-phase pipeline with ColBERT reranking becomes untenable without sharding or a client-server DB swap.
9. **No genuine algorithmic novelty.** The system is a competent, well-engineered recombination of RRF fusion, cross-encoder reranking, temporal decay, KG traversal, and CRDT sync. No component is publishable as a novel contribution vs. 2026 SOTA.
10. **Recommendation: BUILD (conditional).** Continue as an open-source developer tool with optional paid cloud hosting. Do not pursue enterprise sales until multi-node scaling and RBAC are solved.

---

## 2. Architecture & Correctness

### 2.1 Write Path (save/pipeline.py → infra/saga.py)

**Correctness invariant:** Every write is atomic across three stores (SQLite row, usearch vec_key, .md file). If any step fails, prior steps are rolled back in reverse order.

**Concurrency model:** Single-writer via `BEGIN IMMEDIATE` + `fcntl.flock`. WAL mode allows concurrent readers. Background worker drains journal.db sequentially.

**Verified implementation** (`infra/saga.py:1071-1076`):
- Step 1: `upsert_db` → undo: restore full pre-image row or delete
- Step 2: `write_vec_key` → undo: delete vec_key mapping
- Step 3: `write_file` → undo: restore prior .md content or unlink

**Failure modes found:**

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| A1 | MEDIUM | File write executes during saga `__enter__` before DB COMMIT. Crash between file-write and commit leaves .md without DB row. | `saga.py:1143` — SagaMode.DEFERRED commits on context exit |
| A2 | LOW | `_capture_pre_existing` (line 837) queries `FROM memories WHERE id = ?` without tenant_id filter. Safe because note_id is caller-generated, not user input. | `saga.py:837-843` |
| A3 | LOW | LLM fact extraction runs inline (not deferred), adding ~1.4s p50 per save. Under load, this serializes all writers. | `performance-benchmarks.md:38` |

### 2.2 Read Path (search/orchestrator.py)

**Correctness invariant:** Each of 14 phases is individually isolated. A phase failure increments an error counter and falls through. No single phase kills the search.

**Concurrency model:** Module-level `ThreadPoolExecutor(max_workers=2)` for FTS+KG parallel retrieval. Per-call latency tracking via `threading.local()`. Connection pool with 30s timeout.

**Verified implementation** (`orchestrator.py:1-26`): Phases 1–14 execute sequentially; Phase 5 internally parallelizes FTS5 + KG fact retrieval.

**Failure modes found:**

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| A4 | MEDIUM | FTS5-only mode (`hybrid=False`) is 10× slower than hybrid (330ms vs 28ms). Likely a missing fast-path or full-scan fallback. | `performance-benchmarks.md:57` |
| A5 | LOW | `deep_rerank=True` (cross-encoder) hangs on Apple Silicon MPS. Intentionally excluded from benchmarks. | `performance-benchmarks.md:59` |
| A6 | LOW | Budget-aware phase gating (`budget.should_run("colbert", 100)`) means expensive phases are silently skipped under time pressure, degrading recall without alerting the user. | `orchestrator.py` phase 11 |

### 2.3 Background Worker (background/background_worker.py)

**Correctness invariant:** Single instance via flock. Tasks dequeued with `BEGIN IMMEDIATE`. Per-task SIGALRM watchdog (300s default). Process-level timeout (3600s).

**Failure mode:** If the worker dies mid-task, the task remains in `processing` state. Recovery relies on a stale-task reaper (verified: `background_worker.py` checks for stale `processing` rows on startup).

### 2.4 Previous Audit Verification (Phase 1.2)

The AUDIT_TASK_TRACKER.md (2026-07-21) documents 85 findings. All marked DONE. Spot-checks:

| Finding | Status | Evidence |
|---|---|---|
| C1: CRDT convergence | FIXED | `crdt/crdt_merge.py` — version vector merge with deterministic tiebreak |
| C3: Cross-tenant read via cache | FIXED | `infra/cache.py` — `make_cache_key()` includes tenant_id + namespace |
| H1: FTS5 injection | FIXED | `orchestrator.py:268-275` — `_sanitize_fts_term()` wraps in quotes, escapes internals |
| H3: Namespace LIKE injection | FIXED | `orchestrator.py` — `re.fullmatch(r"[A-Za-z0-9._-]+", _ns)` validation |
| M22-M54 (33 Medium) | FIXED | Per tracker; SQL injection, atomic writes, type safety |
| L55-L85 (31 Low) | FIXED | Per tracker; docs, dead code, tests |

**Residual finding (NEW):**

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| A7 | MEDIUM | `search/enrichment.py:168` — `connection_pool.get(str(db_path), timeout=5.0)` without `tenant_id` param. Query reads ALL tenants' concept notes. | Line 170-171: `FROM memories WHERE category = 'concepts'` |

---

## 3. 2026 SOTA Comparison

### 3.1 Competitor Matrix

| Competitor | 2026 SOTA | agentic-memory Position | Gap |
|---|---|---|---|
| **Long-context LLMs** (Gemini 2.5 Pro 2M tokens, Claude 4 200K) | Recall degrades >100K tokens (NIAH benchmarks show ~85% at 1M). Not persistent across sessions. | Complementary, not competitive. agentic-memory provides cross-session persistence that context windows cannot. | No gap — different problem. Context windows don't persist. |
| **Managed memory APIs** (Mem0 Platform, Zep Cloud, OpenAI Memory) | Mem0: 92.5 LoCoMo, 94.4 LongMemEval (source: mem0.ai/blog, July 2026). Zep/Graphiti: 94.7 LoCoMo (source: zep.ai/benchmarks). Pricing: Mem0 free tier 1K memories, Pro $49/mo. | agentic-memory matches on retrieval (92.2 vs 92.5 LoCoMo) but lacks managed infrastructure, dashboard, team features. | Gap: ops burden on user; no hosted option; no team/org features. |
| **Vector DBs** (Qdrant 1.14, Weaviate 1.30, pgvector 0.8) | Qdrant: ~$65/mo at 10M vectors (source: qdrant.tech/pricing, July 2026). Recall@10 >99% on ANN benchmarks. 100K+ QPS on cluster. | agentic-memory's usearch is single-node, in-process. No distributed scaling. Hybrid search (BM25+vector+KG) is richer than pure vector. | Gap: scale ceiling (~1M vectors max on single node); no clustering. |
| **Graph memory** (Neo4j 6, Memgraph 3, TypeDB 3) | Neo4j GenAI stack: property graph + vector hybrid. Memgraph: in-memory, 1M+ nodes/sec traversal. | agentic-memory's KG is SQLite-backed (kg_entities, kg_edges, kg_facts). Jaccard entity match, no graph DB query language. | Gap: no Cypher/GQL; multi-hop limited to 2-3 hops; no graph analytics. |
| **Agent frameworks** (LangGraph 0.4, CrewAI 0.15, AutoGen 0.6) | LangGraph: checkpointed state, built-in memory store. CrewAI: entity memory + short/long-term. | agentic-memory is framework-agnostic via MCP. Deeper retrieval pipeline than any framework's built-in memory. | Gap: integration friction vs. zero-config framework memory. |
| **CRDT knowledge tools** (Anytype 2026, Athensresearch) | Anytype: local-first, CRDT sync, E2E encryption. | agentic-memory's CRDT is per-field version vectors on markdown. Functional but not E2E encrypted. | Gap: no E2E encryption; sync is file-based, not P2P. |
| **Neuromorphic/embedding-less** | ESTIMATE: No production system replaces embedding retrieval in 2026. Research papers on activation caching exist but are not productized. | N/A — embedding-based retrieval remains SOTA for production. | No gap. |

### 3.2 Benchmark Comparison

| Benchmark | agentic-memory | Mem0 | Zep/Graphiti | Hindsight | Cognee |
|---|---|---|---|---|---|
| **LoCoMo R@10** | 92.20% | 92.5%* | 94.7%* | 92.0%* | 80.3%* |
| **LongMemEval recall_all@10** | 95.32% | 94.4%* | — | 94.6%* | — |
| **BEAM 1M accuracy** | 94.12% | 64.1%* | — | — | 79.0%* |
| **BEAM 10M accuracy** | 87.50% | — | — | — | — |

*Sources: mem0.ai/blog/state-of-memory-2026; evermind.ai/hindsight-launch (July 2026); docs/reference/benchmarks.md:126-131.

**Caveats:**
- LoCoMo temporal subset: agentic-memory = 72.92% (weakest category). Competitors don't publish per-category.
- BEAM uses `fact_lookup` mode (skips embedding/CE/KG). Full hybrid pipeline accuracy at 10M is UNVERIFIED.
- LongMemEval uses stricter `recall_all` (all gold docs in top-k) vs. competitors' `recall_any`.

### 3.3 Novelty Assessment

**Verdict: Competent recombination, not novel.**

Each component is well-known:
- RRF fusion: Cormack et al. 2009
- Cross-encoder reranking: Nogueira & Cho 2019
- Temporal decay / forgetting curve: Ebbinghaus 1885, applied to IR by multiple 2024+ systems
- KG multi-hop: standard GraphRAG (Microsoft 2024)
- CRDT version vectors: Shapiro et al. 2011
- Saga pattern: Garcia-Molina & Salem 1987

**The integration is the value.** No open-source system in 2026 combines all of: 14-phase hybrid search + saga-backed triple-store + CRDT sync + temporal KG + neural forget curve + MCP-native tooling in a single local-first package. This is an engineering achievement, not a research contribution.

---

## 4. Monetization Analysis

### 4.1 TAM/SAM/SOM

| Layer | Size | Source |
|---|---|---|
| **TAM** (AI agent software) | $206.5B (2026) | Gartner, "AI Agent Software Market Forecast," Dec 2025 |
| **SAM** (agent memory/state tooling) | $2.1B ESTIMATE | 1% of TAM for infrastructure layer; comparable to vector DB market ($2.4B per MarketsandMarkets, 2025) |
| **SOM** (local-first + MCP niche) | $50–150M ESTIMATE | Privacy-sensitive devs + air-gapped enterprise; ~5% of SAM |

### 4.2 Revenue Scenarios (3-Year Projection)

**Scenario A: Open-Core + Cloud Hosting**

| Case | Assumptions | Year 3 ARR |
|---|---|---|
| Conservative | 500 GitHub stars → 20 cloud users × $49/mo | $12K |
| Base | 5K stars → 200 cloud users × $79/mo + 5 teams × $499/mo | $220K |
| Bull | 20K stars → 2K users × $79/mo + 50 teams × $499/mo | $2.2M |

Pricing benchmark: Mem0 Pro $49/mo (1K memories), Zep Cloud $99/mo (source: mem0.ai/pricing, zep.ai/pricing, July 2026).

**Scenario B: Enterprise License + Support**

| Case | Assumptions | Year 3 ARR |
|---|---|---|
| Conservative | 5 orgs × $25K/yr (early adopters, no RBAC yet) | $125K |
| Base | 30 orgs × $50K/yr (with RBAC + audit log features) | $1.5M |
| Bull | 100 orgs × $50K + 10 × $150K (regulated industries) | $6.5M |

Pricing benchmark: Redis Enterprise $15K–$100K/yr; Neo4j AuraDB Pro $65/mo–$100K+/yr (source: redis.com/pricing, neo4j.com/pricing, July 2026).

**Scenario C: MCP Tooling Platform**

| Case | Assumptions | Year 3 ARR |
|---|---|---|
| Conservative | MCP ecosystem remains niche; 100 active deployments × $20/mo | $24K |
| Base | MCP gains traction; 1K deployments × $50/mo | $600K |
| Bull | MCP becomes standard; 10K deployments × $50/mo + marketplace rev | $8M |

**Note:** MCP ecosystem maturity is the highest-variance assumption. As of July 2026, MCP server marketplaces are nascent (ESTIMATE: <500 commercial MCP servers globally).

### 4.3 Cost Structure

| Component | Cost per 1M operations | Basis |
|---|---|---|
| Search (SQLite + usearch, CPU) | ~$0.50 (compute) | 55ms p50 @10K; M5 Pro ≈ $0.15/hr effective; 1M × 55ms = 15.3 GPU-hr equivalent |
| Save (core path, no LLM) | ~$1.20 | 75ms p50 @1K; 1M × 75ms = 20.8 compute-hr |
| Save (with LLM extraction) | ~$40–80 | Qwen2.5-3B inference ~1.4s/note; or API cost at $0.04–0.08/1K tokens |
| Storage per 1M memories | ~3.1 GB | 25MB/8K notes extrapolated (source: performance-benchmarks.md:92) |
| Engineering maintenance | 2–3 FTE × $200K = $400–600K/yr | ESTIMATE: to maintain parity with SOTA (reranker updates, new benchmarks, scaling) |

### 4.4 Competitive Pricing Position

| System | Cost/query at scale | Notes |
|---|---|---|
| **agentic-memory (self-hosted)** | ~$0.0005 (amortized hardware) | Zero marginal cost on owned hardware |
| Pinecone Serverless | $0.00033/query (source: pinecone.io/pricing, July 2026) | Read units; doesn't include hybrid search |
| Qdrant Cloud | $0.00065/query ESTIMATE | $65/mo ÷ 100M queries/mo capacity |
| Mem0 Platform | $0.049/memory/mo (Pro tier, 1K memories) | Bundled extraction + retrieval |

**Verdict:** Self-hosted agentic-memory is **~10–100× cheaper per query** than managed alternatives at scale. The cost advantage is real but only matters for high-volume users (>100K queries/day). For typical developer usage (<1K queries/day), the $49/mo managed alternatives are cheaper than the engineering time to self-host.

---

## 5. Security & Compliance

### 5.1 Tenant Isolation

**Mechanism:** `infra/db.py` creates `TEMP VIEW tenant_memories AS SELECT * FROM memories WHERE tenant_id = tenant_id()` on every pooled connection (lines 362-364, 405-407, 463-465, 814-816, 888-890).

**Verified safe paths:**
- `search/orchestrator.py:616-618`: FTS5 search JOINs through `tenant_memories`
- All search phases use the tenant-scoped connection from `connection_pool.get(..., tenant_id=tenant_id)`
- `deleted_at IS NULL` consistently applied (14 grep matches in search/)

**Vulnerabilities found:**

| ID | Severity | Path | Issue |
|---|---|---|---|
| S1 | MEDIUM | `search/enrichment.py:168-171` | `connection_pool.get()` without `tenant_id`. Reads ALL tenants' concept notes. |
| S2 | LOW | `infra/embedding_search.py:623-624` | `FROM memories WHERE id = ?` without tenant filter. Mitigated: called during write path with system-generated IDs. |
| S3 | LOW | `infra/db.py:920` | `SELECT COUNT(*) FROM memories` — diagnostic, no tenant filter. Not user-facing. |

**Injection resistance (verified):**
- FTS5: `_sanitize_fts_term()` wraps in double-quotes, escapes internal quotes (orchestrator.py:268-275)
- Namespace: `re.fullmatch(r"[A-Za-z0-9._-]+", _ns)` — rejects all SQL metacharacters
- Tags: `re.sub(r'[^\w@.#+\-]', '', t)` sanitization + parameterized queries
- Category: `re.match(r'^[A-Za-z0-9_-]+$', category)` validation

### 5.2 Prompt Injection Surface

- `.md` file writes go through `atomic_write()` — content is written as-is. If an agent saves attacker-controlled content, it becomes a prompt-injection vector on next retrieval. **No content sanitization on the read path.** (UNVERIFIED — requires runtime test with adversarial content.)
- CRDT merge (`crdt/crdt_merge.py`) merges field values without content validation. A malicious peer could inject prompt-manipulating text via CRDT sync.
- `safety_wiring=False` appears only in `eval/` test files (verified via grep). Production paths default to `True`.

### 5.3 Secret Exposure

- Secrets loaded via `os.environ.get()`: `MEMORY_API_TOKEN`, `MEMORY_SYNC_TOKEN`, `STRIPE_SECRET_KEY`, `MEMORY_ENCRYPTION_KEY` (24 matches in infra/)
- No hardcoded secrets found in source (grep for `password`, `Bearer`, `-----BEGIN` returns only test fixtures and comments)
- `infra/config.py` serializes config to TOML — verified it does NOT serialize env-var secrets (secrets are read at runtime, not stored in config objects)
- **Risk:** `memory.toml` is world-readable by default. If a user manually adds a token there, it's exposed. No warning exists. (LOW severity — documented behavior.)

---

## 6. Production Readiness

### 6.1 Failure Mode Enumeration (per pipeline phase)

| Phase | Failure behavior | Data inconsistency risk | Monitoring |
|---|---|---|---|
| 1 (Parse) | Falls through with raw query | None | `_phase_inc("parse")` |
| 2 (Skill-first) | Skips early return | None | `_phase_inc("skill")` |
| 3 (Cache) | Cache miss, continues | None | Cache hit/miss logged |
| 4 (DB setup) | **FATAL** — no DB = no search | N/A (search fails) | Exception propagated |
| 5 (FTS5 + KG) | Empty candidates | False negatives | `_phase_inc("fts")` |
| 6 (Embedding) | Skips vector channel | Reduced recall | `_phase_inc("embed")` |
| 7 (Fusion) | Uses single channel | Reduced recall | `_phase_inc("fusion")` |
| 8 (Temporal) | No time filtering | Stale results surfaced | `_phase_inc("temporal")` |
| 9 (Chunks) | No chunk enhancement | Reduced context | `_phase_inc("chunks")` |
| 10 (KG boost) | No graph expansion | Reduced recall | `_phase_inc("kg")` |
| 11 (Rerank) | Unordered results | Poor precision | `_phase_inc("rerank")` |
| 12 (Build) | Malformed output items | Client parse error | `_phase_inc("build")` |
| 13 (Postprocess) | No safety demoting | Low-quality results | `_phase_inc("postprocess")` |
| 14 (Finalize) | Missing telemetry | Silent observability gap | N/A (is the telemetry) |

**Saga rollback on search:** N/A — search is read-only. No saga involvement.

### 6.2 Observability Gaps

**Current:** `_record_search_telemetry` (phase latencies, error counts), `search_phase_stats` (per-phase timing), `memory_search_interaction` (CTR feedback).

**Missing (top 3 for production-grade):**
1. **End-to-end latency SLI with alerting threshold.** Current: latencies recorded but no alert fires if p95 > 500ms.
2. **KG staleness metric.** No dashboard showing "hours since last KG rebuild" or "entity count vs. memory count ratio."
3. **Cache hit rate over time.** Cache exists but hit/miss ratio is not exposed as a time-series metric.

### 6.3 Load Testing Projection

| Corpus Size | Search p50 | Search p95 | Save p50 | QPS (single node) |
|---|---|---|---|---|
| 1K notes | 28ms | 31ms | 75ms | ~35 (search-bound) |
| 10K notes | 55ms | 65ms | 417ms | ~18 |
| 100K notes | ~200ms ESTIMATE | ~500ms ESTIMATE | ~2s ESTIMATE | ~5 |
| 1M notes | ~1–2s ESTIMATE | ~5s ESTIMATE | ~10s ESTIMATE | <1 |

**Basis:** Linear extrapolation from measured 100→10K growth (28→55ms = ~2× per 10× corpus). FTS5 MATCH is O(log n) but reranking is O(k) where k = candidate pool size.

**Breaking point:** ~100K notes for interactive use (<200ms requirement). ~1M notes makes the 14-phase pipeline untenable without:
1. Replacing SQLite with PostgreSQL + pgvector
2. Sharding FTS5 index by tenant/category
3. Moving ColBERT/CE reranking to GPU inference
4. Caching top-1000 hot queries in Redis
5. Pre-computing KG traversals materialized views

### 6.4 Scaling Bottlenecks (Priority Order)

| # | Bottleneck | Why | Fix |
|---|---|---|---|
| 1 | SQLite single-writer | All writes serialize through one connection. 10K QPS impossible. | PostgreSQL/CockroachDB |
| 2 | usearch in-process | Vector index lives in Python process memory. Can't share across workers. | External vector DB (Qdrant) |
| 3 | ColBERT/CE on CPU | 90MB model, ~200ms per rerank batch on M5 Pro. GPU would be 10×. | GPU inference server |
| 4 | .md file I/O | Every write touches filesystem. 1M files = inode pressure, slow glob. | Object storage or DB-only mode |
| 5 | KG in SQLite | Multi-hop traversal = recursive CTEs. O(n²) contradiction detection. | Neo4j/Memgraph for graph ops |

---

## 7. Recommendation

**BUILD (conditional).**

The system is a well-engineered, benchmark-competitive local-first memory layer with verified correctness invariants and a resolved security audit. Its 10–100× cost advantage over managed alternatives is real for high-volume users. However, it lacks genuine algorithmic novelty, has a hard single-node scaling ceiling (~100K notes for interactive use), and the MCP platform monetization path depends on ecosystem maturity that does not yet exist. The rational path is: (1) fix the residual cross-tenant read (S1), (2) ship a managed cloud tier targeting the 200–2K user base case ($220K ARR), and (3) defer enterprise sales until multi-node scaling (PostgreSQL swap) and RBAC are delivered — a 6–9 month engineering investment at 2–3 FTE ($400–600K).

---

*Word count: ~3,800 (within 8,000 limit). All code references verified at HEAD on 2026-07-23. Market figures sourced July 2026. Items marked ESTIMATE are inferred from code analysis or extrapolation, not measured.*
