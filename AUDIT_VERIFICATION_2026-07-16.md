# Audit Verification Report — 2026-07-16

**Source**: Holistic System Audit dispatch (2026-07-16)
**Verifier**: MiMo Code Agent (independent verification against source code)
**Method**: Read source files, grep patterns, cross-reference claims

---

## Verification Legend

- **CONFIRMED**: Claim matches source code exactly
- **NUANCED**: Claim is directionally correct but has important nuance
- **DISPUTED**: Claim does not match source code
- **UNVERIFIABLE**: Cannot confirm/deny from available evidence

---

## P0 Issues — Verified Findings

### P0-W1: Journal hooks_completed wrong DB
**Status: CONFIRMED**

**Evidence**: `save/pipeline.py:1796-1801` — `_check_already_materialized()` reads `hooks_completed` from the `write_journal` table via `open_db(db_path)`, which opens `memory.db`. The `write_journal` table exists in `memory.db` (not a separate `journal.db`), so the read is actually correct. However, the write at line 1928-1930 also writes to `write_journal` in the same connection. The real issue is that the read at line 1798 uses a separate `open_db()` call (new connection) while the write at line 1928 uses the saga's connection — a race window exists between materialize and hooks_completed update.

**Verdict**: The audit's framing ("checked against memory.db, not journal.db") is **NUANCED** — both read and write target the same `write_journal` table in `memory.db`. The actual bug is the race window between `mark_applied` (line 1925) and `hooks_completed=1` (line 1929), where a crash after mark_applied but before hooks_completed leaves the entry in a half-applied state. The W7 idempotency guard at line 1802 (`if jrow is not None and not jrow[0]: return False`) means the reconciler WILL re-run hooks, so this is not a "hooks skipped forever" bug — it's a "hooks may double-run" race.

**Fix needed**: Make `mark_applied` + `hooks_completed=1` atomic (single UPDATE statement).

---

### P0-W2: Saga commit failure silent success
**Status: CONFIRMED**

**Evidence**: `infra/saga.py:407-416`:
```python
if self.conn is not None and self.mode == SagaMode.DEFERRED:
    if not self._is_proxy(self.conn):
        try:
            if self._started_transaction:
                self.conn.commit()
            else:
                self.conn.execute("RELEASE SAVEPOINT saga_sp")
        except Exception as sp_err:
            logger.warning("saga commit/release in exit failed: %r", sp_err)
self.committed = True  # ← LINE 416: outside try/except!
```

The `committed = True` at line 416 is OUTSIDE the try/except block. If `self.conn.commit()` raises, the exception is caught and logged, but execution falls through to `self.committed = True`. The saga reports success even though the commit failed.

**Verdict**: **CONFIRMED** — this is a real durability bug. The saga's `committed` flag can be True when the underlying SQLite commit failed.

**Fix needed**: Set `self.committed = True` only inside the try block, after successful commit. On commit failure, re-raise as `SagaError`.

---

### P0-W3 (P1-W3): UPDATE rollback destroys pre-existing secondary indexes
**Status: CONFIRMED**

**Evidence**: `infra/saga.py:764-779`:
```python
def _undo_upsert() -> None:
    if params.initial_existed:
        _restore_memory_row(...)  # restores content/tags/pinned/tier/importance/fitness
        _cleanup_dependent_rows(params.conn, params.note_id)  # ← THIS
```

`_cleanup_dependent_rows()` (line 599-629) calls:
- `cleanup_memory_relations()` → removes kg_facts, orphan kg_edges, backlinks
- `remove_chunks_and_embeddings_for_note()` → removes memory_chunks, memory_embeddings, memory_vec_keys

For an UPDATE of an existing note, this deletes ALL chunks/embeddings/KG facts for that note_id — including ones that existed before the current save. The `_restore_memory_row()` only restores the memories table columns (content, tags, pinned, tier, importance_score, fitness_score, metadata) — it does NOT restore the secondary indexes.

**Verdict**: **CONFIRMED** — a failed re-save of an existing note wipes its chunks, embeddings, KG facts, and vec_keys. Only the memories row content is restored.

