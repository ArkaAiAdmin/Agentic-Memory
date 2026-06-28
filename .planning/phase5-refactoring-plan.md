# Phase 5.x — Root-Level Module Refactoring Plan

**Goal**: Move ~70 remaining root-level `.py` files into domain packages, with
shim modules at root for backward compat (same pattern as Phase 5.1).

**Current state**: 117 root-level `.py` files. ~47 already in packages
(`save/`, `search/`, `background/`, `hooks/`, `cron/`, `fact/`,
`knowledge_graph/`, `backfill/`). ~70 remain.

---

## Package Map

```
agentic-memory/
├── background/     ← DONE (Phase 5.1)
├── fact/           ← EXISTS — needs shim for fact_extraction.py
├── backfill/       ← EXISTS — needs shim for backfill_all.py
├── kg/             ← NEW — contradiction_detector + kg_crdt + kg_dedup + kg_traversal
├── infra/          ← NEW — db, fts, cache, locks, config, metrics
├── sync/           ← NEW — sync_server + sync_client
├── crdt/           ← NEW — crdt_field + crdt_merge
├── mcp/            ← OPTIONAL — consolidate 26 mcp_*.py files
├── temporal/       ← SMALL — temporal_resolver + fact_temporal
├── recall/         ← SMALL — recall + search_memory
└── memory/         ← SMALL — memory_delete + memory_sharing + memory_injection
```

---

## Phase 5.2 — Complete `fact/` + `backfill/` packages (1 session)

Already have packages decomposed via god-module surgery. Need root-level
shims and to move a few remaining files.

### `fact_extraction.py` → shim
- **Root**: replace 2,397L monolith with `_ShimModule` shim
- **Real code**: already in `fact/fact_extract.py` + `fact/fact_clean.py` +
  `fact/fact_schema.py` + `fact/fact_search.py`
- **Risk**: low — already proven by god-module split

### Add to `fact/` package
- `fact_temporal.py` (746L) → `fact/fact_temporal.py`
- `consolidate_facts.py` (356L) → `fact/consolidate_facts.py`
- `llm_extraction.py` (945L) → `fact/llm_extraction.py`  
- `llm_providers.py` (508L) → `fact/llm_providers.py`

### `backfill_all.py` → shim
- **Root**: replace 816L with shim
- **Real code**: already in `backfill/index_backfills.py` + `backfill/kg_backfills.py`
- `backfill_orphans.py` (168L) → move into `backfill/`

### Files to shim: 2 (fact_extraction, backfill_all)
### Files to move: 5 (fact_temporal, consolidate_facts, llm_extraction, llm_providers, backfill_orphans)
### Ef fort: Low-Medium (mostly OOTB; llm_extraction reference chains need care)

---

## Phase 5.3 — Create `kg/` package (1 session)

Move everything KG-related into a single package.

### New `kg/` package
- `contradiction_detector.py` (1,259L) → `kg/contradiction_detector.py`
  - Note: depends on `fact_extraction.extract_facts` (which will be shim)
- `kg_crdt.py` (683L) → `kg/kg_crdt.py`
- `kg_dedup.py` (397L) → `kg/kg_dedup.py`
- `kg_traversal.py` (351L) → `kg/kg_traversal.py`

### Root-level shims for each of the 4 files above

Already existing `knowledge_graph/` package stays as-is — it's a
decomposition of the original `knowledge_graph.py` (already removed).

### Files to shim: 4
### Files to move: 4
### Effort: Medium (contradiction_detector has tangled deps on fact_extraction,
             save_pipeline; kg_crdt depends on crdt_field)

---

## Phase 5.4 — Create `infra/` package (1-2 sessions)

Move all infrastructure-level utilities. **Largest phase by file count**
but mechanically simplest (pure utilities, few cross-package deps).

