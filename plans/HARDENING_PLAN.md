# Corrected Implementation Plan — agentic-memory Hardening

**Basis:** Independent re-verification (2026-07-20) of the prior audit. 4 of 13
prior claims were REFUTED and must NOT be acted on (they are correct behavior
already). This plan only touches CONFIRMED-REAL defects.

## Status of prior audit claims

REFUTED (DO NOT TOUCH — acting would regress):
- CRDT append-only LWW ("C1"): FALSE. `record_entity_add/remove/edge_add`
  (kg/kg_crdt.py:665-755) already use plain INSERT into op-log tables keyed by
  `op_id` AUTOINCREMENT. Already append-only.
- GDPR erasure no-op: FALSE. Tenant-scoped erase works (gdpr.py:68-73,135-141);
  cross-tenant refusal test passes.
- check_permission ignores policies: FALSE. rbac.py:86-115 joins + evaluates
  `policies` with wildcard/prefix.
- Adaptive semantic boost dead mutation: FALSE. fusion.py:155 feeds doubled
  `_sem_w` into RRF.
- ColBERT backfill-only: FALSE. Indexed on normal save path.

CONFIRMED REAL (FIX):
1. SPLADE permanent-load-failure cache kills sparse stage.
2. ColBERT inert in tests (PYTEST forces offline) — doc drift, not a code bug.
3. Two scoring fns never called (dead code).
4. SSM reranker neutral until trained (by design; doc drift only).
5. Contradiction detector reads base `memories` → cross-tenant false positives. [CRITICAL]
6. PageRank full recompute on every save (kg_db.py:493). [HIGH]
7. RBAC admin endpoints authn-only, no authz (api_server.py:1093-1221). [HIGH]
8. Tenant write-path gap: write tenant not validated vs principal. [CRITICAL]
9. GDPR per-subject erase needs default tagging fallback.
10. Communities/betweenness/snapshots have handlers but no cron wires them.
11. Papers: Theorem 2 tautology, orphan-guard drops edges, "age reconciliation"
    undefined.

---

## CHANGE 1 — SPLADE failure-cache (CRITICAL cosmetic, MED function)
**File:** infra/splade_encoder.py
**Fix:** Make the load-failure cache retryable: add an env/flag bypass and a
bounded retry so a transient offline failure does not permanently disable SPLADE.
Also add a deterministic offline fallback encoder so the sparse stage is never
dead in tests/airgapped envs. Keep model string as-is (it is correct).
**Verify:** unit test that load failure does not permanently cache when
MEMORY_SPLADE_RETRY=1; offline fallback produces non-empty splade_tokens.

## CHANGE 2 — Contradiction detector tenant scoping [CRITICAL]
**File:** kg/contradiction_detector.py
**Fix:** `detect_contradictions` (line 479) and any other `FROM memories` query
must accept a `tenant_id` and filter `WHERE tenant_id = ?` (or read the
tenant_memories view). Update cron_resolve_contradictions.py caller +
auto_resolve_contradiction_pair to pass tenant_id consistently.
**Verify:** test with OPENCODE+Agentic Memory IDE notes asserting cross-tenant pairs are
NOT returned.

## CHANGE 3 — PageRank off the save path [HIGH]
**File:** knowledge_graph/kg_db.py
**Fix:** Remove inline `update_graph_analytics(conn)` from index_kg_for_memory
(line 493). Add a `cron_kg_analytics.py` (or reuse cron_kg_backfill) that runs
PageRank + communities + betweenness + snapshot capture on a schedule via
cron/jobs.py registration. This also closes the "communities/snapshots never
wire" gap (CHANGE 10) in one place.
**Verify:** save a memory, assert no PageRank recompute occurs (spy); run the
cron, assert kg_entities.community_id/betweenness populated + graph_snapshots row.

## CHANGE 4 — RBAC admin endpoints authz [HIGH]
**File:** infra/api_server.py
**Fix:** In _handle_rbac_init / _handle_rbac_create_principal /
_handle_rbac_create_role / _handle_rbac_grant / _handle_rbac_revoke /
_handle_acl_add_rule / _handle_acl_delete_rule, call
`mcp_authorize(principal_id, "admin", "rbac", tenant_id=...)` and return 403 if
denied. Bootstrap (init) may stay open only when no principals exist yet.
**Verify:** test that a non-admin token gets 403 on rbac_grant; admin succeeds.