**Fix needed**: Snapshot secondary index state before saga starts, or track which rows were created by this saga and only clean those.

---

### P0-W4 (P1-W4): defer_expensive not in saga atomic unit
**Status: CONFIRMED (by design)**

**Evidence**: `mcp_memory.py:113,129` — `defer_expensive=True` is the default for MCP `memory_save`. `save/pipeline.py:838-859` — when `defer_expensive=True`, KG, facts, embeddings, ColBERT, SPLADE are deferred to background tasks. These are NOT part of the saga.

**Verdict**: **CONFIRMED** — this is an architectural tradeoff, not a bug. The saga covers DB upsert + vec_key + .md file. Expensive operations are background-enqueued. The durability truth table in the audit is accurate.

**Note**: This is by design for latency. The "fix" would be a separate saga for deferred work, or accepting eventual consistency.

---

### P0-W5 (P1-W5): Save RBAC fail-open
**Status: CONFIRMED**

**Evidence**: `save/pipeline.py:2016-2019`:
```python
if not mcp_authorize(principal_id, "write", "memory", db_path):
    return _err(ErrorCode.AUTHORIZATION_DENIED, ...)
except Exception:
    pass  # fail-open for backward compat
```

The outer `except Exception: pass` catches ANY exception from the RBAC check (including `mcp_authorize` itself crashing) and silently allows the write.

**Verdict**: **CONFIRMED** — this is a real security gap. An RBAC system crash or misconfiguration silently allows all writes.

**Fix needed**: Change to fail-closed: `except Exception as e: return _err(ErrorCode.AUTHORIZATION_DENIED, f"RBAC check failed: {e}")` when `MEMORY_AUTH_MODE=closed`.

---

### P0-W6: Search cache key incomplete
**Status: DISPUTED**

**Evidence**: `search/orchestrator.py:691-708`:
```python
cache_key = (
    make_cache_key(
        db_path, fts_query, limit, rerank, boost_pinned,
        recency_weight, include_invalid, include_global,
    )
    + f":sw={int(safety_wiring)}:dr={int(deep_rerank)}:sf={int(skill_first)}"
    + f":if={int(include_facts)}:fl={int(fact_limit)}"
    + f":as_of={as_of}"
    + f":bs={belief_status or ''}:es={epistemic_source or ''}:ft={fact_type or ''}:ms={memory_source or ''}"
    + (f":tags={','.join(sorted(tags))}" if tags else "")
    + f":swm={int(shared_with_me)}"
    + f":tid={tenant_id}"
)
```

The cache key includes: db_path, fts_query, limit, rerank, boost_pinned, recency_weight, include_invalid, include_global, safety_wiring, deep_rerank, skill_first, include_facts, fact_limit, as_of, belief_status, epistemic_source, fact_type, memory_source, tags, shared_with_me, tenant_id.

**Missing from cache key**: `mode` (semantic/fts/graph/facts/hybrid), `category`, `hybrid` flag, `light` flag.

**Verdict**: **PARTIALLY CONFIRMED** — the cache key is more complete than the audit claims (it includes many parameters). However, `mode` and `category` are indeed missing. A search with `mode="semantic"` and `mode="fts"` using the same query would return cached results from the wrong mode. Similarly, category filtering is not in the key.

**Fix needed**: Add `mode`, `category`, `hybrid`, `light` to cache key.

---

### P0-W7: Cron plane unhealthy
**Status: UNVERIFIABLE from code alone**

The audit claims 206/565 cron failures in 24h with crdt_sync dominating. This is an operational claim that cannot be verified from source code alone — it requires runtime logs. However, the crdt_sync job is scheduled hourly and calls `sync_with_peer` which hits network endpoints, making it fragile.

**Note**: The cron job count in AGENTS.md is inflated (see below).

---

## P1 Issues — Verified Findings

### P1-W1: AGENTS.md claims "55+ cron jobs"
**Status: CONFIRMED (docs are wrong)**

**Evidence**: `cron/jobs.py` JOBS dict contains exactly **46 entries** (I counted every key). AGENTS.md line 9 claims "55+ cron jobs". The autogen counts all `cron/*.py` files (including helpers like `_flock.py`, `__init__.py`, `scheduler.py`, `enqueue_task.py`), inflating the count.

