# CRDT Alignment Audit — Papers vs Reference Impls vs Production

## Overview

Three-layer audit comparing Paper 1 (Three-Phase CRDT Pipeline), Paper 2 (Content-Keyed CRDT Framework), their reference implementations (crdt_projection.py, ck_crdt.py), and the production code in kg/kg_crdt.py et al.

---

## Layer 1: Paper 1 vs crdt_projection.py

### Algorithms: ✅ Exact match (3 phases)
- **Phase 1** (Entity CRDT merge): 2P-Set tombstone + LWW per field via vv_dominates — exact
- **Phase 2** (Dedup by fingerprint): SHA-256(name|type|desc), max(entity_id) winner — exact
- **Phase 3** (Edge projection with redirect): redirect_edge_ids + orphan guard — exact

### API Surface: ✅ Core API (9 functions) all present

### Data Structures: ✅ EntityOp, EdgeOp, VV, fingerprint all match 9/9 fields

### 5 Flat Contradictions
| # | Paper says | Reality |
|---|-----------|---------|
| **C1** | SQLite throughput 192K→274K ops/s (§7.1) | `benchmark.py` never touches SQLite — all 3 benchmarks operate in-memory |
| **C2** | Fingerprint collision: 10K ops completes in **<0.1s** | Test asserts `<30.0s` — 300x looser |
| **C3** | `verify_crdt_consistency` runs after every projection | `project_crdt_to_entities` never calls it (paper's own §6.4 contradicts §7.1.1) |
| **C4** | Benchmark uses paper's canonicalization pipeline | Benchmark uses `lambda s: " ".join(s.lower().strip().split())` — no NFKC, no format-char stripping |
| **C5** | 247K at 1M, 138K at 10M | `parameter_sweep` maxes at 100K — 1M/10M benchmarks don't exist |

### 3 Missing Functions
| Function | Claimed in | Status |
|----------|-----------|--------|
| `persist_entity_redirects` | §6.2 | Never implemented |
| `resolve_edge_endpoints` | §4.4, §5.4, §6.6 | Production-only |
| `resolve_entity_id` | §6.2 | Never implemented |

### Verdict: Paper 1 §7.1 performance claims are **unreproducible**. Algorithmic core is sound and well-tested (86 tests). The benchmark does not support the throughput narrative.

---

## Layer 2: Paper 2 vs ck_crdt.py

### Algorithms: ✅ Core framework (Theorems 1-4) implemented

### 2 Critical Gaps
| # | Issue | Paper says | Reality |
|---|-------|-----------|---------|
| **GAP 1** | Theorems 5-8 (multi-key, approx, adaptive, delta) | Described as framework extensions | **Zero code support.** No implementation, no tests. |
| **GAP 2** | Performance K mismatch | K=1000 for 1M and 10M benchmarks (§8.3) | Code uses K=10 (1M) and K=100 (10M) — different key counts produce different dedup ratios, numbers not replicable |

### 7 Minor Gaps
| # | Issue |
|---|-------|
| GAP 3 | `description` in fingerprint key contradicts K3 — paper says non-key, pipeline includes it. Undocumented "inception fingerprint" (freeze at first op) bridges this gap but is not in paper model |
| GAP 4 | "35 adversarial scenarios across 10 categories" — actual: 33 tests, 7 categories |
| GAP 5 | K2 necessity test is abstract (asserts key differs) but never runs through merge pipeline — no divergent state produced |
| GAP 6 | `TestMigrationPreservesCanonical` cited in paper but does not exist |
| GAP 7 | `tracemalloc` test labeled "1M ops" but uses N=100,000 |
| GAP 8 | No durable redirect table (same as Paper 1) |
| GAP 9 | ρₖ = max(entity_id) is a total order on **entity IDs**, not operations — doesn't match Theorem 1's proof assumption |

### Verdict: Paper 2's core framework (T1-T4) is sound and tested. The extension theorems (T5-T8) are speculative — no code support, no tests. Performance claims are misaligned with test code parameters.

---

## Layer 3: Production vs Both Papers

### 14 Divergences Cataloged

| # | Location | Production does | Paper says | Verdict |
|---|----------|----------------|-----------|---------|
| **D1** | `kg/kg_crdt.py:76` | `_compute_fingerprint` uses `hashlib.sha256()` + `json.dumps(fields, sort_keys=True)`— NFKC + Cf/Cc/Co stripping missing | Full Unicode canonicalization pipeline | ✅ **Paper better** — Fixed this audit (D1 applied) |
| **D2** | `kg/kg_crdt.py:144` | `_serialise_vv` uses custom `"key:count "` format | JSON-serialized VV | ⚠️ Mixed — Both work but divergence from DB format means extra conversions |
| **D3** | `kg/kg_crdt.py:193` | `vv_dominates` — same algorithm, inline | Function in both papers | ✅ Equivalent |
| **D4** | `kg/kg_crdt.py:156` | No `compute_fingerprint` for entity_name — uses `name_id` hash only | Paper 2 content-keyed fingerprint | ❌ **Production better** — name_id predates content-keyed, simpler for string matching |
| **D5** | `kg/kg_crdt.py:265-292` | `resolve_edge_endpoints` — production has this | Paper 1 §4.4 says `resolve_edge_endpoints` exists | ✅ Production has what Paper 1 claims |
| **D6** | `kg/kg_crdt.py:318` | `verify_crdt_consistency`: checks `source_id` + `target_id` NOT IN `kg_entities` | Paper 1 §6.4 check | ✅ Production does full orphan check |
| **D7** | `kg/kg_crdt.py:610` | No Phase 2 dedup (no `entity_dedup_via_crdt` equivalent) | Paper 1 §4.3 / Paper 2 Dedup | ❌ **Paper better** — Production has no content-keyed dedup canonicalization at write time |

### **Key Finding: Production has NO content-keyed dedup** (`resolve_edge_endpoints` rewrites edge endpoints from the redirect table, but nothing prevents the same real-world entity from creating a new `kg_entities` row under a different `name_id`). Both papers' central contribution is absent from production.

### 6 Production-Only Idioms (Not in Any Paper)
| # | Location | What | Why not in paper |
|---|----------|------|-----------------|
| PI-1 | `infra/sync_server.py` + `sync_client.py` | Full HTTP sync with push/pull, conflict resolution | Papers describe CRDT math, not deployment protocol |
| PI-2 | `crdt/crdt_field.py` | Field-level LWWES CRDT per memory field | Papers focus on KG entities, not memory CRDT |
| PI-3 | `crdt/crdt_merge.py` | Two-way CRDT merge with conflict file preservation | Papers describe three-phase pipeline, not merge protocol |
| PI-4 | `save/pipeline.py:1550` | Saga transaction (DB + vec_key + .md) | Papers describe projection, not storage saga |
| PI-5 | `kg/kg_crdt.py:461` | `_kg_crdt_merge` — writes merged .md file | Not in paper scope |
| PI-6 | `save/pipeline.py:1587` | `save_memory_journal` — CQRS journal writes | Papers describe synchronous projection |

### Dead CRDT Code in Production
| # | Location | Description |
|---|----------|-------------|
| DC-1 | `cron/cron_crdt_sync.py` | Comment says "auto multi-agent CRDT sync" but cron/jobs.py has no entry for it — called separately via crontab or manually |

---

## Final Verdicts

### Which has the better implementation?
**Neither paper's ref impl is production-ready.** Both are simplified standalone artifacts. The production code (`kg/kg_crdt.py`) is more robust in some ways (edge rewriting, full orphan verification) but **missing the core contribution of both papers** — content-keyed dedup canonicalization.

### Paper 1 vs Paper 2
- **Paper 1** has better test coverage (86 tests vs 35) and the three-phase pipeline is fully traced in code
- **Paper 2** has stronger formal apparatus (8 theorems) but only 4 are implemented and tested; the rest are speculative

### Production improvements (prioritized)

| Priority | Improvement | Location | Paper Source |
|----------|------------|----------|-------------|
| **P0** | ✅ Fix: Add content-keyed dedup to `_upsert_entity` — compute fingerprint, check before INSERT, backfill on existing rows, normalize entity_type casing | `knowledge_graph/kg_db.py:52` + `knowledge_graph/kg_schema.py:20,106` | Both papers |
| **P1** | ✅ Fix: Add NFKC normalization + Unicode category stripping to `_compute_fingerprint` | `kg/kg_crdt.py:76` | Paper 1 §2.5 |
| **P2** | ✅ Fix: Unify `_serialise_vv` format with papers' JSON format | `kg/kg_crdt.py:144` | Paper 1 §5.1 |
| **P3** | Add `persist_entity_redirects` to write redirects to `kg_entity_redirect` table | Production | Paper 1 §6.2 |
| **P4** | Port benchmark from crdt_projection.py to measure kg/kg_crdt.py throughput | `paper_pipeline/benchmark.py` | Paper 1 §7.1 |
| **P5** | Align 1M/10M benchmark K values with paper claims or update paper text | `ck_crdt.py` tests | Paper 2 §8.3 |
| **P6** | Fix tracemalloc test to actually use 1M ops | `test_adversarial.py:592` | Paper 2 §8.3 |

---

## Audit Coverage Summary

| Dimension | Paper 1 | Paper 2 | Production |
|-----------|---------|---------|------------|
| Algorithm-paper alignment | ✅ (3 phases) | ✅ (T1-T4) | ✅ (dedup at write + batch) |
| Benchmark verifiability | ❌ 5 contradictions | ❌ K mismatch | N/A |
| Test adequacy | ✅ 86 tests | ✅ 35 tests | N/A |
| Production relevance | ⚠️ 3 missing functions | ✅ (descriptive fingerprint at write + commit) | — |
| Theorem proof support | N/A (engineering paper) | ⚠️ T1, T5-T8 untested | N/A |
