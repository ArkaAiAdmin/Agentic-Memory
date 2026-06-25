# Plan: Close Remaining Audit Gaps (2026-06-22 follow-up)

## Background

The 2026-06-22 technical review fixed 12 issues across P0 (data loss),
P1 (correctness), SEC (security), and Scenario/Remediation buckets. Four
items were flagged as **audit gaps** — not directly addressed but mitigated
or partially mitigated. This plan closes them in priority order.

| # | Gap | Severity | Current state |
|---|---|---|---|
| 3 | Orphaned KG/backlinks on saga rollback | **High** | `memory_delete.py:403,435,453` does manual cascade; saga rollback does not. Real data-integrity risk. |
| 4 | Circuit-breaker telemetry | Medium | In-memory only (`auto_save.py:901-905`). No persistent record, no admin tool. |
| 2 | Rebuild subprocess graceful skip | Low | Cross-process `flock` exists in `rebuild_vec_index.py:154`; worker treats contention as `RuntimeError` instead of graceful skip. |
| 1 | Connection pool cross-process race | Low (likely N/A) | Per-thread keys mitigate intra-process. Multi-process scenarios may need an inter-process lock — needs deploy-mode audit first. |

## Scope decisions

- **Gap #1** is *deferred* to a separate task pending a deploy-mode audit.
  In practice, the live install has only one process touching the DB at
  a time (the opencode hook + a single sync server). The plan includes
  a "Decision Point" task to confirm before proceeding.
- **Gap #2, #3, #4** are scoped for this plan.

## Phase 1 — Gap #3: Orphaned KG/backlinks on saga rollback (HIGH priority)

### Why this is the highest priority
A failed `save_memory` saga (e.g., disk full, lock contention + fallback
fails, partial FTS5 write) can leave the `memories` row in a clean state
(saga rolled back) but `kg_entities`, `kg_edges`, and `backlinks` rows
referencing the rolled-back `note_id` remain. The next time a search
hits that orphan, it returns a backlink or fact pointing to a non-existent
note. Over time these accumulate and inflate search result noise.

### Approach: belt-and-suspenders

We use **two layers** because the underlying data state is recoverable
but prevention is cheaper than repair:

1. **Cascade FKs** (preferred) — add `ON DELETE CASCADE` to `kg_edges` and
   `backlinks` so that any future `DELETE FROM memories WHERE id = ?` (manual
   or rollback) automatically cleans up the dependent tables. `kg_entities`
   has no FK to `memories` (it can be shared across notes), so the cleanup
   there needs a "delete unused entities" step.

2. **Saga rollback hook** — extend `_rollback` in `save/saga.py` to call
   the same cleanup that `memory_delete.py` already does. This handles the
   case where the `memories` row was INSERTed (not DELETEd) and rolled back;
   cascade FKs only fire on `DELETE`.

3. **Repair tool** — `memory_integrity.find_kg_orphans` / `repair_kg_orphans`
   for historical orphan cleanup, exposed as `--repair-kg-orphans` CLI flag
   in the same vein as `--recover-orphan-files` (Scenario 7 fix).

### Tasks

**T1.1 — Add SQL migration 017 with cascade FKs**

New files:
- `migrations/017_kg_cascade.sql`
- `migrations/017_kg_cascade.down.sql`

Contents (sketch — review against `migrations/009_kg_facts_entity_fks.sql`
for style):
```sql
-- Migration 017: cascade kg_edges + backlinks on memory delete
--
-- kg_edges and backlinks reference memories indirectly (via note_id TEXT
-- or via kg_entities which can be shared). When a memory is deleted, the
-- dependent rows become orphans that bloat search results.
--
-- B-3 fix (2026-06-22 follow-up): add ON DELETE CASCADE to the
-- note_id-bound columns. kg_entities is shared so we cannot cascade
-- to it; instead, the saga rollback path and --repair-kg-orphans
-- CLI handle entity cleanup.
--
-- Note: SQLite requires recreating the table to add a CASCADE FK
-- (ALTER TABLE ... REFERENCES ... ON DELETE CASCADE is silently
-- ignored). We do the standard 12-step recreation per
-- https://www.sqlite.org/lang_altertable.html.

PRAGMA foreign_keys = OFF;
BEGIN;

CREATE TABLE kg_edges_new (
    ... -- same as kg_edges but with FOREIGN KEY (source_id) ... ON DELETE SET NULL
);
INSERT INTO kg_edges_new SELECT * FROM kg_edges WHERE source_id IN (SELECT id FROM kg_entities);
DROP TABLE kg_edges;
ALTER TABLE kg_edges_new RENAME TO kg_edges;
... -- (similar for backlinks)

COMMIT;
PRAGMA foreign_keys = ON;
```