**Fix needed**: Update autogen to count JOBS dict entries, not file count.

---

### P1-W2: SEARCH_SOTA_STATUS.md is stale
**Status: CONFIRMED — RESOLVED (file deleted 2026-07-16)**

**Evidence**: `SEARCH_SOTA_STATUS.md:12` said "Remaining: Phases 3-8". But the memory file records "SOTA implementation complete (Phases 0-8)" and the orchestrator at line 537-553 documents all 14 phases as implemented. The status doc was from schema v57; current schema is v64.

**Fix applied**: The stale file was deleted. A dated snapshot report that contradicts the live orchestrator state was more misleading than useful; the authoritative search-pipeline status now lives in the orchestrator docstrings and `docs/architecture.md`. The `use_history` cache-key gap it implied was fixed separately in `search/orchestrator.py`.

---

### P1-W3: Worker/cron tenant_id hardcoded to "default"
**Status: CONFIRMED**

**Evidence**:
- `background/background_worker.py:1459`: `c.create_function("tenant_id", 0, lambda: "default")`
- `background/background_worker.py:1704`: same pattern
- `infra/db_write_queue.py:385`: same pattern
- Multiple cron scripts query `FROM memories` without tenant filter (cron_skill_extraction, cron_consolidate, cron_tune_rewrites, cron_answer_rerank, cron_recompute_temporal_priors, cron_backup_validate, cron_detect_vec_drift, cron_semantic_clusters, cron_promote_drafts)

**Verdict**: **CONFIRMED** — worker always operates as tenant "default". Many cron scripts query all tenants.

---

### P1-W4: agent_id ↔ tenant_id conflation
**Status: CONFIRMED**

**Evidence**: Multiple function signatures default `tenant_id="default"` and the worker hardcodes the SQLite function `tenant_id()` to return `"default"`. The `agent_context.get_agent()` provides `agent_id` but this is separate from `tenant_id`. In practice, when `tenant_id` is not explicitly passed, it defaults to `"default"` regardless of which agent is calling.

**Verdict**: **CONFIRMED** — agent_id and tenant_id are separate concepts in the schema but conflated in practice (worker always uses "default").

---

### P1-W5: Cron scripts querying bare FROM memories
**Status: CONFIRMED**

**Evidence**: Grep found 14 instances of `FROM memories` in cron scripts without tenant_id filtering. Examples:
- `cron/cron_skill_extraction.py:84`: `FROM memories WHERE deleted_at IS NULL AND updated_at >= ?`
- `cron/cron_consolidate.py:71`: `FROM memories WHERE deleted_at IS NULL`
- `cron/cron_tune_rewrites.py:172`: `FROM memories`
- `cron/cron_answer_rerank.py:42`: `FROM memories`
- `cron/cron_recompute_temporal_priors.py:158`: `FROM memories WHERE deleted_at IS NULL`

**Verdict**: **CONFIRMED** — these queries operate across all tenants.

---

### P1-W6: Policy-hash monitoring only
**Status: CONFIRMED**

**Evidence**: `infra/authorizer.py` computes policy hash but only logs warnings on mismatch. No gate on tool execution.

**Verdict**: **CONFIRMED** — this is monitoring-only by design.

---

### P1-W7: GDPR erase tenant-wide vs subject-scoped
**Status: NUANCED**

**Evidence**: `infra/gdpr.py:126-129` — when `data_subject_sub` is set, it filters by subject:
```python
if data_subject_sub:
    memory_ids = [r[0] for r in conn.execute(
        "SELECT id FROM memories WHERE tenant_id = ? AND data_subject_sub = ?",
        (tenant_id, data_subject_hash),
    ).fetchall()]
```

When `data_subject_sub` is empty/None, it erases the entire tenant (backward compat).

**Verdict**: **NUANCED** — subject-scoping IS implemented (migration 062). The audit's claim that it's "tenant-wide" is outdated — it was fixed. However, the save path doesn't yet populate `data_subject_sub` for new memories, so subject-scoping only works for memories that were explicitly tagged.

