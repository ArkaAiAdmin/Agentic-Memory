# Enterprise-Grade Execution Plan

## Overview

Three parallel workstreams:
1. **Benchmarks** (sequential, clean run) — prove the system works
2. **Bug fixes** (parallel) — fix all issues from deep code review
3. **Documentation** (parallel) — professional-grade docs

---

## Stream 1: Benchmark Suite (Sequential — Clean Run)

### Why Sequential
Benchmarks must be reproducible and uncontaminated by concurrent work. Each benchmark builds on the previous one's baseline.

### 1.1 Search Quality Benchmarks
**Goal**: Prove the 12-phase pipeline is better than simpler alternatives.

**Steps**:
1. Create labeled test dataset: 100+ queries with known relevant memories
2. Implement benchmark harness: `eval/benchmark_search.py`
3. Run 4 configurations:
   - Config A: FTS5 only (Phase 1 only)
   - Config B: FTS5 + Vector (Phases 1-4)
   - Config C: FTS5 + Vector + Reranking (Phases 1-6)
   - Config D: Full 12-phase pipeline
4. Measure: MRR@10, Recall@20, NDCG@10, Precision@5
5. Output: Comparison table with statistical significance

### 1.2 Latency Benchmarks
**Goal**: Prove the system is fast enough for real-time use.

**Steps**:
1. Implement latency harness: `eval/benchmark_latency.py`
2. Measure per-phase latency (p50, p95, p99)
3. Measure end-to-end search latency under:
   - Cold start (no cache)
   - Warm cache
   - Concurrent access (10, 50, 100 threads)
4. Output: Latency distribution tables

### 1.3 Throughput Benchmarks
**Goal**: Prove the system can handle production load.

**Steps**:
1. Implement load test: `eval/benchmark_throughput.py`
2. Measure QPS under:
   - Read-only workload (search)
   - Write-only workload (save)
   - Mixed workload (80% read / 20% write)
3. Measure at: 10, 50, 100, 500, 1000 concurrent connections
4. Output: QPS vs concurrency curves

### 1.4 Concurrency & Consistency Benchmarks
**Goal**: Prove the system is safe under concurrent access.

**Steps**:
1. Implement concurrency test: `eval/benchmark_concurrency.py`
2. Test: 10 threads writing to same note simultaneously
3. Test: 10 threads reading while 1 writes
4. Test: CRDT merge under concurrent field edits
5. Verify: No data loss, no corruption, eventual consistency
6. Output: Consistency verification report

### 1.5 Memory & Resource Benchmarks
**Goal**: Prove the system is resource-efficient.

**Steps**:
1. Implement resource monitor: `eval/benchmark_resources.py`
2. Measure: RSS, CPU, disk I/O under load
3. Measure: SQLite WAL size, FTS5 index size, vec index size
4. Output: Resource usage report

### 1.6 Benchmark Report
**Goal**: Publish results.

**Steps**:
1. Create `docs/benchmarks/` directory
2. Write `docs/benchmarks/methodology.md`
3. Write `docs/benchmarks/results.md` with tables and charts
4. Update `docs/how-to/performance-benchmarks.md` with real numbers
5. Update README with benchmark badge

---

## Stream 2: Bug Fixes (Parallel — 5 Sub-agents)

### 2.1 CRDT Fixes (Sub-agent 1)
**Files**: `crdt/crdt_field.py`, `crdt/crdt_merge.py`, `infra/sync_client.py`, `infra/sync_server.py`

| # | Bug | Fix |
|---|-----|-----|
| C1 | Tombstone tiebreaker inconsistency (line 397-406) | Align with `_lww_tiebreak` direction |
| C2 | Push not field-CRDT-aware | Add field_crdt data to push payload |
| C3 | Tags lost on pull | Pass tags parameter to crdt_field_save |
| C4 | Bearer token timing attack (sync_server.py:182) | Use hmac.compare_digest |
| C5 | Connection leaks in skill sync | Use context manager |
| C6 | Duplicate query in crdt_field_save loop (line 810-814) | Hoist above loop |

### 2.2 Saga & Write Path Fixes (Sub-agent 2)
**Files**: `infra/saga.py`, `save/pipeline.py`

| # | Bug | Fix |
|---|-----|-----|
| S1 | Dead code: _read_new_content_for_file | Delete function |
| S2 | mark_applied called twice | Remove duplicate call |
| S3 | _delete_memory_row commits outside saga | Add comment documenting why |

