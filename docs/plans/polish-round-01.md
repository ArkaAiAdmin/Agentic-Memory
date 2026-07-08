# Polish Round 01 — Error Handling, Types, Code Consistency

**Status**: Draft  
**Created**: 2026-07-08  
**Author**: Agent session (write_journal enablement follow-up)  
**Scope**: Production code only (eval/ excluded unless explicitly noted)  
**Risk**: Low-Moderate — mostly mechanical transformations, no behavioral changes

---

## Why This Round Exists

The codebase was built in ~30 days by iterating with AI agents. The architecture is
sound, testing is thorough (4,248 tests), and the feature set is deep. But the
AI-generation patterns left visible artifacts:

- `except Exception: pass` used as default error handling (~1,151 production blocks,
  413 of which silently swallow)
- 165 functions without type annotations, 71 `# type: ignore` comments
- Duplicated code (~975 lines in two identical llm_extraction files, 33 copies of
  the same `__dir__` boilerplate)
- Inconsistent naming (`LOG` vs `logger`, `db_` vs `database_`, `idx_` vs `index_`)
- ~15 library modules using `print()` instead of `logging`
- 14 modules exporting `_`-prefixed names in `__all__`

This round fixes all of the above. No new features, no behavioral changes — only
structural quality.

---

## Work Packages

### WP1 — Fix Silent Exception Swallows

**Priority**: Critical  
**Risk**: Low — adding logging never changes control flow  
**Files**: ~173 production files with broad `except` blocks

**Rule**: Every `except Exception` block in production code must either:
1. Catch a **specific exception type** (e.g. `json.JSONDecodeError`, `sqlite3.OperationalError`),
   OR
2. Log at `logger.warning(...)` minimum before swallowing,
   OR
3. Re-raise (acceptable for cleanup guards in `finally`-like contexts)

**Instructions per agent**:

1. For each `except Exception: pass` block:
   - Read 5 lines before `try` and 5 lines after `except` to understand context
   - Identify the operation being protected (JSON parse? File read? DB query? Config lookup?)
   - Replace with the specific exception type where possible
   - Add `logger.warning("...")` with a message describing what failed and `as e` to capture details
   - If truly unable to identify a specific type, use `except Exception as e:` with a warning log
2. Check if file has `logger = logging.getLogger(__name__)` at module level — add if missing
3. Check if `import logging` is present — add if missing
4. For ALTER TABLE / CREATE TABLE IF NOT EXISTS that raises on duplicate column: catch `sqlite3.OperationalError` specifically, add comment `# expected when column exists`

**Top 20 files by volume** (process in parallel):

| Agent | Files | Block count |
|-------|-------|-------------|
| A1 | `search/orchestrator.py` | 48 |
| A2 | `save/pipeline.py` | 45 |
| A3 | `infra/db.py` | 32 |
| A4 | `background/background_worker.py` | 31 |
| A5 | `infra/saga.py` | 26 |
| A6 | `infra/embedding_search.py` | 25 |
| A7 | `background/inbox.py`, `mcp_maintenance.py`, `cli.py` | 24+22+22 |
| A8 | `infra/api_server.py`, `search/scoring.py`, `save/post_save_hooks.py` | 21+20+20 |
| A9 | `mcp_verbs.py`, `llm_extraction.py`, `fact/llm_extraction.py` | 19+18+18 |
| A10 | `mcp_search.py`, `mcp_maintenance_ops.py`, `mcp_memory.py` | 17+17+16 |
| A11 | `context_monitor.py`, `infra/sync_server.py`, `kg/contradiction_detector.py` | 16+16+15 |
| A12 | Remaining ~153 files with <15 blocks each | ~400 |

**Fix-on-contact — known silent NameError risk**: `user_profile.py:41` has an
`except Exception: pass` that can leave `_RECENCY_HALF_LIFE_DAYS` unbound, causing
a `NameError` at first use. Fix: provide a safe default `30` in the except block.

---

### WP2 — Type Annotations + `# type: ignore`

**Priority**: High  
**Risk**: Low — annotations don't change runtime behavior  
**Scope**: 165 untyped production functions + 71 `# type: ignore` comments

**Part A — Fix `# type: ignore` comments** (all 71):