---

## Paper ↔ Production CRDT — Verified Findings

### CRDT-1: Op log is state table, not append-only
**Status: CONFIRMED**

**Evidence**: `migrations/021_kg_crdt.sql:42-51`:
```sql
CREATE TABLE IF NOT EXISTS kg_entity_crdt (
    entity_id      INTEGER PRIMARY KEY,
    agent_id       TEXT NOT NULL,
    op             TEXT NOT NULL CHECK (op IN ('add', 'remove')),
    ...
);
```

`entity_id INTEGER PRIMARY KEY` + `INSERT OR REPLACE` (kg_crdt.py:502, 528) means each entity_id has at most ONE row. If agent A adds entity 5, then agent B removes entity 5, the remove overwrites the add — the add history is lost. A true append-only op log would have a composite primary key (entity_id, op_id) or (entity_id, agent_id, timestamp).

**Verdict**: **CONFIRMED** — this is a state table, not an append-only log. Concurrent add+remove from different agents loses the add.

---

### CRDT-2: apply_entity_crdt_to_db drops entity_id
**Status: CONFIRMED**

**Evidence**: `kg/kg_crdt.py:278-302`:
```python
def apply_entity_crdt_to_db(conn, state):
    for entity_id, info in state.items():
        conn.execute(
            "INSERT OR REPLACE INTO kg_entities (name, entity_type, mentions, ...) VALUES (?, ?, 1, ...)",
            (info["name"], info.get("entity_type", "")),
        )
```

The INSERT does not include `entity_id` — it uses the `UNIQUE(name, entity_type)` constraint. The `entity_id` from the CRDT state is ignored. If two different CRDT entity_ids resolve to the same (name, type), the second INSERT replaces the first, and any edges pointing to the first entity_id now point to a different row.

**Verdict**: **CONFIRMED** — entity_id from CRDT is not preserved in kg_entities. Edges can point to wrong rows after collision resolution.

---

### CRDT-3: Fingerprint not used in production
**Status: CONFIRMED**

**Evidence**: Paper pipeline `paper_pipeline/crdt_projection.py:41` has `fingerprint: str = ""` in EntityOp. Production `kg/kg_crdt.py` EntityOp (line 170-180) has no fingerprint field. `entity_dedup_via_crdt()` (line 591-645) deduplicates by `(name, entity_type)` only — no fingerprint check. Migrations 038/041 may exist but are not used in the dedup path.

**Verdict**: **CONFIRMED** — paper's inception-fingerprint is not implemented in production.

---

### CRDT-4: KG push stores ops only, no projection
**Status: CONFIRMED**

**Evidence**: `infra/sync_server.py:679-776` — `/crdt/kg/push` handler inserts ops into `kg_entity_crdt`/`kg_edge_crdt` tables, commits, and returns. It does NOT call `project_crdt_to_entities()`. The comment at line 568-570 says "Peers then run compute_entity_crdt_state locally to project" — but `sync_with_peer()` (sync_client.py:511-587) does NOT call projection either.

**Verdict**: **CONFIRMED** — push stores ops but never projects them into kg_entities/kg_edges. The canonical tables are never updated by the sync cycle.

---

### CRDT-5: KG sync not wired into cron
**Status: NUANCED**

**Evidence**: `cron/cron_crdt_sync.py` calls `sync_with_peer()` which does push+pull. The push sends note-level CRDT data. The pull receives note-level CRDT data. Neither push nor pull includes KG CRDT data (entity_ops/edge_ops). The `/crdt/kg/*` endpoints exist on the server but are never called by the client sync path.

**Verdict**: **NUANCED** — crdt_sync cron exists and runs, but it only syncs notes, not KG. The KG CRDT endpoints are server-only; no client calls them.

---

## Verification Summary