Update:
- `migration_runner.py` — bump `SCHEMA_VERSION` from 16 to 17

**T1.2 — Extract `_cleanup_memory_relations` helper from `memory_delete.py`**

Current `memory_delete.py:403,435,453,711,782,813` has duplicate cleanup
logic. Extract to a single function and call it from both `memory_delete`
and the new saga rollback path.

New file: `save/cleanup.py` (or extend `save/saga.py`)

```python
def cleanup_memory_relations(conn, note_id: str) -> dict[str, int]:
    """Remove kg_edges, kg_entities (if unreferenced), and backlinks
    rows tied to *note_id*. Returns counts of removed rows per table."""
    ...
```

`memory_delete.py:403-460` refactored to call this helper.

**T1.3 — Wire into `save/saga.py` rollback**

When a saga step fails after the `memories` row was INSERTed/UPDATEd,
the rollback path needs to:
1. Reverse the `memories` row (existing behavior)
2. **NEW:** call `cleanup_memory_relations(conn, note_id)` to wipe
   the dependent rows that may have been written by the post-save
   hooks before the failure

The order matters: clean up dependent rows first, then the `memories`
row. If cascade FKs are in place, the `memories` DELETE will auto-clean,
but explicit cleanup is safer (works on installs that haven't run
migration 017 yet).

**T1.4 — Add `find_kg_orphans` / `repair_kg_orphans` to `memory_integrity.py`**

Mirrors the existing `find_orphan_files` / `recover_orphan_files` pattern
(Scenario 7 fix from 2026-06-22).

```python
def find_kg_orphans(db_path: str) -> dict[str, list]:
    """Return orphan rows:
      - kg_edges pointing to kg_entities with no surviving note_ids
      - kg_entities that are referenced by zero surviving notes
      - backlinks rows where source_id or target_id is not in memories
    """
    ...

def repair_kg_orphans(db_path: str, dry_run: bool = True) -> dict[str, int]:
    """Delete orphan rows. Returns counts per table."""
    ...
```

CLI wiring (mirror `--recover-orphan-files`):
```python
# memory_integrity.py main()
parser.add_argument(
    "--repair-kg-orphans",
    action="store_true",
    help="Delete orphan kg_edges/kg_entities/backlinks rows.",
)
```

**T1.5 — Tests** (`eval/test_kg_orphan_recovery.py`)

Cover:
1. Saga failure after `kg_edges` insert → rollback cleans up
2. Saga failure after `backlinks` insert → rollback cleans up
3. `memory_delete` still works (regression for the refactor)
4. `find_kg_orphans` correctly identifies orphans in a manually-broken DB
5. `repair_kg_orphans --dry-run` does not modify the DB
6. `repair_kg_orphans` (without --dry-run) removes the orphans
7. After migration 017, deleting a memory cascades to `kg_edges` and
   `backlinks` automatically (no explicit cleanup needed)
8. After migration 017, `kg_entities` is NOT auto-deleted (since they
   are shared) — `repair_kg_orphans` is the path to clean up unreferenced
   entities

## Phase 2 — Gap #4: Circuit-breaker telemetry (MEDIUM priority)

### Why this matters
Operators currently have **no visibility** into the auto-save circuit
breaker state across process restarts. If a daemon crashes while the
breaker is open, the new daemon starts "fresh" with a closed breaker
and may hammer a still-broken dependency. Conversely, a healthy daemon
that never opens the breaker leaves no record of its existence.

