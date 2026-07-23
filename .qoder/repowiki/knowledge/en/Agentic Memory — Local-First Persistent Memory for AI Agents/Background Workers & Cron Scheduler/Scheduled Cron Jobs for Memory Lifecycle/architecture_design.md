Each file is an independent executable wrapper (`#!/usr/bin/env python3`) whose `main()` acquires a process-wide lock via `_flock.acquire_lock_or_exit('<job_name>')` to prevent concurrent runs, then delegates to a sibling package module:
- `cron_retention_stats.py` → `adaptive_retention.batch_update_retention` + `neural_forget.batch_update_retention`
- `cron_promote_drafts.py` → direct SQLite queries over `memories`, `user_access_log`, `kg_facts`, `memory_chunks` with optional tenant scoping via `infra.tenant_query.install_tenant_context`
- `cron_auto_summarize.py` → `summarization.auto_summarize_long`
- `cron_auto_share.py` → `memory_sharing.auto_share_high_value` (gated by `MEMORY_MULTI_AGENT`)
- `cron_tier_migration.py` → `tier_migration.consolidate_warm_sessions` / `archive_cold_files` / `prune_superseded`

All wrappers bootstrap the repo root on `sys.path` so imports resolve regardless of cwd, set feature flags via `os.environ.setdefault(...)` (e.g. `MEMORY_ADAPTIVE_RETENTION`, `MEMORY_SUMMARIZATION`, `MEMORY_TEMPORAL_TIERS`, `MEMORY_MULTI_AGENT`), and use `infra.infrastructure.resolve_active_memory_dir` (or `MEMORY_DB_PATH` env) to locate the target database. Each script exits non-zero on exception and prints a one-line summary of counts (scanned/promoted/skipped/failed).