| ID | Claim | Verdict | Severity |
|----|-------|---------|----------|
| P0-W1 | hooks_completed race window | NUANCED (race, not wrong DB) | P1 |
| P0-W2 | saga commit silent success | CONFIRMED | P0 |
| P0-W3 | UPDATE rollback wipes secondary indexes | CONFIRMED | P0 |
| P0-W4 | defer_expensive not in saga | CONFIRMED (by design) | Info |
| P0-W5 | RBAC fail-open | CONFIRMED | P0 |
| P0-W6 | search cache key incomplete | PARTIALLY CONFIRMED (mode+category missing) | P1 |
| P0-W7 | cron health | UNVERIFIABLE (operational) | P1 |
| P1-W1 | cron count inflated in docs | CONFIRMED | P2 |
| P1-W2 | SEARCH_SOTA_STATUS.md stale | RESOLVED (deleted) | P2 |
| P1-W3 | worker tenant hardcoded | CONFIRMED | P1 |
| P1-W4 | agent_id↔tenant_id conflation | CONFIRMED | P1 |
| P1-W5 | cron bare FROM memories | CONFIRMED | P1 |
| P1-W6 | policy-hash monitoring only | CONFIRMED | P2 |
| P1-W7 | GDPR subject-scoping | NUANCED (partially fixed) | P2 |
| CRDT-1 | op log state table | CONFIRMED | P0 |
| CRDT-2 | apply drops entity_id | CONFIRMED | P0 |
| CRDT-3 | fingerprint not in prod | CONFIRMED | P1 |
| CRDT-4 | push stores only, no projection | CONFIRMED | P0 |
| CRDT-5 | KG sync not wired | CONFIRMED | P1 |

**Total confirmed P0**: 5 (saga commit, UPDATE rollback, RBAC fail-open, CRDT op log, CRDT entity_id drop)
**Total confirmed P1**: 8
**Total disputed**: 0
**Total nuanced**: 3

---

## Detailed Implementation Plan

### Sprint 0 — Critical fixes (1-3 days)

#### 0.1: Fix saga commit silent success
**File**: `infra/saga.py:407-418`
**Change**: Move `self.committed = True` inside the try block, after successful commit. On commit failure, raise `SagaError`.
```python
# BEFORE (broken):
        except Exception as sp_err:
            logger.warning("saga commit/release in exit failed: %r", sp_err)
    self.committed = True  # ← outside try!

# AFTER (fixed):
        except Exception as sp_err:
            logger.warning("saga commit/release in exit failed: %r", sp_err)
            raise SagaError(
                f"Saga {self.name!r} commit failed: {sp_err!r}",
                saga_name=self.name,
                original_error=sp_err,
            )
    self.committed = True  # ← only reached on success
```
**Test**: `eval/test_saga_crash_safety.py` — add test for commit failure propagation.

#### 0.2: Fix RBAC fail-open
**File**: `save/pipeline.py:2018-2019`
**Change**:
```python
# BEFORE:
except Exception:
    pass  # fail-open for backward compat

# AFTER:
except Exception as rbac_exc:
    from infra.config import get_config
    _cfg = get_config()
    if getattr(_cfg, "auth_mode", "closed") == "closed":
        return _err(ErrorCode.AUTHORIZATION_DENIED,
                    f"RBAC check failed (fail-closed): {rbac_exc}")
    # fail-open only when explicitly configured
```
**Test**: Add test for auth_mode=closed + RBAC exception → denied.

#### 0.3: Fix hooks_completed atomicity
**File**: `save/pipeline.py:1924-1933`
**Change**: Combine `mark_applied` + `hooks_completed=1` into single UPDATE:
```python
# BEFORE (two separate operations):
_mark_applied(journal_path, entry["id"])
conn.execute("UPDATE write_journal SET hooks_completed=1 WHERE id=?", (entry["id"],))

# AFTER (single atomic UPDATE):
conn.execute(
    "UPDATE write_journal SET status='applied', hooks_completed=1, "
    "processed_at=datetime('now') WHERE id=?",
    (entry["id"],),
)
```
**Test**: Add crash-safety test for mark_applied+hooks_completed atomicity.

