# How to Add a Cron Job

Add a new background job to the agentic-memory system. There are 26 cron scripts today (`cron/cron_*.py`); this is how to add a 27th. All cron scripts live in the `cron/` subdirectory (moved from the repo root on 2026-06-22).

This is the **maintainer** version. For the high-level skill, see `skills/add-a-cron-job/SKILL.md`.

## When to use this

- You need a recurring task (every N minutes / hourly / daily / weekly).
- The task is "background maintenance" — not on the critical path of any user query.

## When NOT to use this

- You need a one-shot tool (just write a script and document it in `docs/how-to/`).
- You need an event-driven action (use a hook instead — `add-a-claude-code-hook`).

## Steps

1. **Create the script.** Use `cron_your_op.py` as the name pattern. Place it in `cron/` (not the repo root).

2. **Use the standard preamble:**

   ```python
   #!/usr/bin/env python3
   """Cron wrapper: your-op — what this does in one line.

   Run hourly via crontab:
       0 * * * * /path/to/agentic-memory/venv/bin/python /path/to/agentic-memory/cron/cron_your_op.py
   """
   import os
   import sys
   import json
   import sqlite3
   from pathlib import Path

   # Feature flag env (if applicable)
   os.environ.setdefault("MEMORY_YOUR_FEATURE", "1")

   # Standard preamble
   os.chdir(os.path.dirname(os.path.abspath(__file__)))
   sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

   from memory_common import connection_pool, get_memory_paths, safe_close_db

   def main():
       cwd, local_mem, global_mem = get_memory_paths()
       db_path = global_mem / "memory.db"
       if not db_path.exists():
           print(f"No memory.db at {db_path}", file=sys.stderr)
           sys.exit(1)

       conn = connection_pool.get(str(db_path), timeout=30.0)
       try:
           # your logic here
           result = your_op(conn)
           print(json.dumps(result, indent=2))
       finally:
           safe_close_db(conn)

   if __name__ == "__main__":
       main()
   ```

3. **Use `sys.executable`, not hardcoded paths.** This is the M8 fix. Don't write `venv/bin/python` literally.

4. **Add the crontab line** to `cron/install_crontab.sh` (NOT `docs/how-to/cron-setup.md` — that's a user-facing reference, not the source of truth). The block is delimited by `# BEGIN agentic-memory managed block` / `# END agentic-memory managed block` markers so re-running the installer is idempotent.

5. **Update `memory_workflow.md`** (Automated Maintenance table + File Locations) with a row for your cron.

6. **Add to CI drift check** (optional): `/Users/arka/.opencode/scripts/cron_wirings_check.py` verifies that cron scripts reference real modules. Run it to confirm.

7. **Install + test manually:**
   ```bash
   bash cron/install_crontab.sh
   venv/bin/python cron/cron_your_op.py
   tail memory/your_op.log  # if it logs
   ```

## Existing crons (model after these)

All scripts live under `cron/`.

| Cron | Frequency | Purpose |
|---|---|---|
| `background_worker.py` | 5 min | Process background task queue + vec drift auto-repair |
| `cron/cron_backup.py` | Daily 02:00 | SQLite backup, 7-day rotation |
| `cron/cron_compact.py` | Monthly 1st | Full maintenance cycle |
| `cron/cron_consolidate.py` | Sunday 04:00 | Dedup + contradiction |
| `cron/cron_concept_drift.py` | Sunday 06:00 | Embedding centroid drift detection (populates `drift_alarms`) |
| `cron/cron_heartbeat.py` | Daily 03:00 | Tier reassignment |
| `cron/cron_integrity_check.py` | Sunday 01:00 | DB health |
| `cron/cron_pinned_decay.py` | Sunday 05:00 | Unpin stale pinned |
| `cron/cron_purge_expired.py` | Monthly 06:00 | Hard-delete tombstones |
| `cron/cron_quality_filter.py` | Monday 06:00 | Quality gate stats |
| `cron/cron_retention_stats.py` | Monday 08:00 | Adaptive retention + neural forget curve |
| `cron/cron_rewrite_links.py` | Sunday 04:30 | Fix broken wiki links |
| `cron/cron_auto_summarize.py` | Monday 07:00 | Auto-summarize long notes |
| `cron/cron_rebuild_fts.py` | Daily 02:33 | Lightweight FTS5 B-tree rebuild |
| `cron/cron_detect_vec_drift.py` | Daily 04:30 | Detect vec_keys ↔ memories drift |
| `cron/cron_kg_backfill.py` | Sunday 03:30 | Knowledge graph backfill (incremental) |
| `cron/cron_kg_backfill_monitor.py` | Daily 04:00 | KG backfill progress monitor |
| `cron/cron_skill_extraction.py` | Monday 03:45 | Refresh skill extraction cache |
| `cron/cron_cross_session_learn.py` | Monday 04:15 | Cross-session learning |
| `cron/cron_embedding_recompute.py` | (manual) | Re-embed after model revision change (2026-06-22) |
| `cron/cron_tier_migration.py` | (manual) | On-demand tier migration (2026-06-22) |
| `cron/cron_auto_share.py` | (manual) | Auto-publish opt-in to shared pool (2026-06-22) |
| `cron/cron_sync.py` | (manual) | Multi-agent sync orchestration (2026-06-22) |

## Common pitfalls

- **Don't hardcode venv paths.** Use `sys.executable` or `MEMORY_PYTHON` env.
- **Don't bypass feature flags.** Set the env var at the top of the script.
- **Don't print huge output to stdout.** Use a log file.
- **Don't race on `memory/.rebuild.lock`.** Only one rebuild-flavored cron at a time.
- **Don't place new cron scripts at the repo root.** They live in `cron/` since 2026-06-22.

## Reference

- All 23 existing crons: `cron/cron_*.py` in the `cron/` subdirectory
- Crontab installer: `cron/install_crontab.sh` (idempotent block installer)
- Cron setup how-to: `docs/how-to/cron-setup.md` (user-facing reference)
- Drift check: `/Users/arka/.opencode/scripts/cron_wirings_check.py`
- Skill (deeper version): `skills/add-a-cron-job/SKILL.md`
