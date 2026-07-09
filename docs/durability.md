# Durability Contract — save_memory Guarantees

## What `save_memory` Guarantees

When `save_memory(...)` returns a `note_id` string (not an `"Error [...]"`), all
stores are durable:

| Store | Durability mechanism |
|-------|---------------------|
| **SQLite DB** | Committed within the saga transaction; crash-consistent via WAL mode |
| **Vec key** | INSERT into `memory_vec_keys` committed in the same saga step |
| **Markdown file** | Written atomically via `safe_atomic_write` (write to `.tmp`, `os.rename`) |
| **FTS5 index** | Updated in the same saga step as the DB row |
| **KG entities/edges** | Extracted and committed in the same saga step |
| **KG facts** | Extracted and committed in the same saga step |
| **Backlinks** | Updated in the same saga step |

## Write Paths

### Direct Path (`save_memory`)

The direct path writes to all stores synchronously within a saga transaction:

```
save_memory()
    → acquire flock (memory/.write.lock)
    → begin saga
    → Step 1: upsert DB row + FTS5 + chunks + embeddings + KG + facts + backlinks
    → Step 2: insert vec_key row
    → Step 3: atomic write .md file
    → commit saga
    → release flock
    → return note_id
```

### CQRS Journal Path (`save_memory_journal`)

The CQRS path enqueues writes to a separate journal database for async materialization:

```
save_memory_journal()
    → validate parameters
    → enqueue to journal.db (lock-free INSERT with WAL)
    → return note_id immediately
    → background worker materializes:
        → acquire flock
        → begin saga
        → apply same 3-step saga as direct path
        → commit saga
        → release flock
```

**Durability guarantee:** The journal entry is durable on disk before returning.
The materialization is eventual but crash-consistent — if the worker crashes,
the entry remains in the journal and is retried on next drain.

## Saga Transaction (3-Step Write)

The saga executes three steps sequentially, each committed to SQLite
independently:

1. `do_upsert_db` — INSERT/UPDATE the `memories` row + FTS5 + chunks + embeddings + KG + facts + backlinks
2. `do_write_vec_key` — INSERT the vec_key row
3. `do_write_file` — atomic write of the `.md` file

### On Failure

If step N raises, steps 1..N-1 are rolled back **in reverse order** via
`saga.undo_upsert`. The rollback includes dependent-row cleanup:

- `kg_facts` rows linked to this memory are deleted
- Orphan `kg_edges` are removed (ON DELETE CASCADE)
- Backlinks are reverted

Rollback raises `SagaError` (a `RuntimeError` subclass). There is **no fallback**
that silently commits partial data. Every saga failure propagates to the caller.

### Undo Guarantees

Even if the undo itself fails (e.g. disk full during rollback), the error is
logged and the next undo is still attempted. The principle is:
**losing data is acceptable, crashing the tool call is not.**

### Caller Responsibilities

| Caller | On SagaError |
|--------|-------------|
| **Agent (direct MCP tool call)** | Error is surfaced as a tool response; agent may retry |
| **Auto-save hook** | Caught by `auto_save._upsert_memory`, logged to `memory_audit_log`, returns `{"saved": False, "error": "..."}` — no agent crash |
| **CQRS journal** | Entry remains in journal; retried on next worker drain |
| **CLI / tests** | Bare `RuntimeError` — wrap in try/except if non-fatal |

## Cross-Process Locking

- `save_memory` acquires an `fcntl.flock` on `memory/.write.lock` before
  opening the SQLite connection
- The saga path skips re-acquiring the lock if `lock_already_held=True`
- Lock order is always: **file lock first, then connection**
- The lock auto-releases when the holding process dies (OS-managed)
- CQRS journal uses lock-free INSERT (WAL mode handles concurrency)

## Concurrency Model

| Scenario | Behavior |
|----------|----------|
| **Single agent, direct path** | Flock serializes writes; no contention |
| **Multiple agents, direct path** | Flock serializes; one writer at a time |
| **Multiple agents, CQRS path** | Lock-free journal INSERT; worker materializes sequentially |
| **Mixed direct + CQRS** | Direct path acquires flock; CQRS worker waits for flock |

## What Is NOT Guaranteed

- **Post-save hooks** (backfill_global, index rewrites, skill extraction) run
  *after* the saga commits and are not covered by rollback. If a post-save hook
  fails, the memory is saved but the hook work is lost (retry on next save).
- **Background tasks** (entity extraction, contradiction detection) are
  enqueued after the saga and may be delayed or lost if the worker pool is
  exhausted.
- **CRDT sync** is eventual. A server crash between a CRDT merge and its
  propagation to the `.md` file creates silent drift.
- **CQRS materialization** is eventual. A crash between journal enqueue and
  materialization leaves the entry in the journal for retry. The note_id is
  returned before materialization completes.

## Best Practices

1. **Treat the returned `note_id` as the durability signal** — if you get a
   string starting with `"Error ["`, the save failed.
2. **Never bypass `save_memory`** — direct DB writes, file writes, or vec_key
   inserts skip the saga and cannot be rolled back.
3. **`defer_expensive=True` (default)** returns in <200ms by deferring
   embedding, KG extraction, and contradiction checks. Expensive work runs
   asynchronously via cron. The durability of the core save is unaffected.
4. **Use CQRS for high-throughput** — `save_memory_journal` enables lock-free
   multi-agent writes. The tradeoff is eventual materialization.
5. **Test the failure path** — mock `_index_facts` to raise mid-save and
   verify no partial rows remain in `memories`, `kg_facts`, or `backlinks`.