For each comment:
1. Read the suppressed line and surrounding context (look at the 3 lines before)
2. Fix the actual type error rather than ignoring it
3. If the error is `union-attr` on `Optional[X]`, add `if x is not None:` guard before the access
4. If the error is `arg-type`, add `typing.cast()` or fix the annotation at the call site
5. If the error is `no-any-return`, add explicit `-> float | str | ...` return annotation
6. If the error is `import` for a platform-specific module, use `TYPE_CHECKING` guard properly
7. If the error is `assignment`, fix the variable annotation to match the assigned value
8. Truly unfixable cases (e.g. `fcntl = None` on Windows): keep the ignore but add `# reason: platform-conditional import`

**Part B — Add annotations to untyped functions** (all 165):

For each function:
1. Read function body and all `return` statements
2. Infer parameter types from call sites (search with grep)
3. Use `Optional[X]` for `= None` defaults
4. Use generic types (`list[X]`, `dict[str, X]`, `set[X]`) instead of bare `list`, `dict`, `set`
5. Add `-> None` for functions with only side effects
6. For `**kwargs` with known keys, prefer explicit parameters where feasible (check callers first)

**File assignment**:

| Agent | Files | Focus |
|-------|-------|-------|
| B1 | `spaced_repetition.py` (6 funcs), `memory_bootstrap.py` (6 funcs) | 100% untyped, small files |
| B2 | `agentic_memory/integrations/__init__.py` (7 funcs) | 100% untyped, skip test/scratch |
| B3 | `save/post_save_hooks.py` (12 untyped), `backfill/index_backfills.py` (5) | Core integrity hooks |
| B4 | `infra/vector_store.py` — fix 6 `union-attr` ignores | Optional type narrowing |
| B5 | `infra/embedding_search.py` — fix 2 `no-any-return`, `cron/_flock.py` — 3 ignores | |
| B6 | `hooks/memory-proactive-context.py` — 3 `misc` ignores | |
| B7 | `save/pipeline.py` — 9 untyped functions | Core write path |
| B8 | `search/orchestrator.py` — 5 untyped functions | Core read path |
| B9 | `infra/infrastructure.py` — 4 untyped functions, `rebuild_index.py` — 4 | |
| B10 | Remaining ~100 untyped across various files | |

**Rule**: Do NOT add new `# type: ignore` comments. Every existing one must be resolved
or justified with `# reason:`.

---

### WP3 — Code Deduplication

**Priority**: High  
**Risk**: Moderate — file reorganization can break imports; run tests after

**Part A — `llm_extraction.py` dedup** (2 files × ~975 lines, near-identical):

1. Make `llm_extraction.py` (repo root) canonical since it's the older, more imported path
2. Convert `fact/llm_extraction.py` to a thin shim:
   ```python
   # fact/llm_extraction.py
   from llm_extraction import *  # noqa: F401, F403
   ```
3. Search all imports across codebase — update any `from fact.llm_extraction import X` to
   `from llm_extraction import X` (or keep the shim, your choice)
4. Delete duplicate functions from `fact/llm_extraction.py`
5. Run full test suite after

**Part B — `_md5_to_uint64` dedup** (defined in 2 files):

1. Extract to shared utility: `infra/hash_utils.py`
2. Import in both `rebuild_vec_index.py` and `save/pipeline.py`

**Part C — `__dir__` boilerplate dedup** (33 files × ~22 lines):

1. Add helper to `infra/memory_common.py` (or `infra/utils.py`):
   ```python
   def _public_dir(self: object) -> list[str]:
       return [a for a in dir(super(type(self), self)) if not a.startswith("_")]
   ```
2. Replace this pattern in all 33 files:
   ```python
   def __dir__(self):
       return [a for a in dir(super()) if not a.startswith("_")]
   ```
   with:
   ```python
   def __dir__(self):
       return _public_dir(self)
   ```
3. Add the import: `from infra.memory_common import _public_dir`

---

### WP4 — `__all__` + Naming Convention Cleanup

**Priority**: Medium  
**Risk**: Low — `__all__` changes are documentation, renaming is mechanical

**Part A — `__all__` export cleanup** (14 modules, 165+ `_`-prefixed exports):

For each `__all__` that exports `_`-prefixed names:
1. Search for cross-module importers of each `_`-prefixed name
2. If imported externally: rename the function (remove `_` prefix), update all callers
3. If NOT imported externally: keep `_` prefix, remove from `__all__`
4. Modules to clean: `search/__init__.py`, `save/__init__.py`, `infra/memory_common.py`,
   `backfill/__init__.py`, `save/pipeline.py`, `knowledge_graph/__init__.py`,
   `infra/infrastructure.py`, `mcp_common.py`, `infra/cache.py`, `llm_extraction.py`,
   `fact/llm_extraction.py`, `rebuild_index.py`

