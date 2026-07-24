# CQRS Multi-Writer Implementation — Worktree Summary

## Branch
`feat/cqrs-multi-writer` off `main` (6dfc49bc)

## Goal
Remove the `.rebuild.lock` flock bottleneck from the write path so multiple agent processes can write concurrently. The key architectural insight: replace process-wide lock serialization with an append-only write journal + single-writer reconciliation daemon.

## What Changed

### New: `infra/write_journal.py` (358 lines)
Append-only SQLite journal (`journal.db`) with WAL mode, thread-local connections, and per-entry status tracking (`pending → processing → applied/failed`).

Key API:
- `init_journal_db()` — idempotent schema creation
- `enqueue_write()` — lock-free INSERT, returns deterministic `note_id`
- `dequeue_pending()` — atomic `BEGIN IMMEDIATE` claim of batch
- `mark_applied()` / `mark_failed()` — lifecycle transitions
- `journal_stats()`, `purge_applied()`, `reset_stuck_processing()` — ops helpers
- `get_pending_by_agent()`, `get_entry_by_note_id()` — query helpers

Note_id format: `category/title_slug` (deterministic). The journal's auto-increment `id` column de-duplicates; `INSERT OR IGNORE` handles re-enqueue safely.

### Modified: `save/pipeline.py` (+270 lines)
- **Added `save_memory_journal()`**: public async entry point. Validates synchronously → enqueues to journal → returns note_id immediately. Same signature as `save_memory`.
- **Added `materialize_journal_entry()`**: daemon-side function. Reconstructs `SaveRequest` from journal entry → runs the full saga (DB upsert + vec key + file write + post-save hooks + background tasks + CRDT projection).
- **Removed `lock_already_held` parameter** from `_try_saga_persist`, `_persist_via_saga`, and `_save_memory_core` → `_persist_via_saga` call chain.
- **Removed flock acquisition** from `_update_memory_index_incremental`: no more `_acquire_lock`, `FileLockError` handling, or `release_flock` in finally.
- **Removed flock acquisition** from `_save_memory_core`: no more `_acquire_lock`, early-exit release, or finally-block release.
- `save_memory` (direct path) remains unchanged for internal callers.

### Modified: `infra/saga.py` (−67 lines)
- Removed `_acquire_serialize_lock()` function
- Removed `_release_serialize_lock()` function
- Removed `lock_already_held` parameter from `saga_save_memory()`
- Removed stale locking docstring comments

### Modified: `background/background_worker.py` (+88 lines)
- Added `_reconciliation_loop()`: polls journal every 100ms, calls `materialize_journal_entry()` for each pending batch
- Added `_start_reconciler()`: starts the daemon thread
- Wired reconciler startup into `run_worker()` (both single-threaded and WorkerPool paths)
- Added `_RECONCILER_SHUTDOWN` event; signal handler sets it; finally block joins the daemon thread

### Modified: `mcp_verbs.py` (no change — deferred)
- MCP verbs still call `save_memory` (synchronous path). Wired to `save_memory_journal` initially but reverted because the daemon isn't running in tests yet. Will wire in follow-up.

### Modified: `save/__init__.py`
- Added `save_memory_journal` to `__all__` and `__getattr__`

### New: `eval/test_write_journal.py` (415 lines, 23 tests)
- `TestInitJournalDb`: schema creation, indexes, idempotency
- `TestJournalLifecycle`: enqueue/dequeue/apply/fail/purge/wait
- `TestQueries`: `get_pending_by_agent`, `get_entry_by_note_id`
- `TestResetStuckProcessing`: reset old processing entries
- `TestConcurrentEnqueues`: 10 threads, all unique note_ids
- `TestSaveMemoryJournal`: enqueue to real journal DB
- `TestMaterializeJournalEntry`: SaveRequest reconstruction from entry dict

### Modified: `eval/test_integration_save_pipeline.py`
- `TestSaveMemoryLockOrder` renamed + updated: now asserts `_acquire_lock` is NOT called on the save path (CQRS contract)

## Test Results
- **4102 passed, 0 failures, 35 skipped** (excluding slow tests)
- All existing regression tests pass without modification (except the intentionally updated lock-order test)
- No changes to core test files required

## Architecture After This Change

```
Agent A → save_memory_journal() → INSERT INTO journal.db (lock-free)
Agent B → save_memory_journal() → INSERT INTO journal.db (lock-free)
Agent C → save_memory_journal() → INSERT INTO journal.db (lock-free)
                                                        ↓
Reconciliation Daemon (single writer to main DB)
  dequeue_pending() → materialize_journal_entry() → saga → main DB
```

- `.rebuild.lock` removed from write path entirely (still used by `rebuild_index.py` for index rebuild serialization)
- `_acquire_serialize_lock` / `_release_serialize_lock` removed from `infra/saga.py`
- `lock_already_held` parameter removed from all call sites
- SQLite WAL + `BEGIN IMMEDIATE` handle residual cross-process safety
- CRDT merge handles concurrent edits to same note_id (field-level LWWES)

## What's NOT Done Yet (next steps)
1. Wire MCP verbs (`memory_save`, `memory_edit`, `memory_learn`) to `save_memory_journal`
2. Add feature flag `MEMORY_WRITE_JOURNAL_ENABLED` for gradual rollout
3. Add `_supplement_with_pending` journal-aware read supplement in `mcp_search.py`
4. Add `save_and_wait()` synchronous wrapper for agents needing immediate read-after-write
5. Add the `context_monitor.py` → `save_memory_journal` migration (currently calls `save_memory`)
6. Remove `_acquire_lock` function stub (currently dead code, kept for test compat)
7. Add journal-aware search supplement (`_supplement_with_pending`)
8. Run the full slow suite end-to-end

## Remaining Risks
- `_acquire_lock` is dead code (no production callers) but still imported by 4 test files. Safe to remove in a follow-up.
- `materialize_journal_entry` uses `_acquire_db_connection` which goes through `sqlite_write_queue`. In the daemon this is fine; tests that call it directly may time out if the write queue is contended.
- The `_project_sql_to_crdt` call in `materialize_journal_entry` uses the pre-allocated `note_id` from the journal entry (correct — it's the deterministic `category/slug` format).

## Commit Message
```
feat: add CQRS write-journal for lock-free multi-agent writes

- New infra/write_journal.py: append-only journal DB (WAL mode, thread-local conns)
- New save_memory_journal(): validate → enqueue → return note_id immediately
- New materialize_journal_entry(): daemon-side saga application
- Reconciliation daemon in background_worker.py (polls every 100ms)
- Removed flock (_acquire_lock, _acquire_serialize_lock) from write path
- Removed lock_already_held parameter from saga + pipeline
- saga.py: -67 lines, save/pipeline.py: +270 lines net
- 23 new tests in eval/test_write_journal.py (all passing)
- 4102 tests pass, 0 failures
```