### Approach
Persist breaker state to `memory_audit_log` (table already exists per
`migration_runner.py`). Log on:
- **Open**: breaker transitions to open (record: open_time, failure_count, reason)
- **Close**: breaker recovers and transitions to closed (record: open_duration_s)
- **Half-open probe**: every retry after a closed state (record: probe_time, success)

This gives a queryable history via `memory_audit_query` without a new table.

### Tasks

**T2.1 — Add `_persist_circuit_state` to `auto_save.py`**

```python
def _persist_circuit_state(conn, event: str, **fields) -> None:
    """Insert a row into memory_audit_log for a breaker event.
    
    event ∈ {"open", "close", "half_open"}
    fields:
      - failure_count: int
      - window_s: float
      - backoff_s: float
      - duration_s: float (for close events)
    """
    conn.execute(
        "INSERT INTO memory_audit_log (event_type, ts, details_json) "
        "VALUES (?, ?, ?)",
        (
            f"auto_save_circuit_{event}",
            _t.time(),
            json.dumps(fields),
        ),
    )
    conn.commit()
```

Call sites:
- `_AUTO_SAVE_STATE["circuit_open_until"] = now + cb_seconds` (line 966) → log "open"
- After successful save following a recovery → log "close" (compute duration_s)
- Every retry after a window of failures → log "half_open" (best-effort, may be omitted if it complicates the hot path)

**T2.2 — Add `circuit_breaker_status()` admin function**

Under `mcp_maintenance_ops.py`:
```python
def circuit_breaker_status(
    limit: int = 20,
    since_ts: float | None = None,
) -> list[dict]:
    """Return the last N circuit-breaker events from memory_audit_log."""
    ...
```

Register in the dispatch table. This becomes a new ADMIN tool
(`memory_maintenance(operation="circuit_breaker_status")`).

**T2.3 — Add `circuit_breaker_ttl_days` config**

In `config.py`:
```python
auto_save_circuit_breaker_ttl_days: int = 7  # audit log retention
```

This is informational; actual cleanup is the daily-digest cron's job.

**T2.4 — Tests** (`eval/test_circuit_breaker_telemetry.py`)

Cover:
1. Breaker open event writes a row to `memory_audit_log`
2. Breaker close event writes a row with `duration_s` field
3. `circuit_breaker_status()` returns events in reverse-chronological order
4. Limit param respected
5. `since_ts` filter works
6. Process restart loses in-memory state but keeps audit history

## Phase 3 — Gap #2: Rebuild subprocess graceful skip (LOW priority)

### Tasks

**T3.1 — Update `background_worker.py:222-225`**

The current code raises `RuntimeError` for any non-zero return code.
`rebuild_vec_index.py` exits non-zero when its `flock` is contended
(returns 0 with a "skipped" log message, OR raises BlockingIOError and
exits non-zero). Either way, the worker should distinguish "skipped"
from "failed".

```python
result = subprocess.run(...)
if result.returncode != 0:
    # Check if the script reported it was skipped (another rebuild in progress)
    if "Another vec_index rebuild" in (result.stdout or "") or \
       "Another vec_index rebuild" in (result.stderr or ""):
        return f"vec_idx rebuild skipped: {reason}; another rebuild in progress"
    raise RuntimeError(...)
```

**T3.2 — Tests** (`eval/test_rebuild_concurrency.py`)

Cover:
1. Two concurrent rebuild subprocesses → second one returns "skipped"
   instead of "RuntimeError"
2. The skip outcome is logged but does NOT mark the task as failed
3. When the first rebuild completes, a subsequent call succeeds

## Phase 4 — Gap #1: Cross-process pool lock (DEFERRED)

### Decision Point

Audit the live install for multi-process DB access patterns:
- `opencode` session process: 1 process per session
- `sync_server.py`: 1 long-lived process
- `cron/*` scripts: 1 short-lived process per cron tick
- `background_worker`: 1 long-lived process (or flock-protected so only 1)
- `auto_save.py daemon`: 1 long-lived process per memory dir (flock-protected)

