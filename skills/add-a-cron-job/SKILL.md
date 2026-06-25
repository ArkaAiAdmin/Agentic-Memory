---
name: add-a-cron-job
description: Maintainer procedure for adding a new background cron job to the agentic-memory system. Use when you need a recurring task (consolidation, dedup, integrity check, etc.) that runs on a schedule. Don't use for one-shot CLI tools (just write a script and document it in `docs/how-to/`).
---

# Add a Cron Job

How to add a new background job to the agentic-memory system. There are 23 cron scripts today (`cron/cron_*.py`); this is how to add a 24th. All cron scripts live in the `cron/` subdirectory (moved from the repo root on 2026-06-22).

## The 60-second version

1. Write `cron/cron_your_op.py` in the `cron/` subdirectory. Keep it small (50-200 lines).
2. Use `sys.executable` for the python interpreter, **not** hardcoded paths (M8 fix).
3. Set the feature flag env var it needs at the top (`os.environ.setdefault(...)`).
4. Add the crontab line to `cron/install_crontab.sh` (NOT `docs/how-to/cron-setup.md` — that file is for users, not maintainers).
5. Add a CI drift check (if you want it covered by the wirings-check script).
6. Update the crontab: `bash cron/install_crontab.sh`.
7. Test it manually: `venv/bin/python cron/cron_your_op.py`.

Total: ~30 minutes.

## Step 1: write the script

```python
#!/usr/bin/env python3
"""Cron wrapper: your-op — what this does in one line.

Run hourly via crontab:
    0 * * * * /path/to/agentic-memory/venv/bin/python /path/to/agentic-memory/cron_your_op.py
"""
import os
import sys
import json
import sqlite3
from pathlib import Path

# Step 2: feature flag env (if applicable)
os.environ.setdefault("MEMORY_YOUR_FEATURE", "1")

# Step 3: standard preamble
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Step 4: import what you need
from memory_common import connection_pool, get_memory_paths
from your_module import your_op

# Step 5: do the work
def main():
    cwd, local_mem, global_mem = get_memory_paths()
    db_path = global_mem / "memory.db"
    if not db_path.exists():
        print(f"No memory.db at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = connection_pool.get(str(db_path), timeout=30.0)
    try:
        result = your_op(conn)
        print(json.dumps(result, indent=2))
    finally:
        # Return to pool, do NOT close
        from memory_common import safe_close_db
        safe_close_db(conn)

if __name__ == "__main__":
    main()
```

## Conventions (must follow)

1. **Use `sys.executable` (or `MEMORY_PYTHON` env var override), never hardcode venv paths.** This is the M8 fix. The hardcoded venv path breaks for any other user.
   ```python
   # WRONG (pre-M8):
   PYTHON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python")
   # RIGHT (post-M8):
   PYTHON = os.environ.get("MEMORY_PYTHON") or sys.executable or "python3"
   ```

2. **Set feature flag env at the top of the script** if your op depends on one. The cron wrapper is the place to ensure the flag is on, so users who schedule the cron don't have to remember.
   ```python
   os.environ.setdefault("MEMORY_KNOWLEDGE_GRAPH", "1")
   os.environ.setdefault("MEMORY_SELF_DIRECTED", "1")
   ```

3. **Return connections to the pool, don't close them.** Use `safe_close_db(conn)` which handles both.
   ```python
   from memory_common import safe_close_db
   try:
       result = your_op(conn)
   finally:
       safe_close_db(conn)  # returns to pool
   ```

4. **Print a one-line summary to stdout, the rest to stderr or a log file.** Stdout is for cron monitoring; details go to `memory/<your_op>.log`.

5. **Lock-file awareness.** Long-running operations should use `memory/.rebuild.lock` (or a similar lock) to prevent concurrent runs. See `cron_compact.py` and `rebuild_index.py` for the pattern.

6. **Verify db_path.exists() before doing anything.** This is the lesson from the 2026-06-15 `backfill_all` bug — a missing path caused a `ProgrammingError` instead of a clean error.

## Step 2: add a crontab line to `cron/install_crontab.sh`

Open `cron/install_crontab.sh` and add a row inside `build_block()`
(marked by `# BEGIN agentic-memory managed block` /
`# END agentic-memory managed block`). The block is delimited by
markers so re-running the installer is idempotent — it replaces
the agentic-memory block without touching unrelated user crontab
entries. Match the format of the existing entries (UTC time,
`$VENV_PY $ROOT/cron/cron_your_op.py`, log to
`$LOG_DIR/your_op.log`).

For user-facing documentation (not the crontab), add a row to
`docs/how-to/cron-setup.md` too. That file is for users, not
maintainers; the install line is the source of truth.

```markdown
### N. Your Op (Hourly)

What this does in one sentence.

```bash
0 * * * * /path/to/agentic-memory/venv/bin/python /path/to/agentic-memory/cron_your_op.py
```
```

## Step 3: add a test (if the op is non-trivial)