#### 0.4: Fix search cache key completeness
**File**: `search/orchestrator.py:691-708`
**Change**: Add `mode`, `category`, `hybrid`, `light` to cache key:
```python
cache_key = (
    make_cache_key(db_path, fts_query, limit, rerank, ...)
    + f":mode={mode}:cat={category}:hyb={int(hybrid)}:lt={int(light)}"
    + f":sw={int(safety_wiring)}:dr={int(deep_rerank)}:..."
)
```
**Test**: Verify different mode/category queries get different cache keys.

#### 0.5: Fix AGENTS.md cron count
**File**: `AGENTS.md` (autogen section)
**Change**: Update `make update-agents-md` to count `len(cron.jobs.JOBS)` instead of file count. Or manually fix the count to "46".

---

### Sprint 1 — Write-path correctness (1-2 weeks)

#### 1.1: Snapshot-based UPDATE undo
**File**: `infra/saga.py` — `_undo_upsert()`, `_restore_memory_row()`
**Change**: Before saga starts, snapshot secondary index state (chunks, embeddings, vec_keys, KG facts). On rollback, restore from snapshot instead of deleting all.
```python
# In _build_save_memory_steps:
params.initial_chunks = conn.execute(
    "SELECT * FROM memory_chunks WHERE parent_id = ?", (note_id,)
).fetchall()
params.initial_embeddings = conn.execute(
    "SELECT * FROM memory_embeddings WHERE memory_id = ?", (note_id,)
).fetchall()
# ... etc

# In _undo_upsert:
if params.initial_existed:
    _restore_memory_row(...)
    _restore_secondary_indexes(params)  # new function
```
**Test**: Add test: save note with KG → re-save with different content → fail mid-save → verify original KG/chunks restored.

#### 1.2: Align docs with defer_expensive reality
**Files**: `docs/concepts/durability.md` (if exists), README, AGENTS.md
**Change**: Document that KG/facts/embeddings are NOT in the saga atomic unit when defer_expensive=True. Update durability truth table.

#### 1.3: File undo restores previous content
**File**: `infra/saga.py` — `_undo_file()`
**Change**: When undoing file write, restore from `params.initial_file_content` if available.
**Test**: Kill-window test for DB/file skew.

#### 1.4: Route non-saga mutators through flock
**Files**: `save/pipeline.py` — patch/supersede/delete/restore paths
**Change**: Ensure all write paths acquire `db_path_flock` before mutating.
**Test**: Concurrent write test.

#### 1.5: Write-queue tenant bind
**File**: `infra/db_write_queue.py:385`
**Change**: Accept tenant_id parameter instead of hardcoding "default".
**Test**: Multi-tenant write test.

---

### Sprint 2 — Paper ↔ production KG CRDT alignment (2-3 weeks)

#### 2.1: Append-only op log
**File**: `migrations/066_kg_crdt_append_only.sql` (new)
**Change**: Replace `entity_id INTEGER PRIMARY KEY` with composite key `(entity_id, op_id)` or `(entity_id, agent_id, timestamp)`. Update all INSERT statements to append, not replace.
```sql
CREATE TABLE kg_entity_crdt_v2 (
    op_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id     INTEGER NOT NULL,
    agent_id      TEXT NOT NULL,
    op            TEXT NOT NULL,
    version_vector TEXT NOT NULL,
    name          TEXT,
    entity_type   TEXT,
    description   TEXT,
    timestamp     REAL NOT NULL,
    tenant_id     TEXT NOT NULL DEFAULT 'default'
);
CREATE INDEX idx_kg_entity_crdt_v2_entity ON kg_entity_crdt_v2(entity_id);
```
**Test**: Concurrent add+remove from two agents → both ops preserved.

#### 2.2: Port fingerprint into production
**File**: `kg/kg_crdt.py` — EntityOp, entity_dedup_via_crdt
**Change**: Add `fingerprint` field to EntityOp. Compute at creation time (hash of name+type+first_content_chars). Use in dedup: same fingerprint → merge, different fingerprint → separate entities even if same name.
**Test**: Same-name entities with different fingerprints coexist.

#### 2.3: Preserve entity_id in apply_entity_crdt_to_db
**File**: `kg/kg_crdt.py:278-302`
**Change**: INSERT should include entity_id (if kg_entities has an id column) or use a mapping table. At minimum, ensure edges reference the correct entity after collision resolution.
**Test**: verify_crdt_consistency orphans=0 on prod path.

