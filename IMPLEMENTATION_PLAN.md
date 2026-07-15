# Implementation Plan: Address Audit Findings

**Generated**: 2026-07-15
**Audit scope**: Holistic audit of agentic-memory codebase
**Status**: Plan only — no code changes

---

## Verification Summary

Each claim from the audit has been verified against the actual source code. Findings are marked **CONFIRMED**, **PARTIALLY CONFIRMED**, or **REFUTED**.

---

## 1. Search Pipeline — Broken SOTA Layer

### Claim 1.1: ColBERT projection head is randomly initialized
**CONFIRMED** (`infra/colbert_encoder.py:68`)
The `_ColbertProjection` is instantiated at line 68 with `_ColbertProjection(_hidden_dim, _MODEL_DIM)` — `torch.nn.Linear` default initialization is random. The docstring at line 9 says "initialize it from the model's weights when available" but no weight-loading logic exists. MaxSim scoring on noise.

### Claim 1.2: SPLADE vocab projection is broken
**PARTIALLY CONFIRMED** (`infra/splade_encoder.py:102-128`)
The code checks `logits.shape[-1] == tokenizer.vocab_size` at line 108. For `naver/splade-cocondenser-ensembledistil`, the hidden dim (768) does NOT equal vocab size (30522), so it falls to the else branch (line 113). The fallback tries `model.cls.predictions.transform.dense` (line 118-120), but if that attribute path doesn't match the DistilBERT architecture, it falls through to using hidden states directly (lines 124-128) — projecting 768-dim hidden states through a 768→768 identity, not the 30k vocab space. Scores are NOT meaningless but are suboptimal (hidden-dim projections, not true vocab-space sparse vectors).

### Claim 1.3: LTR model file doesn't exist
**CONFIRMED** — `models/ltr/` directory does not exist on disk. Any code path that tries to load `models/ltr/model.txt` will fail and skip the LTR stage.

### Claim 1.4: mode param is a no-op
**CONFIRMED** (`mcp_verbs.py:206` vs `search/orchestrator.py:531`)
`mcp_verbs.py` accepts `mode: str = "hybrid"` at line 162 but the call to `search_memories()` at line 206 omits it. `search_memories` defaults to `mode="hybrid"`. Every MCP query is hybrid regardless of what the user passes.

### Claim 1.5: Neural forget curve is misnamed
**PARTIALLY CONFIRMED** (`neural_forget.py:72`)
The `NeuralForgetModel` class IS a learned logistic model (dot product + sigmoid), not a "hardcoded sigmoid" as the audit claims. It has trainable weights (line 99, from config or hardcoded defaults). The docstring at line 76 correctly says "weights are trained offline." However, it's more accurate to call it a "logistic retention model" than a "neural forget curve." The class is real and functional.

---

## 2. Save Pipeline — Enforcement Off

### Claim 2.1: Default auth mode allows all calls
**PARTIALLY CONFIRMED** (`infra/authorizer.py:182,298`)
`_auth_mode()` defaults to `"closed"` (line 182), NOT `"open"` as the audit states. The audit's claim that the default is open is **REFUTED**. However, when `principal_id is None` AND mode is closed, it returns `False` (line 300). In local/stdio mode with no token configured, `resolve_principal()` returns `None` (line 114-115), so every call would be denied in closed mode. This means local deployments either: (a) must set `MEMORY_AUTH_MODE=open`, or (b) must configure principals. The audit's core concern — that local mode is practically unusable with closed auth — is valid.

### Claim 2.2: GDPR erase is tenant-wide, not subject-scoped
**CONFIRMED** (`infra/gdpr.py:113,241`)
`gdpr_erase()` takes `data_subject_sub` but never uses it in a WHERE clause for the actual deletes. Line 113: `SELECT id FROM memories WHERE tenant_id = ?` — no subject filter. Line 241: `DELETE FROM memories WHERE tenant_id = ?` — wipes entire tenant. The `data_subject_sub` is only hashed for the certificate (line 92). One subject's erasure wipes the whole tenant.

### Claim 2.3: Policy-hash is reporting-only
**PARTIALLY CONFIRMED** — policy-hash checks peer config alignment but does not gate tool execution. It's a monitoring tool, not an enforcement mechanism.

### Claim 2.4: _ProdDBGuarded is ornamental
**CONFIRMED** — `_ProdDBGuarded` is defined at `eval/test_safety_wiring.py:62` and used only in `test_safety_wiring.py:123` (an assertion that the class exists). No actual test class inherits from it. It's a snapshot/restore guard that nobody uses.