## CHANGE 5 — Tenant write-path validation [CRITICAL]
**File:** mcp_memory.py (memory_save), save/pipeline.py, memory_delete.py
**Fix:** Resolve tenant from the authenticated principal once; pass it through to
save_memory; in the saga/pipeline assert the row's tenant_id == principal tenant
(reject/rewrite mismatch). Apply same to REST _handle_add_memory (api_server.py).
**Verify:** test that principal of tenant-A writing tenant-B is rejected.

## CHANGE 6 — GDPR subject fallback on write
**File:** save/pipeline.py
**Fix:** When data_subject_sub not supplied, default it to the principal sub
(hashed) so per-subject erasure is possible later. Keep tenant-scoped erase.
**Verify:** existing test_gdpr_erase_* still pass; new test that write without
subject still erasable by principal sub.

## CHANGE 7 — RBAC in CI (careful)
**File:** eval/conftest.py
**Fix:** Do NOT blanket-remove MEMORY_AUTH_MODE=open (breaks ~hundreds of tests).
Instead add a fixture `auth ClosedClient` providing a mock admin principal, and
add a SEPARATE CI job running the security suite under MEMORY_AUTH_MODE=closed.
Keep default open for the green suite; gate closed-mode tests explicitly.
**Verify:** new job runs test_security_health_check + new authz tests under closed.

## CHANGE 8 — Dead scoring fns
**File:** search/scoring.py, search/phases/fusion.py, search_pipeline.py
**Fix:** Either wire `_apply_temporal_decay` / `_apply_jaccard_surprise_penalty`
into the post-rank metadata envelope (cheap, honest) OR document them as
explicitly-disabled. Prefer wiring into envelope so docs are true. Remove doc
drift claiming them as active default stages.

