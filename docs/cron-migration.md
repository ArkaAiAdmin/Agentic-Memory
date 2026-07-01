# Cron Migration (Phase B)

## What Changed

All 30 cron jobs now route through a task queue instead of running standalone scripts directly from crontab.

### Before (pre-Phase B)

```
crontab → cron/cron_compact.py
crontab → cron/cron_heartbeat.py
crontab → cron/cron_sync.py
... 30 direct script calls
```

Each script had its own flock, DB connection, locking, and error handling. Cron fired scripts at fixed times regardless of system load.

### After (Phase B)

```
crontab → cron/enqueue_task.py --task-type cron_compact
crontab → cron/enqueue_task.py --task-type cron_heartbeat
crontab → cron/enqueue_task.py --task-type cron_sync
... 30 enqueue calls
         ↓
   task_queue (SQLite)
         ↓
   background_worker.py (every 15 min, drain mode)
         ↓
   CRON_SCRIPT_MAP → runs original script
```

## Architecture

- `cron/enqueue_task.py` — thin CLI wrapper that inserts a task into `task_queue`
- `background/background_worker.py` — main loop pops tasks from the queue and dispatches via `CRON_SCRIPT_MAP` (line 344)
- `CRON_SCRIPT_MAP` — dict mapping task types (e.g. `"cron_compact"`) to script paths (`"cron/cron_compact.py"`)

### Tasks kept direct (not enqueued)

These run directly from crontab because they need real-time execution or can't tolerate queue latency:

- `background_worker.py` — the worker itself (can't enqueue itself)
- `cron/cron_health_check.py` — health check needs immediate feedback
- `cron/cron_watchdog.py` — watchdog must run on schedule
- `cron/cron_daemon_watchdog.py` — daemon watchdog same constraint

### Schema

```
task_queue table:
  id          INTEGER PRIMARY KEY
  task_type   TEXT NOT NULL       -- matches CRON_SCRIPT_MAP key
  payload     TEXT (JSON)         -- args, env overrides
  priority    INTEGER DEFAULT 0   -- higher = sooner
  status      TEXT DEFAULT 'pending'  -- pending|running|completed|failed
  created_at  TEXT (UTC)
  started_at  TEXT (UTC)
  completed_at TEXT (UTC)
  error       TEXT
```

## Benefits

- **Debouncing** — `enqueue_task.py --debounce-seconds 3600` skips enqueue if the same task type completed within N seconds
- **Queue depth control** — `--max-queue-size 500` rejects new tasks when backlog is too deep
- **Rate limiting** — background worker consumes at a controlled pace (`--max-tasks=50` per cycle)
- **Observability** — `monitor_task_queue.py` alerts on backlog depth and stale tasks
- **No overlapping runs** — task queue ensures at-most-once execution per row

## Adding a New Cron Task

1. Write the script (or use an existing one)
2. Add an entry to `CRON_SCRIPT_MAP` in `background/background_worker.py`
3. Add an `enqueue_task.py --task-type <name>` line in `cron/install_crontab.sh`'s `build_block()` function
4. Run `bash cron/install_crontab.sh` to apply