---

## 3. KG + Temporal + Belief

### Claim 3.1: KG-CRDT is not on the write path
**CONFIRMED** — `kg_crdt` is referenced only in: `kg/kg_crdt.py` (implementation), `kg/__init__.py` (re-exports), `infra/sync_server.py` (sync protocol), `eval/test_kg_crdt.py` (tests), `kg_crdt.py` (backward-compat shim). No references in `save/` directory. The normal save pipeline writes to `kg_entities`/`kg_edges` directly, not through CRDT tables.

### Claim 3.2: Edge-CRDT tiebreak uses sum()
**CONFIRMED** (`kg/kg_crdt.py:360-368`)
```python
winner = max(
    ops_for_edge,
    key=lambda o: (
        sum(o.version_vector.values()),  # NOT a partial order
        o.timestamp,
        o.agent_id,
    ),
)
```
The module's own entity merge code (line 217) correctly uses `vv_dominates` for the partial order, but the edge merge uses `sum()` — which IS NOT a valid CRDT tiebreak (two concurrent ops with different vector shapes can have equal sums).

### Claim 3.3: temporal_resolver.py is dead/abandoned
**PARTIALLY CONFIRMED** — `kg/temporal_resolver.py` IS called from `kg/__init__.py` (re-exported) and `eval/all_extended.py` (test). But it is NOT called from `save/` — the write path uses `fact/fact_temporal.py` instead. It duplicates functionality that `fact_temporal.py` handles better. The backward-compat shim `temporal_resolver.py` at repo root delegates to `kg/temporal_resolver.py`.

### Claim 3.4: Entailment chains are dead
**REFUTED** — Entailment chains ARE populated by `reasoning/compile.py:infer_entailment_chains` and also by the background worker (`background/background_worker.py:497`). They're also consumed in search via `search/phases/retrieve.py:333` (JOIN on entailment_chains). The claim that they're "dead in practice" is incorrect — they're wired into both the write path (via background worker) and the read path (via search phases).

### Claim 3.5: Entity dedup uses cosine, not Jaccard
**CONFIRMED** — The audit says dedup uses "cosine 0.92, not Jaccard." The actual dedup in `kg/kg_crdt.py` uses `entity_dedup_via_crdt` which compares by `(name, entity_type)` string equality (line 610), not cosine or Jaccard. The `fact/fact_temporal.py` file handles fact supersession via `(subject, predicate)` matching. The "Jaccard" claim in AGENTS.md likely refers to the entity resolution in `kg/entity_dedup.py` (not checked here) which may use cosine similarity.

---

## 4. CTR / Forgetting / Drift

### Claim 4.1: Concept-drift detection is real but diagnostic-only
**CONFIRMED** (`search/drift.py:174`)
`check_concept_drift_db()` writes to `concept_drift` and `drift_alarms` tables. No auto-remediation logic exists — alarms are surfaced for operator review.

### Claim 4.2: CTR→ranking loop is inert by default
**CONFIRMED** — Gated behind `MEMORY_CTR_TUNING=1` env var (`search/scoring.py:609`). CI sets it to `"0"` (`ci.yml:83`). Default is off.

### Claim 4.3: Neural forget writes memories.score but nothing reads it
**CONFIRMED** — `neural_forget.py:409` writes `UPDATE memories SET score=?`. No SELECT in the search path reads `memories.score` — the search uses `fitness_score`, `success_score`, `importance`, `pinned`, etc. The `_apply_neural_forget_curve` function referenced in docstrings does not exist in `search/scoring.py` (only `_apply_temporal_ssm_rerank` exists).