For most cron jobs, the underlying op already has tests in `eval/`. The cron wrapper is just a CLI driver. Skip if:
- The cron is a thin wrapper around a tested function.
- The cron is a "best effort" background job (a test failure in CI doesn't matter).

Add a test if:
- The cron has any logic (rotation, batching, decision-making).
- The cron's correctness affects data integrity.

Test pattern (use a `tempfile.mkdtemp()` DB):

```python
# eval/test_cron_your_op.py
import subprocess, tempfile, os
import pytest

class TestCronYourOp:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # set up minimal DB

    def test_runs_cleanly(self):
        env = os.environ.copy()
        env["MEMORY_DB_PATH"] = f"{self.tmpdir}/test.db"
        result = subprocess.run(
            ["venv/bin/python", "cron_your_op.py"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert result.returncode == 0
        assert "expected output" in result.stdout
```

## Step 4: update CI drift check (optional but recommended)

The script `~/.opencode/scripts/cron_wirings_check.py` verifies that cron scripts reference real modules. If you add a new cron, add its expected references to that script's allowlist (or ensure it's caught by the generic check).

Run it:

```bash
venv/bin/python ~/.opencode/scripts/cron_wirings_check.py
```

## Step 5: update memory_workflow.md

In the "File Locations" table, add a row for your new cron:

```markdown
| `cron_your_op.py` | What it does in one sentence |
```

## Step 6: test it manually

```bash
# As a sanity check
venv/bin/python cron_your_op.py

# Check the output log
tail memory/your_op.log
```

If the cron modifies state, verify the state with the appropriate MCP tool:

```python
# For a memory-tier migration, for example:
from memory_maintenance import tier_stats
print(tier_stats())
```

## Common pitfalls

- **Don't hardcode `sys.executable` to an absolute path.** Use the env-var pattern.
- **Don't bypass feature flag env vars.** If your op needs `MEMORY_KNOWLEDGE_GRAPH=1`, set it at the top of the script.
- **Don't run two crons that conflict on the same lock file.** `memory/.rebuild.lock` is shared; only one rebuild-flavored cron at a time.
- **Don't write huge output to stdout.** Crontab captures stdout; if it's 10 MB, it eats disk. Use a log file.

## Existing crons to model on

| Cron | What it does | When to run | Look at for |
|---|---|---|---|
| `background_worker.py` | Process background task queue + vec drift auto-repair | Every 5 min | Basic structure, sys.executable pattern |
| `cron/cron_auto_summarize.py` | Auto-summarize long notes | Monday 7am | TF-IDF compression |
| `cron/cron_auto_share.py` | Auto-publish opt-in memories to shared pool | (manual) | `memory_auto_share` opt-in pattern |
| `cron/cron_backup.py` | Daily SQLite backup | Daily 2am | Backup pattern, rotation, busy_timeout |
| `cron/cron_compact.py` | Full maintenance cycle | Monthly 1st 2am | Lock file, multiple steps, integrity_check at end |
| `cron/cron_concept_drift.py` | Embedding centroid drift detection (populates `drift_alarms`) | Sunday 6am | Drift alarm pattern, v15 |
| `cron/cron_consolidate.py` | Dedup + contradiction | Sunday 4am | SHA-256 + n-gram Jaccard pattern |
| `cron/cron_crdt_sync.py` | Multi-agent CRDT sync | (varies) | Sync server interaction |
| `cron/cron_cross_session_learn.py` | Cross-session learning | Monday 4:15am | Pattern extraction |
| `cron/cron_detect_vec_drift.py` | Detect vec_keys ↔ memories drift | Daily 4:30am | Vector index health |
| `cron/cron_embedding_recompute.py` | Re-embed after model revision change | (manual) | Idempotent recompute pattern |
| `cron/cron_heartbeat.py` | Tier reassignment | Daily 3am | Self-healing pattern, archive |
| `cron/cron_integrity_check.py` | DB health check | Sunday 1am | Read-only safety pattern |
| `cron/cron_kg_backfill.py` | Knowledge graph backfill | Sunday 3:30am | KG schema, dedup |
| `cron/cron_kg_backfill_monitor.py` | KG backfill progress monitor | Daily 4am | Progress tracking |
| `cron/cron_pinned_decay.py` | Unpin stale pinned memories | Sunday 5am | Audit, psi formula |
| `cron/cron_purge_expired.py` | Hard-delete tombstones older than 30 days | Monthly 6am | Date math, safety net |
| `cron/cron_quality_filter.py` | Quality gate stats | Monday 6am | Validation metrics |
| `cron/cron_rebuild_fts.py` | Lightweight FTS5 B-tree rebuild | Daily 2:33am | `INSERT INTO fts VALUES('rebuild')` |
| `cron/cron_retention_stats.py` | Adaptive retention + neural forget curve | Monday 8am | Half-life computation, cache invalidation |
| `cron/cron_rewrite_links.py` | Fix broken wiki links | Sunday 4:30am | Backlink consistency |
| `cron/cron_skill_extraction.py` | Refresh skill extraction cache | Monday 3:45am | Skill metadata |
| `cron/cron_sync.py` | Multi-agent sync orchestration | (manual) | Alternative sync entry point |
| `cron/cron_tier_migration.py` | On-demand tier migration | (manual) | `memory_run_tier_migration` pattern |

## Reference

- All 23 crons: `cron/cron_*.py` in the `cron/` subdirectory
- Crontab installer: `cron/install_crontab.sh` (idempotent block installer)
- Cron setup how-to: `docs/how-to/cron-setup.md`
- Drift check: `~/.opencode/scripts/cron_wirings_check.py`
- Lock file pattern: `rebuild_index.py:98`
- M8 / M9 fixes: `CONTRIBUTING.md` (top section)

— last reviewed 2026-06-22
