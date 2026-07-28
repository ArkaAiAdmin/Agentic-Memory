# Search Pipeline Comprehensive Bug Audit Report

Date: 2026-07-28
Scope: All 14 phases of the search pipeline in agentic-memory
Methodology: Sub-agent deep audits + manual verification of critical findings

---

## CRITICAL Bugs (3)

### Bug C1 — Column offset in `_build_result_items` (FTS no-rerank path)
**File:** `search/orchestrator.py`, lines 1610–1619
**Impact:** FTS-mode results (and fact_lookup mode) silently report `supersedes=1` (access_count integer) instead of `None`, causing downstream dedup/suppression logic to falsely treat results as superseded and drop them.

**Root cause:** The no-rerank FTS path builds a 14-element tuple where index 12 = access_count and index 13 = supersedes. But `_build_result_items` (envelope.py:94) reads `r[12]` as `supersedes`. For 14-element FTS tuples, `r[12]` is access_count (integer `1`), which is truthy — falsely marking results as superseded.

**Affected code paths:**
1. FTS mode no-rerank (1610–1619): access_count at idx 12, supersedes at idx 13 (never read)
2. fact_lookup mode no-rerang (same block, 1610–1619, same issue)
3. Rerank failure fallback (1662): raw 13-element SQL rows where idx 12 = score (float, truthy)
4. shared_with_me path (1805–1821): 14-element repacked rows where idx 12 = access_count (0)

**Fix:** All FTS-mode and fact_lookup no-rerank tuples must match the 13-element canonical shape:
```
(idx 12) = None  # supersedes
(idx 13) = removed (access_count not in canonical shape)
```

---

### Bug C2 — Column offset in `_build_result_items` (postprocess.py + enrichment.py)
**Files:** 
- `search/phases/postprocess.py`, line 65
- `search/enrichment.py`, lines 135 and 138

**Impact:** When enrichment/postprocess reads result tuples, it pulls `metadata_json` from the wrong index (index 11 = access_count instead of index 10 = metadata), causing `json.loads(integer)` to fail silently. The metadata's `auto_summary` field is **never surfaced** in formatted output.

**Root cause:** All three files read:
- r[10] as `last_accessed` but the **raw SQL rows** (not reformatted tuples) have last_accessed at index 9
- r[11] as `metadata_json` but raw SQL rows have metadata at index 10
- r[12] as `supersedes` but raw SQL rows have score at index 12

Wait — actually, the _rerank_results no-rerank path already reformats tuples into the canonical 13-element shape (see orchestrator.py lines 327–357). The offset bug only affects:
1. The FTS no-rerank path at orchestrator.py:1610-1619 (access_count at idx 12)
2. The FTS failure fallback at orchestrator.py:1662 (score float at idx 12)
3. The shared_with_me path (access_count at idx 12)

When `_build_result_items` processes these mis-shape tuples, the `last_accessed`, `metadata_json`, and `supersedes` fields are all wrong.

The postprocess.py and enrichment.py bugs are **cascading** from the rerank_results no-rerank path which correctly produces 13-element tuples with supersedes at index 12. Those are already correct because `_rerank_results` builds them properly.

So the offset bugs in **postprocess.py and enrichment.py are NOT bugs** — they correctly read the canonical 13-element shape from `_rerank_results`. The bug is ONLY in the FTS no-rerank path and shared_with_me path in orchestrator.py.

**Corrected assessment:** postprocess.py and enrichment.py are fine. The bug is in orchestrator.py's FTS no-rerank tuple building (lines 1610–1619) and shared_with_me path (lines 1793–1821).

---

### Bug C3 — Temporal filter `valid_to` boundary inconsistency
**File:** `search/orchestrator.py`, lines 1489 and 1500

**Impact:** When `valid_from` column exists, temporal filter uses `valid_to > ?` (exclusive), excluding notes with `valid_to == as_of`. When `valid_from` does NOT exist (older DBs), filter uses `valid_to >= ?` (inclusive), including notes with `valid_to == as_of`. Same time-travel query produces different inclusion/exclusion depending on DB schema.

**Fix:** Use `valid_to > ?` consistently in both branches (exclusive is correct — notes valid UP TO but NOT INCLUDING a date).

---

## HIGH Bugs (4)

### Bug H1 — shared_with_me SQL columns misaligned in `_swm_display_rows`
**File:** `search/orchestrator.py`, lines 1793–1821

**Impact:** The shared_with_me SQL hardcodes 14 trailing values (`0.0, 0.0, 0, 1`) at wrong positions. When repacked into `_swm_display_rows`, the columns don't match the canonical 13-element shape. Index 12 gets `access_count` (0, falsy — safe but wrong type), index 13 gets `pinned` (1) — silently dropped since `_build_result_items` only reads up to index 12.

**Fix:** Restructure the SQL to return only 13 columns matching canonical shape, or repack `_swm_display_rows` with `None` at index 12 (supersedes) and drop the extra column 13.

---

### Bug H2 — `rstrip("s")` strips all trailing 's' characters from KG hyphenated identifiers
**File:** `search/phases/kg_traversal.py`, line 329