## CHANGE 9 — Papers 1 & 2 formalism fixes
**Files:** paper_pipeline/*.md, paper_pipeline_2/*.md, + crdt_projection.py /
ck_crdt.py reference impl
Fixes:
 (a) Define "conflict-free age reconciliation" OR rename the concept to match
     the actual content (structural dedup convergence). Add a definitions
     section.
 (b) Theorem 2 (Paper 1 / Paper 2 §): replace the tautological proof. State
     max(entity_id) as a *convention* (latest-creation tiebreak), not a theorem
     with hand-picked axioms. Re-label as "Selection Convention" not "Theorem".
 (c) Orphan guard: change reference impl (ck_crdt.py:341-346) to REDIRECT
     non-canonical endpoints via the redirect map (matching the production
     resolve_edge_endpoints path) instead of dropping edges; update paper text
     at md:175,180 to "reconciled, not dropped". Re-run benchmark.
 (d) Add a partial-replication convergence test (the actual CRDT hard case the
     papers disclaim) to strengthen the claim, or explicitly scope the theorem
     to full-op-bag convergence.
**Verify:** paper test suites still pass; new partial-replication test added.

---

## Execution order
1. CHANGE 2 (contradiction tenant) — CRITICAL, isolated
2. CHANGE 5 (tenant write validation) — CRITICAL, isolated
3. CHANGE 4 (RBAC admin authz) — HIGH
4. CHANGE 3 (PageRank off save) — HIGH + closes CHANGE 10
5. CHANGE 1 (SPLADE cache) — function
6. CHANGE 6 (GDPR subject fallback)
7. CHANGE 7 (CI auth) — careful, separate job
8. CHANGE 8 (dead scoring fns)
9. CHANGE 9 (papers)

## Verification
- Each change: targeted unit/integration test added.
- Full suite run after each batch: `make test` backgrounded, poll every 30s,
  confirm 0 failures before next batch.
- Run autogen docs (`make update-docs`) before commit per AGENTS.md Rule 24.

## Progress (as of 2026-07-20)

- [x] CHANGE 2 — Contradiction detector tenant scoping [CRITICAL]
      Files: kg/contradiction_detector.py, cron/cron_resolve_contradictions.py,
      mcp_maintenance.py. Test: eval/test_contradiction_tenant_scope.py (3/3).
- [x] CHANGE 5 — Tenant write-path validation [CRITICAL]
      Files: mcp_memory.py, agentic_memory/client.py, infra/api_server.py,
      memory_delete.py. Test: eval/test_tenant_write_path.py (3/3).
- [x] CHANGE 4 — RBAC admin endpoints authz [HIGH]
      File: infra/api_server.py (_require_rbac_admin on APIRequestHandler).
      Test: eval/test_rbac_admin_authz.py (3/3, unit, mocks mcp_authorize).
- [x] CHANGE 3 — PageRank off the save path [HIGH] + closes CHANGE 10
      Files: knowledge_graph/kg_db.py (removed inline update_graph_analytics),
      cron/cron_kg_analytics.py (new: PageRank+betweenness+communities+snapshot),
      cron/jobs.py (daily `kg_analytics`), background/background_worker.py
      (CRON_SCRIPT_MAP + HANDLERS). Also fixed pre-existing LSP errors in
      background_worker.py via isinstance narrowing (no type:ignore).
      Test: eval/test_kg_analytics_off_save_path.py (2/2).
- [x] CHANGE 1 — SPLADE failure-cache [MED function]
      File: infra/splade_encoder.py. Load failure is now retryable (bounded
      budget + MEMORY_SPLADE_RETRY reset); added deterministic offline fallback
      encoder (MEMORY_SPLADE_FALLBACK) so the sparse stage is never dead in
      offline/airgapped/test envs. Real model still loads + encodes (no regress).
      Test: eval/test_splade_failure_cache.py (3/3).
 - [x] CHANGE 6 — GDPR subject fallback on write
       File: save/pipeline.py. `data_subject_sub` added to _MANAGED_COLS so the
       column is no longer silently dropped / flagged as schema drift; threaded
       through SaveRequest -> save_memory -> _persist_via_saga -> _try_saga_persist
       -> _update_memory_index_incremental -> _upsert_memory_row (INSERT + UPDATE).
       When a caller omits data_subject_sub, the save path defaults it to a stable
       PII-free hash of the authenticated principal (fallback tenant_id) so
       per-subject erasure is always possible. Explicit values are respected.
       Test: eval/test_gdpr_subject_fallback.py (3/3).
 - [x] CHANGE 7 — RBAC in CI (separate job; do NOT blanket-disable open auth)
       Added reusable, contamination-safe fixtures in eval/conftest.py:
       closed_auth_env (forces MEMORY_AUTH_MODE=closed), mock_admin_principal
       (migrated temp DB + memory:admin/ops:admin granted), closed_auth_principal
       (activates the admin in agent_context, saves+restores prior state), and
       ClosedClient (AgenticMemoryClient bound to that principal — real
       mcp_authorize path, no mocked authorizer). New eval/test_closed_auth_client.py
       (2/2) exercises save + delete end-to-end under closed mode. A dedicated
       `security-closed-auth` CI job in .github/workflows/ci.yml runs the
       auth/security subset under closed mode; the main `test` job keeps open.
       PRE-EXISTING BUG FIXED ON CONTACT (Rule 17): soft_delete_note / restore_note
       used the invalid SQL `tenant_id = tenant_id()` in their tenant-less branch,
       which matched no rows and made every tenant-less delete/restore a silent
       no-op. Replaced with principal-resolved effective tenant (mirrors
       resolve_tenant_for_principal used elsewhere).
- [x] CHANGE 8 — Dead scoring fns: retained `_apply_temporal_decay` /
       `_apply_jaccard_surprise_penalty` in search/scoring.py as LEGACY
       unit-tested math utilities (marked with docstrings forbidding live
       pipeline use). The true canonical post-rank behavior is the
       order-invariant `temporal_decay` / `jaccard_surprise` envelope fields
       in search/enrichment.py under the RANK-FIRST LOCK (PR1.1). Removed
       doc-drift in scoring.py that implied these were active default stages.
       Added eval/test_search_rank_lock.py::test_envelope_factors_equal_legacy_scoring_math
       proving the envelope factors are numerically equal to the legacy math,
       so "docs are true" (CHANGE 8 intent satisfied via composition, not
       duplication).
       USER DIRECTIVE (post-review): the four factors were computed but NEVER
       consumed — purely cosmetic. Per RANK-FIRST LOCK (PR1.1) we must not
       mutate final_score (the ranking key) after the CE rerank, so we adopted
       OPTION A: search/enrichment.py now also computes a user-visible
       ``display_score = final_score * concept_boost * centrality_boost *
       jaccard_surprise * temporal_decay`` and attaches it to every result
       item. The JSON ``results[].display_score`` is the enriched score the
       user sees; final_score (and result order) is left untouched. Verified
       end-to-end in a live search_memories call and via
       eval/test_search_rank_lock.py::test_display_score_folds_factors_without_changing_order.
 - [x] CHANGE 9 — Papers 1 & 2 formalism fixes (Theorem 2, orphan-guard,
        age reconciliation, partial-replication convergence)
 
 All changes (CHANGE 1-9) are completed, merged into `main`, and pushed
 (root repo `agentic-memory-local` + paper repo `agentic-memory-paper`).
 Branch `feat/hardening-critical` deleted.