**Part B — `db_` / `database_` standardization**:

In affected files, rename `database_` → `db_` in Python identifiers only
(not strings, comments, or docstrings). Files: `infra/db.py`, `mcp_maintenance.py`,
`search/orchestrator.py`, `self_directed.py`, `save/backlinks.py`.

**Part C — `idx_` / `index_` standardization**:

In `infra/embedding_search.py`: standardize on `index_` (more descriptive).
In `rebuild_index.py`: already consistent with `idx_`, leave as-is.

---

### WP5 — Logger + `print()` Standardization

**Priority**: Medium  
**Risk**: Low-Moderate — `print()` → `logger` changes output visibility

**Part A — `LOG` → `logger`** (4 files):

Replace `LOG = logging.getLogger(...)` with `logger = logging.getLogger(...)` in:
- `infra/vector_store.py`
- `infra/dist_lock.py`
- `eval_judge.py`
- `docker/cron_runner.py`

Update all `LOG.` references to `logger.` in the same file.

**Part B — Hardcoded logger names → `__name__`** (5 files):

Replace `logging.getLogger("hardcoded_name")` with `logging.getLogger(__name__)` in:
- `okf_export.py` ("okf_export")
- `okf_import.py` ("okf_import")
- `background/daemon.py` ("auto_save.daemon")
- `docker/cron_runner.py` ("agentic_memory.cron_runner") — also in WP5-A
- `infra/config.py` ("agentic_memory.config")

**Part C — Function-level loggers → module-level** (4 files):

Move `logger = logging.getLogger(__name__)` from inside function body to module
scope in:
- `infra/safe_call.py` (line 54 — created on every call)
- `infra/file_lock.py` (line 117 — created on every call)
- `infra/config.py` (line 1676 — has a SEPARATE module-level logger at line 34,
   just delete the inner one and use the module-level logger)
- `infra/memory_config.py` (line 202)

**Part D — `print()` → `logger` in library modules** (exclude CLI entry points):

| File | Replace with |
|------|-------------|
| `infra/metrics_server.py` | `logger.info(...)` |
| `infra/sync_client.py` | `logger.info(...)` |
| `infra/sync_server.py` | `logger.info(...)` |
| `infra/agents_md_generator.py` | `logger.info(...)` |
| `infra/metrics.py` | `logger.info(...)` |
| `infra/pinned_decay.py` | `logger.info(...)` |
| `infra/arc_cache.py` | `logger.info(...)` |
| `infra/config.py` | `logger.info(...)` / `logger.warning(...)` |
| `kg/contradiction_detector.py` | `logger.info(...)` / `logger.debug(...)` |
| `recall/search_memory.py` | `logger.info(...)` |
| `fact/consolidate_facts.py` | `logger.info(...)` |
| `infra/migration_runner.py` | `logger.info(...)` (not interactive CLI) |

**Rule**: Keep `print()` in standalone CLI entry points (`cli.py`, cron scripts,
`__main__` blocks, `eval/` test fixtures). Only library modules that get imported
should use `logger`.

---

### WP6 — Docstring + Dead Code Cleanup

**Priority**: Low  
**Risk**: Low — only comments and dead branches

**Instructions**:

1. Remove docstrings from 1-line functions where the function name already describes
   the behavior (e.g. `def add(a, b): """Add a and b.""" return a + b`)
2. Remove docstrings from `@property` accessors that just restate the attribute name
3. Remove docstrings from private methods (`_name()`) that are trivially obvious
4. Keep docstrings on: public API functions, complex algorithms,
   functions with non-obvious side effects, functions that raise or return meaningful errors
5. Remove dead defensive code: `if x is None: return` when the type annotation says
   `x: str` (not Optional), or when all callers guarantee a value
6. Similarly, `if not x:` guards on parameters that are guaranteed non-empty

**Target files** (sample — expand as found):
- `agentic_memory/models.py` (9 dataclass docstrings, all obvious)
- `kg/kg_crdt.py` (8 trivial docstrings on 1-line functions)
- `kg/contradiction_detector.py` (private method docstrings)
- `kg/contradiction_resolver.py` (private method docstrings)
- `infra/vector_store.py` (distance function docstrings)
- `infra/error_counter.py` (4 zero-value convenience wrappers)

---

### WP7 — Large File Splitting (Optional)

