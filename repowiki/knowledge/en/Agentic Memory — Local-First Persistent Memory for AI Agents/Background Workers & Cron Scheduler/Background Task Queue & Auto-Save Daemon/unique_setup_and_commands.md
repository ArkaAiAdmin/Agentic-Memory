Worker entry points are invoked as standalone scripts:
- `python background/background_worker.py --once [--type=<task>] [--drain] [--interval=N] [--max-tasks=N]`
- `python background/fleet_worker.py <journal_path> <target_base> <worker_id> <n_workers>`
- `python background/auto_save.py daemon` (spawns the auto-save daemon)
Key env vars: `MEMORY_DB_PATH`, `MEMORY_WORKER_INTERVAL`, `MEMORY_WORKER_MAX_TASKS`, `MEMORY_WORKER_BATCH_SIZE`, `MEMORY_WORKER_TASK_TIMEOUT_S`, `MEMORY_VEC_REBUILD_THRESHOLD`, `MEMORY_ADAPTIVE_RETENTION`, `AUTO_SAVE_TOOL_ALLOWLIST` / `AUTO_SAVE_TOOL_DENYLIST`, `AUTO_SAVE_INBOX_MAX_BYTES`.