# Maintenance Operations Runbook

> Audience: agents and operators using `memory_advanced(operation=...)`.
> Full tool surface with signatures: `docs/MCP_SURFACE.md` (auto-generated).
> Rule 21 governs this page: **cron + the background worker own routine
> maintenance.** Reach for these operations only when cron is down, a
> diagnostic is needed now, or you are fixing something specific.

## Diagnosis path (when something seems wrong)

Run top-down; stop at the first answer.

| Step | Operation | Answers |
|---|---|---|
| 1 | `memory_system_health` | Overall green/yellow/red across DB, search, worker, crons, disk |
| 2 | `memory_health_check` | Row counts, vec drift, FTS sync, pool depth, journal health |
| 3 | `memory_audit_query` | What did recent memory activity actually do (errors included) |
| 4 | `memory_auto_save_status` | Is the auto-save daemon alive, inbox depth, last flush |
| 5 | `memory_integrity_check` | Deep corruption scan (0 critical = OK) |

Logs worth reading before any repair: `memory/worker.log`,
`memory/watchdog-daemon.log`, `memory/scheduler.log`.

## Safe to run manually (non-destructive)

- **Search quality triage:** `memory_search_stats`, `memory_quality_filter_status`, `memory_answer_rerank_stats`
- **Skill surface:** `memory_list_skills`, `memory_extract_skills` (idempotent, gated), `memory_skill_decay_dry_run`
- **Graph inspection:** `memory_graph_*` read paths, `memory_facts_*`
- **Sync/coordination:** `memory_share(action="stats")`, `heartbeat`, agent profile queries
- **Retention reporting:** `memory_retention_stats`, `memory_arc_stats`

## Requires intent — run only when fixing something specific

- `memory_rebuild_fts` / `memory_backfill_all --incremental` — after schema
  or embedding-model changes. Rebuild order is Rule 3: embeddings/chunks
  first, vec index LAST.
- `memory_compact` — after large soft-delete waves (e.g. post-consolidation)
  to reclaim FTS5 space.
- `memory_resolve_contradictions --dry-run` — review first, apply second.
- `memory_repair_unindexed` — normally cron-owned (6h); manual run is safe
  and idempotent.

## Destructive — confirmation gate applies

These refuse without `confirm=True` via `memory_advanced`:

- `purge_expired`, `okf_export`, `crdt_sync`, `dedup_entities`,
  entity merge/delete/prune/archive
- Anything that drops rows: read the operation's docstring, verify the
  recovery story (soft-delete window / `.conflict-*` files) first.

## Do NOT run manually (cron-owned)

Per Rule 21 the following run on schedule and running them by hand adds
lock contention, not freshness: journal drain (`journal_reconciler`),
queue drain (`background_worker --drain`), consolidation sweep,
`daily_digest`, `auto_share`, `sync_usage`, watchdogs. If they appear
broken, diagnose (table above) — don't double-run them.

## Disaster Recovery & Backup Restoration

For step-by-step procedures on restoring database snapshots or point-in-time archives from `$MEMORY_HOME/backups/` into `$MEMORY_HOME/data/`, see:
- [restore-backup.md](file:///Users/arka/.config/agentic-memory/docs/runbooks/restore-backup.md)