**Priority**: Low  
**Risk**: High — structural change, only if time permits

Candidates:
- `search/orchestrator.py` (2,519 lines) — extract each phase into
  `search/phases/phase_*.py`
- `save/pipeline.py` (2,425 lines) — extract saga orchestration, index updates,
  post-save hooks into separate files under `save/`
- `infra/config.py` (1,693 lines) — not recommended to split (config is inherently
  central)

---

## Execution Strategy

### Parallelism

Run WP1–WP6 as **independent sub-agents per file group**. Each sub-agent:

1. Receives: file list + exact fix instructions for its work package
2. Must `git add` changes in a temporary local stash so conflicts are visible
3. Must run `ruff check` on every file it edits and fix any warnings
4. Must fix bugs on contact (cannot defer — see "fix on contact" rules below)
5. Returns: list of files changed, lines changed, and any unexpected issues found

### Fix-on-Contact Rules

Every sub-agent MUST fix the following without asking:

1. **Bug in the code they're modifying**: If you find a variable that's used before
   assignment, a typo in an identifier, a missing import, or a logic error — fix it
   in the same edit.
2. **Pre-existing silent failure**: If you find an `except Exception: pass` where the
   variable being assigned inside the try is then used outside — ensure a safe default
   is provided.
3. **Circular import you're creating**: If your edit creates a circular import, find
   a different approach (late import, inline the dependency, etc.)
4. **Test that fails because of your change**: Fix the test assertion, don't revert
   the production change.

### Ordering Dependencies

- WP2 (types) is independent of all others
- WP3 (dedup) should run first if touching llm_extraction, since WP1 and WP5 also touch it
- WP4 (naming) depends on WP3 for `llm_extraction` files
- WP5 (logger) is independent
- WP6 (docstrings) is independent

Recommended execution order:
1. WP3 (dedup) → WP4 (naming) — sequential for llm_extraction chain
2. WP1 + WP2 + WP5 + WP6 — parallel (no overlap)
3. WP7 — last, if time permits

---

## Verification

After all work packages complete:

1. **mypy**: `venv/bin/python -m mypy <all modified files>` — zero errors
2. **ruff**: `venv/bin/python -m ruff check <all modified files>` — zero warnings
3. **Tests**: `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/python -m pytest eval/ -q`
   — 0 failures (full suite)
4. **Dry-run maintenance**: `memory_organize(target="safe_default", dry_run=True)` —
   no crashes

---

## Appendix A — Known Fix-on-Contact Items

These were discovered during audit and must be fixed by whichever sub-agent encounters
them:

| File | Issue | Fix |
|------|-------|-----|
| `user_profile.py:41` | `_RECENCY_HALF_LIFE_DAYS` unbound after except | Default to `30` |
| `kg/kg_dedup.py:35` | Import error silently swallowed | Catch `ImportError` specifically |
| `infra/error_counter.py:133-154` | 4 wrappers that just call `get_counter()` | Keep but add docstring noting they exist for convenience |
| `background/tool_complete.py:674` | `__import__` + `and...or` ternary | Replace with proper `import` + conditional |
| `background/tool_complete.py:714-719` | Docstring after import (not recognized by Python) | Move before function body |
| `infra/config.py:196-199` | Redundant `if env_val is not None:` | Simplify to `if env_val:` or keep for clarity |

## Appendix B — Module Layout (Reference)

```
agentic-memory/
├── save/
├── search/
├── infra/
├── background/
├── hooks/
├── cron/
├── kg/
├── fact/
├── crdt/
├── recall/
├── agentic_memory/
├── eval/
├── docs/
│   ├── plans/               ← this file
│   └── ...
└── *.py (root-level modules)
```

## Appendix C — Total Effort Estimate

| WP | Description | Est. file edits | Sub-agents needed | Est. time |
|----|-------------|----------------|-------------------|-----------|
| WP1 | Fix silent swallows | ~150 | 12 | 1-2 hours |
| WP2 | Type annotations | ~40 | 10 | 1-2 hours |
| WP3 | Code dedup | ~37 | 3 | 0.5-1 hour |
| WP4 | `__all__` + naming | ~20 | 2 | 0.5 hour |
| WP5 | Logger standardization | ~20 | 4 | 0.5 hour |
| WP6 | Docstring cleanup | ~30 | 1 | 0.5 hour |
| WP7 | File splitting (optional) | ~5 | 1 | 1-2 hours |
| **Total** | | **~300** | **~33** | **4-8 hours** |