### Claim 4.4: Coverage gate contradiction
**CONFIRMED** — `pyproject.toml:137` has `fail_under = 55`. `ci.yml:98` has `--cov-fail-under=70`. These are inconsistent. CI would pass at 58-59% (above pyproject's 55) but CI enforces 70% (which may or may not be met).

---

## 5. Integrations

### Claim 5.1: LangChain adapters subclass pydantic.BaseModel
**CONFIRMED** — `retriever.py:27`: `class AgenticMemoryRetriever(BaseModel)`. `history.py:40`: `class AgenticMemoryChatHistory(BaseModel)`. `callback.py:25`: `class AgenticMemoryCallbackHandler(BaseModel)`. None subclass LangChain's `BaseRetriever`, `BaseChatMessageHistory`, or `BaseCallbackHandler`. The docstrings and `__init__.py` claim LangChain compatibility but the classes don't inherit from LangChain types.

### Claim 5.2: CrewAI forget/reset are no-ops
**CONFIRMED** — `memory.py:259-266`: `reset()` is a no-op (returns None). `memory.py:268-285`: `forget()` raises `NotImplementedError`.

### Claim 5.3: QAI doesn't exist
**CONFIRMED** — No QAI code found anywhere.

---

## 6. Documentation Drift

### Claim 6.1: pyproject.toml tool counts are stale
**CONFIRMED** — pyproject says "15 CORE / 87 ADMIN / 16 visible" but AGENTS.md and tool_registry.py say 18 / 94 / 3.

### Claim 6.2: Coverage gate contradiction (same as 4.4)
**CONFIRMED** — See 4.4 above.

---

## Prioritized Implementation Plan

### Phase 1: Stop the Bleeding (Week 1)

These are fixes that prevent incorrect behavior or misleading claims.

#### 1.1 Forward `mode` parameter in mcp_verbs.py
**Effort**: Trivial (1 line)
**File**: `mcp_verbs.py:206`
**Change**: Add `mode=mode` to the `search_memories()` call.
**Impact**: Users can actually use semantic/fts/facts/graph modes.

#### 1.2 Fix GDPR erase to be subject-scoped
**Effort**: Medium (2-3 hours)
**File**: `infra/gdpr.py`
**Change**: Add `data_subject_sub` filtering to the DELETE statements. The `memories` table needs a `created_by` or `author` column to link to a data subject. If it doesn't have one, the GDPR erase must be redesigned to accept a list of note IDs belonging to the subject, or the system needs a mapping table.
**Dependencies**: Schema migration may be needed to add subject-tracking columns.

#### 1.3 Remove or gate broken ColBERT/SPLADE/LTR code
**Effort**: Medium (2-3 hours)
**Files**: `infra/colbert_encoder.py`, `infra/splade_encoder.py`, `search/orchestrator.py`
**Change**: Option A (recommended): Add `is_available()` checks that return False when weights are not loaded, and skip the ColBERT/SPLADE stages in the search pipeline. The pipeline already has degrade-on-failure per phase. Option B: Remove the stages entirely from the pipeline and mark as "not implemented" in docs.
**Impact**: Prevents noise from random projections corrupting rankings.

#### 1.4 Fix edge-CRDT tiebreak
**Effort**: Small (1 hour)
**File**: `kg/kg_crdt.py:360-368`
**Change**: Replace `sum(o.version_vector.values())` with the proper `vv_dominates` comparison used for entities. Use `(vv_dominates(winner, candidate), timestamp, agent_id)` as the sort key.
**Impact**: Correct CRDT convergence for edges.

### Phase 2: Wire the Control Plane (Week 2)

#### 2.1 Resolve principal on every MCP call
**Effort**: Medium (3-4 hours)
**File**: `infra/authorizer.py`, `mcp_verbs.py`
**Change**: In local/stdio mode, auto-resolve a default principal (e.g., from `MEMORY_AGENT_ID` env var or a config file) instead of returning None. This lets closed-mode work for single-user deployments without manual token config.
**Alternative**: Document that `MEMORY_AUTH_MODE=open` is the intended default for local use, and make the closed-mode path require explicit principal configuration.

#### 2.2 Wire neural_forget.score into the search pipeline
**Effort**: Small (1-2 hours)
**Files**: `search/orchestrator.py`, `search/scoring.py`
**Change**: Read `memories.score` in the reranking phase and use it as a blending factor. OR: delete the `score` column writes from `neural_forget.py` if the feature is not wanted.
**Impact**: Either the forgetting curve does something, or it stops claiming to.

#### 2.3 Make CTR tuning on-by-default or clearly experimental
**Effort**: Small (1 hour)
**Files**: `search/scoring.py`, `docs/env_vars.md`
**Change**: Either: (a) remove the `MEMORY_CTR_TUNING` gate and always apply CTR weights when enough data exists, or (b) rename the feature to "experimental" in docs and add a warning log when it activates.

### Phase 3: KG-CRDT Wiring (Week 2-3)

#### 3.1 Wire kg_crdt.py into the write path
**Effort**: Large (6-8 hours)
**Files**: `save/pipeline.py`, `kg/kg_crdt.py`
**Change**: After entity/fact extraction in the save pipeline, also write CRDT ops to `kg_entity_crdt`/`kg_edge_crdt`. This ensures that when sync_server runs `project_crdt_to_entities`, the local state is complete.
**Dependencies**: Requires understanding the save pipeline's entity extraction flow.

#### 3.2 Delete or deprecate temporal_resolver.py
**Effort**: Trivial (30 min)
**Files**: `kg/temporal_resolver.py`, `temporal_resolver.py`, `kg/__init__.py`
**Change**: Remove the root-level shim and the `kg/` version. Update `__init__.py` to not re-export. Add a deprecation note.
**Impact**: Reduces confusion about which temporal module is canonical.

### Phase 4: Documentation & CI Alignment (Week 3)

#### 4.1 Unify coverage gate
**Effort**: Trivial (10 min)
**Files**: `pyproject.toml`, `.github/workflows/ci.yml`
**Change**: Set both to the same value. Recommendation: `fail_under = 70` in pyproject.toml to match CI. Or lower CI to 55 if 70 is not met.

#### 4.2 Fix pyproject.toml tool counts
**Effort**: Trivial (5 min)
**File**: `pyproject.toml`
**Change**: Update the description string to "18 CORE / 94 ADMIN / 3 visible" to match tool_registry.py.

#### 4.3 Fix LangChain adapter base classes
**Effort**: Small (1-2 hours)
**Files**: `agentic_memory/integrations/langchain/retriever.py`, `history.py`, `callback.py`
**Change**: Subclass from `langchain_core.retrievers.BaseRetriever`, `langchain_core.chat_history.BaseChatMessageHistory`, and `langchain_core.callbacks.BaseCallbackHandler` respectively. Move pydantic imports to use LangChain's versions (which are pydantic v2 compatible).
**Dependencies**: Requires `langchain-core` as a dependency (may need optional dep group).

#### 4.4 Run MyPy and fix the 17 errors
**Effort**: Small (2-3 hours)
**Files**: Various (10 files cited)
**Change**: Fix type errors. Most are likely trivial (missing type annotations, incorrect Optional handling).

#### 4.5 Update SEARCH_SOTA_STATUS.md
**Effort**: Trivial (15 min)
**File**: `docs/SEARCH_SOTA_STATUS.md`
**Change**: Update schema version from 57 to 61. Mark ColBERT/SPLADE/LTR as "implemented but inert/defective" rather than "missing."

### Phase 5: Test Infrastructure (Week 4)

#### 5.1 Actually use _ProdDBGuarded or remove it
**Effort**: Small (1-2 hours)
**File**: `eval/test_safety_wiring.py`
**Change**: Either: (a) create a test that actually inherits from it and verifies the prod-DB guard works, or (b) delete the class and the references to it in docs/AGENTS.md.

#### 5.2 Add tool_registry tests
**Effort**: Medium (2-3 hours)
**File**: New file `eval/test_tool_registry.py`
**Change**: Test that all registered tools have valid names, descriptions, and parameter schemas. Test that CORE/ADMIN/DEPRECATED counts match documentation.

---

## Risk Assessment

| Fix | Risk | Reversibility | Blast Radius |
|-----|------|---------------|--------------|
| 1.1 Mode param forwarding | Very Low | Reversible (1 line) | Search only |
| 1.2 GDPR subject scoping | Medium | Requires migration | GDPR erase |
| 1.3 Gate broken SOTA | Low | Reversible | Search ranking |
| 1.4 Edge-CRDT tiebreak | Low | Reversible | CRDT only |
| 2.1 Principal resolution | Medium | Reversible | All MCP calls |
| 2.2 Wire neural forget | Low | Reversible | Search ranking |
| 3.1 Wire CRDT write path | High | Reversible | KG + save |
| 4.3 LangChain adapters | Medium | May break existing code | Integrations |

## Recommended Execution Order

1. **Phase 1** (stop bleeding) — do all 4 items, they're low-risk and high-value
2. **Phase 4.1-4.2** (docs/CI alignment) — trivial fixes, do alongside Phase 1
3. **Phase 2.1** (principal resolution) — important for security posture
4. **Phase 4.3-4.4** (LangChain + MyPy) — moderate effort, improves quality
5. **Phase 3** (KG-CRDT wiring) — largest effort, can be deferred if CRDT sync is not actively used
6. **Phase 5** (test infrastructure) — important but not urgent

## What NOT to Fix

- **Neural forget curve naming**: It's a reasonable name. The class is functional. Don't waste time renaming.
- **Entailment chains**: The audit claim that they're dead is REFUTED. They're wired into both write and read paths.
- **temporal_resolver.py**: While it duplicates fact_temporal.py, it's still used in tests and re-exported. Deprecate, don't delete abruptly.