**Impact:** `ph.rstrip("s").rstrip("S")` strips ALL trailing 's' characters, not just one plural marker.
- `"addresses"` → `"addresse"` (meaningless)
- `"kiss"` → `"ki"` (meaningless)
- `"classes"` → `"clas"` (meaningless)

**Fix:** Use single-character strip: `ph[:-1] if ph.endswith(('s', 'S')) and len(ph) > 2 else ph`

---

### Bug H3 — Hard-split cursor in chunk_index.py doesn't advance past consumed spans
**File:** `search/chunk_index.py`, lines 243–248

**Impact:** When a single span exceeds `_QW5_CHUNK_MAX_SIZE`, the hard-split loop generates multiple chunks from that span. But after hard-splitting, `cursor += 1` advances by only 1 span index. This is actually **correct** — the cursor is at the hard-split span, and after processing it, it advances by 1 to the next span. The subagent's finding was **false positive** — the hard-split only ever processes the current span, not multiple spans. The outer `while` loop increments `cursor` by 1 after each span, which is the correct behavior.

**Correction:** Not a bug.

---

### Bug H4 — `li_avg_dist` (late-interaction avg distance) never stored in results tuple
**File:** `search/rerankers.py`, line 399

**Impact:** The late-interaction reranker computes `li_avg_dist` but `new_r[13] = li_avg_dist` never executes because `len(new_r) > 13` is False for 13-element FTS tuples. The metric is computed but discarded entirely — dead work that contributes nothing to ranking quality signal.

**Fix:** Either extend tuple to 14 elements (index 13) so li_avg_dist can be stored and used downstream, or remove the dead computation.

---

## MEDIUM Bugs (5)

### Bug M1 — `_apply_cross_encoder_rerank` imported but never called
**File:** `search/orchestrator.py`, line 98

**Impact:** Dead import adds noise, may confuse maintainers writing future code paths.

---

### Bug M2 — `_detect_ce_query_type` is dead code
**File:** `search/rerankers.py`, line 692

**Impact:** Defined but never called. `_select_ce_mode` always returns `"combined"` directly.

---

### Bug M3 — `save/indexers.py` imports through legacy shim module
**File:** `save/indexers.py`, line ~14

**Impact:** `from search_pipeline import _qw5_index_chunks_for` creates a fragile import chain. Direct import `from search.chunk_index import _qw5_index_chunks_for` is more robust.

---

### Bug M4 — `KG entity_ids.add(row[0] if not isinstance(row, sqlite3.Row) else row[0])`
**File:** `search/phases/kg_traversal.py`, line ~431

**Impact:** Both branches of the isinstance check produce identical `row[0]`. Misleading dead code.

---

### Bug M5 — enrichment.py module docstring claims factors multiply into `final_score` but code folds into `display_score` only
**File:** `search/enrichment.py`, module docstring vs lines 79–100

**Impact:** Documentation/code contract mismatch. The RANK-FIRST LOCK design intentionally keeps `final_score` immutable post-CE, but the docstring contradicts this.

---

## LOW Bugs (3)

### Bug L1 — `_entity_name_to_memory_id` called with empty `seen_ids` set in multi-hop traversal
**File:** `search/phases/kg_traversal.py`, line ~732

**Impact:** DB LIKE queries cannot exclude already-matched memory IDs, forcing more rows than necessary. Dedup happens later at line 753 but wastes DB I/O.

---

### Bug L2 — `_temporal_compare` uses inefficient correlated subquery for rowid lookup
**File:** `search/orchestrator.py`, lines ~722–726

**Impact:** `JOIN tenant_memories m ON m.id = (SELECT id FROM tenant_memories WHERE rowid = fts.rowid)` does an unnecessary per-row secondary lookup. `ONS m.rowid = fts.rowid` is direct and equivalent.

---

### Bug L3 — Docstring says 12-tuple but actual tuples are 13 elements (rerankers.py and answer_rerank.py)
**Files:** `search/rerankers.py` (lines 148, 333), `search/answer_rerank.py` (line 138)

**Impact:** Wrong tuple size in documentation, not code correctness.

---

## Confirmed Non-Bugs

- **profile_search.py hardcoded paths** — files `longmemeval_s_cleaned.json` and `longmemeval_oracle.json` exist at `eval/longmemeval_s/`
- **Hard-split cursor advancement** — correctly advances by 1 span per outer loop iteration
- **postprocess.py column indexing** — correctly reads canonical 13-element rerank tuples
- **enrichment.py column indexing** — correctly reads canonical 13-element rerank tuples

---

## Summary

| Severity | Count | Files Affected |
|----------|-------|---------------|
| CRITICAL | 2* | orchestrator.py (FTS no-rerank tuple + shared_with_me + temporal boundary) |
| HIGH | 3 | orchestrator.py, kg_traversal.py, rerankers.py |
| MEDIUM | 5 | orchestrator.py, rerankers.py, chunk_index.py, save/indexers.py, enrichment.py |
| LOW | 3 | kg_traversal.py, orchestrator.py, rerankers.py, answer_rerank.py |

*C2 (postprocess/enrichment) was re-assessed as cascading from C1 (orchestrator no-rerank tuple) and not a separate bug.
