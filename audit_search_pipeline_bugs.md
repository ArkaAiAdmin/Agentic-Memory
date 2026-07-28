# Search Pipeline Audit — Bug Report

**Repo:** `/Users/arka/.config/agentic-memory`  
**Audit date:** 2026-07-28  
**Files audited:** 16 (orchestrator, phases/*.py, scoring.py, rerankers.py, chunk_index.py, enrichment.py, save/indexers.py, infra/embedding_search.py, memory_mcp.py, search_pipeline.py, etc.)

---

## Summary: 7 bugs found (1 critical, 2 high, 3 medium, 1 low)

---

### BUG 1 — CRITICAL — Tuple index mismatch for `supersedes` in FTS no-rerank path

**File:** `search/orchestrator.py`  
**Lines:** 1610–1619 (fact_lookup branch), 1653–1663 (fts fallback branch)  
**Conflict:** `_build_result_items` in `search/phases/envelope.py` line 94 reads `supersedes = r[12]`, but the no-rerank tuple places `access_count` at index 12 and `supersedes` at index 13.

**What happens:**
```python
# orchestrator.py lines 1610-1619 — FTS no-rerank pass-through:
(
    r[0], r[1], r[2], r[3], r[4], r[5],   # id..fitness
    -r[5], None, None, None,              # rank, final_score, fitness_dup, importance
    r[9] if len(r) > 9 else None,         # last_accessed  [idx 10]
    r[10] if len(r) > 10 else None,       # metadata       [idx 11]
    r[11] if len(r) > 11 else 1,          # access_count   [idx 12]  ←
    None,                                  # supersedes      [idx 13]  ← MISALIGNED
)
```
That tuple has **14 elements** (indices 0–13). `_build_result_items` reads index 12 as `supersedes`, which is the `access_count` value (usually `1`). So FTS-mode results silently report `supersedes=1` (truthy) instead of `None`, which could cause downstream display or dedup logic to falsely treat results as superseded.

**Fix:** Reorder the no-rerank tuple so index 12 is `None` (supersedes) and index 13 is `access_count`:
```python
results_to_display = [
    (
        r[0], r[1], r[2], r[3], r[4], r[5],
        -r[5], None, None, None,
        r[9]  if len(r) > 9  else None,   # last_accessed  [10]
        r[10] if len(r) > 10 else None,   # metadata       [11]
        None,                               # supersedes      [12]  ← FIXED
        r[11] if len(r) > 11 else 1,      # access_count   [13]  ← FIXED
    )
    for r in results
]
```
Both the `fact_lookup` branch (line ~1610) and the `fts` fallback branch (line ~1653) need the same fix. The `_hybrid_fusion` + `_rerank_results` paths are already correct (supersedes at index 12, access_count at index 11).

---

### BUG 2 — HIGH — `_build_result_items` reads `supersedes` from wrong index for 15-element tuples

**File:** `search/phases/envelope.py`  
**Line:** 94  
**Symptom:** The function reads `supersedes = r[12]` for all result tuples, but tuples from `_hybrid_fusion` already contain `access_count` at index 11 and `supersedes` at index 12 — **correct for 15-element tuples from the rerank path.** However, the no-rerank FTS tuples from Bug 1 have a different layout. This bug is a consequence of Bug 1 and should be fixed by fixing the tuple layout in Bug 1 rather than adding index-switching logic here.

**Note:** If additional callers produce tuples with a different layout, a more robust fix would be to add a named tuple or dataclass instead of relying on positional indices. At minimum, add an assertion or length check to catch misaligned tuples early.

---

### BUG 3 — HIGH — Regex `??` (reluctant) instead of `?` (greedy) in value capture patterns causes single-word capture of multi-word values

**File:** `search/orchestrator.py`  
**Lines:** 632–634  
**Pattern:** `(\S+(?:\s+\S+)??)` should be `(\S+(?:\s+\S+)?)`

In Python regex, `?` (single) makes the preceding group **greedy** (prefers 1 match), while `??` makes it **reluctant** (prefers 0 matches). The intent is to capture multi-word values like "version 20.1" or "2.7.18 final". With `??`, the inner non-capturing group `(?:\s+\S+)` is reluctant — it prefers to match zero times, so the outer capture `\S+` alone greedily takes just the first word, and the rest of the value is left uncaptured.

**Example:** For `"Node.js is now version 20.1 released"`:
- **With `??` (current):** captures `"20"` only (or the minimum word). `"1 released"` is not captured.
- **With `?` (fix):** captures `"20.1 released"` (multi-word value).

All three patterns on lines 632–634 have this bug:
```python
rf"{_re.escape(kw)}\s+(?:\w+\s+)?is\s+now\s+(\S+(?:\s+\S+)??)(?:\.|;|\n)",  # line 632
rf"{_re.escape(kw)}\s+was\s+(\S+(?:\s+\S+)??)(?:\.|;|\n)",                  # line 633
rf"changed\s+to\s+(\S+(?:\s+\S+)??)(?:\.|;|\n)",                            # line 634
```

**Fix:** Replace all three `??` with `?`:
```python
rf"{_re.escape(kw)}\s+(?:\w+\s+)?is\s+now\s+(\S+(?:\s+\S+)?)(?:\.|;|\n)",
rf"{_re.escape(kw)}\s+was\s+(\S+(?:\s+\S+)?)(?:\.|;|\n)",
rf"changed\s+to\s+(\S+(?:\s+\S+)?)(?:\.|;|\n)",
```

---

### BUG 4 — MEDIUM — `save/indexers.py` imports from private re-export module `search_pipeline`

**File:** `save/indexers.py`  
**Line:** ~14  
```python
from search_pipeline import _qw5_index_chunks_for
```
`search_pipeline.py` is a private re-export shim (351 lines of re-exports wrapping `search/` modules). The function `_qw5_index_chunks_for` is actually defined in `search/chunk_index.py` and re-exported via `search_pipeline.py`. Importing through the shim adds an unnecessary indirection layer. If `search_pipeline.py` is refactored or renamed (it has no public API contract), `save/indexers.py` breaks silently at import time.

**Fix:** Import directly from the defining module:
```python
from search.chunk_index import _qw5_index_chunks_for
```

---

### BUG 5 — MEDIUM — Hardcoded fallback values in shared_with_me query may populate `supersedes` with integer instead of `None`

**File:** `search/orchestrator.py`  
**Lines:** ~1796–1848 (shared_with_me branch)

The shared_with_me SQL query hardcodes 0.0, 0.0, 0, 1 as the last four columns (rank, final_score, access_count, pinned). When this 14-element row is unpacked and passed to `_build_result_items`, `r[12]` (supersedes) gets the hardcoded `0` (access_count) instead of `None`. The result is that shared memories from other agents silently report `supersedes=0` (falsy but not `None`) instead of `None`.

**Fix:** Pad the shared_with_me row to 15 elements, placing `None` at index 12 (supersedes) and `0` at index 11 (access_count):
```python
SELECT id, content, source_file, tags, created_at,
       importance, category, fitness_score, last_accessed,
       metadata, 0.0, 0.0, 0, NULL, 1   -- rank, final_score, access_count(11), supersedes, pinned
FROM ...
```

---

### BUG 6 — MEDIUM — `_rerank_results` reads `forget_score` from index 12 of FTS results but FTS pass-through puts `access_count` there (compound with Bug 1)

**File:** `search/orchestrator.py`  
**Lines:** ~1440–1450 (inside `_rerank_results`, `_score_result` call)

When `_rerank_results` is called on already-reranked results (the normal path), each result tuple has 15 elements: `[id, content, source_file, tags, created, rank, final_score, fitness, importance, pinned, last_accessed, metadata, access_count, supersedes]`. So `r[12] = access_count` is read as `forget_score`.

This is **not a bug for the rerank path** — `forget_score` in `ScoreContext` is defined but **never read** by `_compute_final_score` in `search/scoring.py` (the function doesn't reference `ctx.forget_score`). So the value is silently ignored. It's dead code / misleading field name, but not a correctness issue.

**Recommendation:** Remove `forget_score` from the result tuple and `ScoreContext` if it is not used, or wire it into `_compute_final_score` if it was intended to participate in scoring.

---

### BUG 7 — LOW — `_detect_ce_query_type` is defined but never called for CE mode selection

**File:** `search/orchestrator.py`  
**Lines:** ~730–780 (function definition)

The function `_detect_ce_query_type` exists and classifies queries as `"single_token"`, `"multi_word"`, or `"multi_phrase"`. However, `_select_ce_mode` (called at line ~1330) always returns `"combined"` when `deep_rerank=False` regardless of query type. The CE mode selection does not use `_detect_ce_query_type` at all. The function is dead code / leftover from a planned feature that was never integrated.

**Recommendation:** Either wire `_detect_ce_query_type` into `_select_ce_mode` for lightweight CE mode selection, or remove `_detect_ce_query_type` to reduce dead code surface.

---

## Verification Notes

| Bug | Verified By |
|-----|-------------|
| 1 | Read orchestrator.py lines 1610–1663; read envelope.py line 94; confirmed index mismatch |
| 2 | Read envelope.py line 94; confirmed single index used for supersedes |
| 3 | Read orchestrator.py lines 632–634; confirmed `??` (reluctant) vs `?` (greedy) |
| 4 | Read save/indexers.py line 14; confirmed private shim import |
| 5 | Read orchestrator.py lines 1796–1848; confirmed hardcoded column layout |
| 6 | Read orchestrator.py `_rerank_results`; read scoring.py `_compute_final_score`; confirmed forget_score unused |
| 7 | Read orchestrator.py `_detect_ce_query_type` definition and `_select_ce_mode` call site; confirmed no wiring |

## Items NOT flagged as bugs

- `infra/embedding_search.py` uses `__getattr__` (make_lazy_getattr) to resolve `_CONTEXTUAL_ENABLED` — this is a deliberate lazy-import pattern documented in `infra/_lazy_imports.py`.
- `save/indexers.py` importing from `search_pipeline` (Bug 4) is a dependency issue, not a correctness bug.
- Regex double-`?` in `is_entailed` check at orchestrator.py ~line 687 — verified to be correct single-`?` (not `??`).
- `_hybrid_fusion` padding of 15-element tuples — verified correct for both FTS and semantic-only paths.
- `_merge_chunk_hits` returns 5-element tuples matching the unpacking in `fusion.py`.
- `_select_ce_mode` always returns "combined" for non-deep mode — intentional design (no query-type differentiation needed without deep rerank).
- SQL injection: shared_with_me query uses parameterized `?` placeholders. No injection risk.

## Files NOT fully audited (read but truncated)

- `infra/embedding_search.py` (>55KB, truncation at model loading section)
- `search/scoring.py` (full read but focus was on forget_score field — may have other issues in temporal decay or SSM logic)
- `search/phases/postprocess.py`, `kg_traversal.py`, `session.py`, `telemetry.py`, `_db_utils.py` — not read yet
