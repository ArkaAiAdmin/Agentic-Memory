# CQRS Write Journal Runbook

## Overview

When `MEMORY_WRITE_JOURNAL_ENABLED=true` (or `write_journal = true` in
`memory.toml`), all writes go through a lock-free `INSERT` into
`journal.db` instead of direct `save_memory`. A background reconciler
thread (`journal-reconciler`) drains the journal and materializes entries
into `memory.db` via the standard saga (DB upsert + vec key + .md file).

## Architecture

```mermaid
graph LR
    A[Agent process - save_memory _journal] -->|INSERT| B[journal.db]
    B --> C[Background thread - reconciliation loop - 100ms poll]
    C --> D[dequeue -> materialize -> saga]
```

- **Multi-agent safe**: any number of agent processes can `INSERT` into
  `journal.db` concurrently (no `flock`, no contention).
- **Single writer**: the reconciler thread is the *only* writer to
  `memory.db`, so no cross-process `flock` on the main DB.
- **Crash recovery**: `reset_stuck_processing` runs at daemon start
  to unstick entries that were `processing` when the previous daemon
  crashed.

## Health Check

```bash
# Via MCP
memory_health_check(conn="default")

# Returns a "journal" section:
{
  "journal": {
    "enabled": true,
    "path": "/path/to/journal.db",
    "pending": 0,
    "processing": 0,
    "failed": 0,
    "reconciler_alive": true
  }
}
```

### Interpreting

| Metric | Normal | Alert |
|---|---|---|
| `pending` | 0–few | Growing unbounded → reconciler may be stuck |
| `processing` | 0 | >0 for >60s → daemon crashed mid-batch |
| `failed` | 0 | >0 → an entry permanently failed materialization |
| `reconciler_alive` | true | false → reconciler thread died unexpectedly |

## Dead-Letter Recovery

When an entry fails permanently (content-hash mismatch, validation
failure, max retries exhausted), it's moved from `write_journal` to
`journal_failed` with the error message and original payload.

### List dead letters

```sql
SELECT id, original_id, note_id, error, retry_count, created_at
FROM journal_failed;
```

### Replay a dead letter (manual)

```python
from infra.write_journal import get_entry_by_note_id
from save.pipeline import save_memory

entry = get_entry_by_note_id(Path("journal.db"), "lessons/my-note")
# Fix the issue, then re-save:
save_memory(content=entry["content"], category=entry["category"], ...)
```

## Reconciliation Loop Stuck

1. **Check liveness**: `status["journal"]["reconciler_alive"]` in health
   check. If false, restart the MCP server — the daemon thread dies with
   the process.

2. **Stuck processing entries**: entries with `status='processing'` for
   >60s suggest the daemon crashed mid-batch. On restart,
   `reset_stuck_processing()` unmarks them back to `pending`.

   Manual: `venv/bin/python -c "from infra.write_journal import reset_stuck_processing; reset_stuck_processing(Path('memory/journal.db'))"`

3. **Pending entries not draining**: check the reconciler log (stderr).
   A permanent error on one entry blocks the batch. Dead-letter it:
   ```python
   from infra.write_journal import mark_dead_letter
   mark_dead_letter(Path("memory/journal.db"), entry_id=42, error="manual dead-letter")
   ```

## Read-Your-Writes

When the journal is enabled, `memory_search` automatically supplements
results with pending entries from the write journal
(`_supplement_with_pending`). This ensures the agent sees its own recent
writes before the daemon materializes them.

If you see duplicate results (once from supplement, once after
materialization), the supplement entry has `"_pending": true`. The
duplicate disappears after the next poll cycle (≤100ms).

## New Tool: memory_read_your_writes

The `memory_search` tool already does read-your-writes by supplementing
pending journal entries. No separate tool needed.

## Related

- `save/pipeline.py` — `materialize_journal_entry`, `save_memory_auto`
- `background/background_worker.py` — `_reconciliation_loop`, `_start_reconciler`
- `infra/write_journal.py` — `enqueue_write`, `mark_dead_letter`, `reset_stuck_processing`
- `mcp_maintenance.py` — `memory_health_check` (journal section)
- `docs/architecture.md` — system architecture