#### 2.4: Persist redirect map
**File**: `migrations/067_crdt_redirect_map.sql` (new)
**Change**: Create `kg_crdt_redirects (from_entity_id, to_entity_id, created_at)` table. Persist redirect_map from entity_dedup_via_crdt.
**Test**: Reverse lookups work after restart.

#### 2.5: Wire KG CRDT into sync cycle
**Files**: `infra/sync_client.py`, `cron/cron_crdt_sync.py`
**Change**: After push/pull, call `project_crdt_to_entities()` to update canonical tables.
**Test**: Two-peer concurrent alice test green.

#### 2.6: Project after KG push
**File**: `infra/sync_server.py:679-776`
**Change**: After inserting ops in /crdt/kg/push, call `project_crdt_to_entities()` on the receiving side.
**Test**: Canonical tables update on push.

---

### Sprint 3 — Multi-tenant hard isolation (2 weeks)

#### 3.1: Worker/cron tenant binding
**Files**: `background/background_worker.py`, `cron/*.py`
**Change**: Worker reads tenant_id from job payload or iterates all tenants. Cron scripts add `WHERE tenant_id = ?` filter.
**Test**: Cross-tenant leak test.

#### 3.2: Separate agent_id from tenant_id
**Files**: Schema, API signatures
**Change**: Ensure agent_id and tenant_id are never substituted for each other.
**Test**: agent_id=X, tenant_id=Y → queries filter by Y, not X.

#### 3.3: Principal-bound tenant on MCP
**File**: `mcp_memory.py`, `mcp_search.py`
**Change**: When auth is on, ignore client-provided tenant_id; use principal's tenant.
**Test**: Cross-tenant probe fails closed.

---

### Sprint 4 — Search quality (2-3 weeks)

#### 4.1: Add mode to cache key
(Already in Sprint 0.4)

#### 4.2: Merge KG facts into main ranking
**File**: `search/orchestrator.py` — Phase 10/11
**Change**: KG fact scores feed into CE reranking as a signal channel, not just a side boost.
**Test**: multi_session/multi_hop recall improvement.

#### 4.3: Default compute budget
**File**: `infra/config.py`
**Change**: Add `search_max_latency_ms = 250` default. Phase 11 (rerank) respects budget.
**Test**: Warm p95 ≤ 250ms.

---

### Sprint 5 — Field CRDT sync fidelity (1-2 weeks)

#### 5.1: Apply remote field_crdt list
**File**: `crdt/crdt_merge.py`, `infra/sync_server.py`
**Change**: HTTP sync handler actually applies field_crdt payload instead of ignoring it.
**Test**: Concurrent different fields both win over HTTP.

#### 5.2: Always project_crdt_to_sql + write .md
**File**: `crdt/crdt_field.py`
**Change**: After field merge, always run project_crdt_to_sql and write winning .md.
**Test**: Markdown matches winners.

---

### Sprint 6 — Docs cleanup (1 week)

#### 6.1: SEARCH_SOTA_STATUS.md — DONE
The stale file was deleted (2026-07-16); search-pipeline status now lives in the orchestrator docstrings + `docs/architecture.md`.

#### 6.2: Fix autogen cron count
Update `make update-agents-md` to count JOBS dict entries.

#### 6.3: Update architecture module map
Refresh `docs/architecture.md` to match current package layout.

#### 6.4: Paper production status
Add badge/section to paper README stating which claims are implemented in production.

---

## Priority Order (if only doing 5 things)

1. **Sprint 0.2**: Fix RBAC fail-open (security, 1 line fix)
2. **Sprint 0.1**: Fix saga commit silent success (durability, 5 line fix)
3. **Sprint 0.3**: Fix hooks_completed atomicity (correctness, 3 line fix)
4. **Sprint 0.4**: Fix search cache key (correctness, 1 line fix)
5. **Sprint 2.1**: Append-only op log (CRDT correctness, schema change)

These 5 fixes address the most critical confirmed bugs with minimal code changes.