In the current install, the only multi-process scenario is:
- long-lived `auto_save.py daemon` + short-lived cron script (e.g.,
  `cron_daily_digest.py`)

For a `SELECT`-only cron, no lock is needed (SQLite WAL allows concurrent
reads). For a write cron (e.g., `tier_migration`), the cron already has
its own flock via `cron/install_crontab.sh:lock` (Scenario 10 fix). The
remaining gap is **two write crons running at the exact same moment**,
which is prevented by the per-cron flock and the `*/15` cadence reduction.

**Decision:** If the audit confirms that no two long-lived processes can
hold a write transaction on the same DB simultaneously, **no fix is
needed** — we just document the limitation.

If the audit reveals a real multi-process write scenario, the fix is:
- Add an inter-process file lock (`.memory_write.lock`) acquired
  before any `connection_pool.get()` that may lead to a write
- Release after `safe_close_db(should_commit=True)`
- Per-thread `threading.local()` cache to avoid re-acquiring within
  the same process

**T4.0 — Deploy-mode audit (decision task)**

Before implementing, run:
```bash
grep -rn "connection_pool.get" . --include="*.py" | grep -v eval
```
and confirm all callers are either in a single-process context or
already have a flock.

## Documentation updates (all phases)

After all code lands:
- `CHANGELOG.md` — entries for all phases
- `AGENTS.md` — new hard rule for kg cascade behavior; new env var
  `auto_save_circuit_breaker_ttl_days`; new CLI flag `--repair-kg-orphans`;
  new admin op `circuit_breaker_status`
- `memory_workflow.md` — new troubleshooting section entries
- `docs/architecture.md` — schema v17 entry; new crash-safety table rows
  for `cleanup_memory_relations` and circuit-breaker persistence

## Verification

Before declaring done:
- `python -m mypy .` — 0 errors (per AGENTS.md)
- `python -m pytest eval/test_kg_orphan_recovery.py eval/test_circuit_breaker_telemetry.py eval/test_rebuild_concurrency.py` — all pass
- Full test suite: `python -m pytest eval/ --timeout=300` — no regressions
- Manual test on live install: `python memory_integrity.py ~/.config/agentic-memory/memory/memory.db --recover-orphan-files --repair-kg-orphans --dry-run`

## Risk assessment

| Task | Risk | Mitigation |
|---|---|---|
| T1.1 (migration 017) | Medium — SQLite requires table recreation for CASCADE FK | Test on a backup copy first; ensure the down migration is correct |
| T1.2 (refactor memory_delete) | Low — pure code movement, no behavior change | Full regression on `eval/test_memory_delete*` |
| T1.3 (saga rollback) | Medium — rollback path is fragile (per AGENTS.md hard rule 6) | Careful ordering; new test cases; do not change existing rollback semantics |
| T1.4 (integrity CLI) | Low | Mirror existing `--recover-orphan-files` pattern |
| T2.1 (audit log writes) | Low — adds rows to existing table | Use a distinct `event_type` prefix for filterability |
| T2.2 (admin function) | Low | Follows existing admin op pattern |
| T3.1 (worker error parsing) | Low | String-match is brittle but matches existing log format |

## Out of scope (deferred)

- **Saga rollback metrics** — track rollback count, error types, etc.
  Useful for observability but not data integrity.
- **Multi-process connection pool coordination** — only relevant if
  Gap #1 audit shows a real need.
- **Cross-host DB replication** — explicitly out of scope per the
  local-first design principle.

## Estimated effort

| Phase | LoC change | Test LoC | Docs LoC | Total |
|---|---|---|---|---|
| Phase 1 (kg orphans) | ~250 | ~300 | ~50 | ~600 |
| Phase 2 (circuit telemetry) | ~80 | ~150 | ~30 | ~260 |
| Phase 3 (rebuild skip) | ~20 | ~80 | ~10 | ~110 |
| Phase 4 (pool lock) | ~50 | ~100 | ~20 | ~170 |
| **Total** | **~400** | **~630** | **~110** | **~1140** |

Roughly **2-3 hours** of focused work, **half of which is testing**.