### New `infra/` package
```
infra/
├── __init__.py        ← re-export all public symbols
├── db.py              ← from db.py (788L)
├── db_path_flock.py   ← from db_path_flock.py (296L)
├── db_write_queue.py  ← from db_write_queue.py (415L)
├── db_migrations.py   ← from db_migrations.py (837L)
├── file_lock.py       ← from file_lock.py (141L)
├── fts.py             ← from fts.py (180L)
├── cache.py           ← from cache.py (174L)
├── frontmatter.py     ← from frontmatter.py (104L)
├── memory_config.py   ← from memory_config.py (264L)
├── safe_call.py       ← from safe_call.py
├── exceptions.py      ← from exceptions.py (< 100L)
├── _bootstrap_path.py ← from _bootstrap_path.py
├── _lazy_imports.py   ← from _lazy_imports.py
├── metrics.py         ← from metrics.py (303L)
├── reranker.py        ← from reranker.py (561L)
├── dist_lock.py       ← from dist_lock.py (300L)
├── vector_store.py    ← from vector_store.py (342L)
├── agent_context.py   ← from agent_context.py (209L)
├── embedding_incremental.py  (268L)
├── embedding_recompute.py    (155L)
├── embedding_search.py   (1,245L — large!)
├── pinned_decay.py    (223L)
├── quality_gates.py   (435L)
└── infrastructure.py  (369L — meta module, needs care)
```

### Root-level shims for each file above

### Files to shim: ~22
### Files to move: ~22
### Effort: Medium-High (large number of files, but each is a simple cut-paste;
             embedding_search.py at 1,245L needs care with imports)

---

## Phase 5.5 — Create `sync/` package (1 session)

### New `sync/` package
- `sync_server.py` (1,162L)
- `sync_client.py` (656L)
- `sync_invariant.py` (282L)
- `sync_check.py` (< 100L)

### Root-level shims for each

### Files to shim: 4
### Files to move: 4
### Effort: Low (self-contained, no external deps)

---

## Phase 5.6 — Create `crdt/` package (1 session)

### New `crdt/` package
- `crdt_field.py` (1,110L)
- `crdt_merge.py` (783L)

### Root-level shims for each

### Files to shim: 2
### Files to move: 2
### Effort: Low

---

## Phase 5.7 — Create `recall/` + `temporal/` packages (1 session, optional)

### New `recall/` package
- `recall.py` (586L)
- `search_memory.py` (195L)

### New `temporal/` package
- `temporal_resolver.py` (270L)
- `fact_temporal.py` (746L — if not moved to fact/ in 5.2)

### Root-level shims for each

### Files to shim: 3-4
### Effort: Low

---

## Follow-up Fixes (interleaved with phases)

| # | Fix | Where | Phase |
|---|---|---|---|
| F1 | Deduplicate imports | `mcp_memory.py` | Pre-5.2 |
| F2 | Fix `_PROXIED_DB_NAMES` fragility | `memory_common.py` | Pre-5.2 |
| F3 | Centralize `sys.path.insert` | 6 modules → `infra/bootstrap.py` | 5.4 |
| F4 | Audit bare `except Exception` | `auto_save.py`, `save_pipeline.py`, `mcp_maintenance.py` | 5.4 |
| F5 | Fix `MEMORY_ENHANCED_LOGGING` vs `MEMORY_LEGACY_LOGGING` | `cron/cron_common.py` or wherever both are `True` | 5.4 |
| F6 | Remove overengineered `mdns_discovery.py` | Replace with static config or remove | 5.7 |
| F7 | Centralize cron job logging (no open-code per-job log handlers) | `cron/*.py` | 5.4 |

---

## Migration Order (Recommended)

```
Phase 5.1  ← DONE (background/)
F1 + F2    ← quick wins (duplicate imports, PROXIED_DB_NAMES)
Phase 5.2  ← fact/ + backfill/ (already partially extracted — lowest risk)
Phase 5.3  ← kg/  (moderate risk — tangled deps to fact_extraction)
Phase 5.4  ← infra/ (biggest, but safest — pure utilities)
F3 + F4 + F5  ← interleave with Phase 5.4
Phase 5.5  ← sync/ (self-contained)
Phase 5.6  ← crdt/ (self-contained, but kg_crdt depends on it — do after 5.3)
Phase 5.7  ← recall/ + temporal/ + mdns_discovery fix (lowest priority)
```

---

## Verification Gate Per Phase

Each phase must pass:

```bash
# 1. Full test suite
./venv/bin/python -m pytest eval/ -v --tb=short 2>&1 | tail -5

# 2. No import errors from shims
./venv/bin/python -B -c "from <moved_module> import <public_symbol>"

# 3. Shims preserve __file__ and __name__
./venv/bin/python -B -c "import <root>; print(<root>.__file__); print(<root>.__name__)"

# 4. Mypy
./venv/bin/mypy --strict <moved_module>.py  # from both root and package
```