### 2.3 RBAC & Auth Fixes (Sub-agent 3)
**Files**: `infra/rbac.py`, `mcp_verbs.py`, `infra/authorizer.py`, `infra/authlib_sso.py`, `memory_delete.py`

| # | Bug | Fix |
|---|-----|-----|
| R1 | Policy deny effect never checked | Add `AND p.effect != 'deny'` to query |
| R2 | _check_authorization fail-open on exceptions | Log exception, return deny |
| R3 | OIDC nonce not validated | Add nonce claim verification |
| R4 | Empty redirect_uri in OIDC | Use configured redirect_uri |
| R5 | SAML signature not enforced by framework | Add verify flag parameter |
| R6 | _is_cross_tenant_admin LIKE '%:admin' | Use explicit role list |
| R7 | list_trash no RBAC | Add RBAC check |
| R8 | Authorizer docstring wrong | Fix to say fail-closed |

### 2.4 Search Pipeline Fixes (Sub-agent 4)
**Files**: `search/scoring.py`, `search/orchestrator.py`, `infra/metrics_server.py`

| # | Bug | Fix |
|---|-----|-----|
| P1 | Neural forget curve misleading name | Rename to jaccard_surprise_penalty |
| P2 | Whitespace tokenization crude | Use regex tokenization |
| P3 | _phase_latencies.clear() not atomic | Hold lock during clear |
| P4 | _temporal_decay_factor timezone issue | Use UTC explicitly |
| P5 | SQL injection in metrics_server.py:74 | Parameterize query |
| P6 | _apply_concept_boost opens own DB | Accept existing connection |

### 2.5 MCP Tool Fixes (Sub-agent 5)
**Files**: `mcp_memory.py`, `mcp_maintenance_ops.py`

| # | Bug | Fix |
|---|-----|-----|
| T1 | Duplicated RBAC helpers | Extract to shared module |
| T2 | Injection scanner threshold not tunable | Add config option |

---

## Stream 3: Documentation Overhaul (Parallel — 3 Sub-agents)

### 3.1 Fix Stale Content (Sub-agent 6)
**Files**: `docs/MCP_SURFACE.md`, `docs/reference/schema.md`, `docs/reference/configuration.md`, `docs/reference/mcp-tools.md`, `README.md`

| Task | What |
|------|------|
| Regenerate MCP_SURFACE.md | Schema v56, 115 tools |
| Regenerate schema.md | 57 migrations, 69 tables |
| Regenerate configuration.md | All config keys + 34 env vars |
| Regenerate mcp-tools.md | 115 tools |
| Fix README badges | Schema v56, 4848 tests |

### 3.2 Add Missing Foundation (Sub-agent 7)
**Files**: `CHANGELOG.md`, `CONTRIBUTING.md`, `docs/reference/known-issues.md`, `docs/benchmarks/methodology.md`

| Task | What |
|------|------|
| Create CHANGELOG.md | All commits from git log |
| Create known-issues.md | All documented gaps and limitations |
| Create benchmarks/methodology.md | Benchmark methodology |
| Update comparison.md | Current competitor data |

### 3.3 Build Docs Site + API Docs (Sub-agent 8)
**Files**: `mkdocs.yml`, `.github/workflows/docs.yml`, `docs/api/*.md`

| Task | What |
|------|------|
| Configure MkDocs | Material theme, search, navigation |
| Add CI workflow | Build docs on push |
| Generate API docs | From docstrings |
| Test code examples | eval/test_doc_examples.py |

---

## Execution Order

```
Phase 1 (Parallel):
  ├── Sub-agent 1: CRDT fixes
  ├── Sub-agent 2: Saga fixes
  ├── Sub-agent 3: RBAC fixes
  ├── Sub-agent 4: Search fixes
  ├── Sub-agent 5: MCP tool fixes
  ├── Sub-agent 6: Fix stale docs
  ├── Sub-agent 7: Add missing docs
  └── Sub-agent 8: Build docs site

Phase 2 (Sequential — after all Phase 1 complete):
  └── Benchmark suite (1.1 → 1.2 → 1.3 → 1.4 → 1.5 → 1.6)

Phase 3 (After Phase 2):
  └── Run full test suite to verify everything
```

## Success Criteria

| Metric | Target |
|--------|--------|
| Full test suite | 0 failures |
| Benchmark MRR@10 | > 0.7 (12-phase vs < 0.5 for FTS-only) |
| Benchmark p99 latency | < 200ms |
| Benchmark QPS | > 50 at 100 concurrent |
| Documentation | All stale content fixed, docs site buildable |
| Bug fixes | All 20+ bugs from code review fixed |
