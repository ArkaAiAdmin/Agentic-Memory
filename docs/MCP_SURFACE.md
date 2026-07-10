<!-- MCP_TRUTH_SOURCE: docs/_meta.json -->
<!-- Last verified by gen_doc_meta.py — check provenance.last_meta_regenerated for freshness -->
<!-- Truth ranking: _meta.json > AGENTS.md AUTO-GEN > this file. See _meta.json["provenance"]. -->
# Agentic Memory — MCP Surface Reference

> **One-stop quick reference for any agent that uses the agentic-memory MCP tools.**
> Last updated: 2026-07-10. Schema v37.

---

## Mandatory Workflow

> **All agents must follow this workflow. Do not improvise your own save/search flow.**

### Every session MUST do this:

```
1. memory_session_start(query="<current subsystem or task>")
   ↓ Gets context from previous sessions

2. [Do your work]

3. memory_save(category="sessions|lessons|decisions", ...)
   ↓ Save what you learned BEFORE ending the session
```

### The only save tool

**There is exactly one `memory_save` MCP tool.** It is registered as a CORE verb.
Always call it by name from the MCP surface. Do not call `save_pipeline.save_memory` directly.

### Default search behavior

- `memory_search(query="...")` is the **default for ALL memory lookups**.
- Do NOT call `memory_maintenance` for search. It is admin-only.
- Default mode is `hybrid` (FTS5 + vector + KG fusion). Only change mode if hybrid returns bad results.

### When to use memory_maintenance

**Never call memory_maintenance as a default post-task ritual.** The system maintains itself via cron.
Only call it when:
- You need a specific admin operation NOT available as a CORE verb
- You are debugging and need diagnostics
- You need an immediate result that cron hasn't produced yet

### When to save

| Trigger | Tool to call | Category |
|---------|-------------|----------|
| Learned a lesson / fixed a bug | `memory_save` | `lessons` |
| Made an architecture decision | `memory_save` | `decisions` |
| Completed a project milestone | `memory_save` | `projects` |
| User stated a preference | `memory_save` | `preferences` |
| End of session | `memory_save` | `sessions` |
| Flaky test found | `memory_save` | `lessons` (pinned=True) |
| Significant milestone | `memory_save` | `projects` (importance=4) |

### When to search

| Trigger | Tool to call |
|---------|-------------|
| Starting any task | `memory_search(query="<topic>")` |
| Before making a decision | `memory_search(query="<feature> decisions")` |
| Before pushing write-path code | `memory_search(query="save_pipeline saga safety")` |
| Debugging | `memory_search(query="<error> <subsystem>")` |

### When NOT to call a tool

- Do NOT call `memory_organize` unless cron is **not running** or you need an immediate result.
- Do NOT call `memory_maintenance` for routine cleanup.
- Do NOT call `save_pipeline.save_memory` directly — use the MCP verb.
- Do NOT call Python hooks directly — use the MCP surface.

---

## What This System Is

Local-first, MCP-server-shaped memory layer for AI agents. All data lives at
`~/.config/agentic-memory/memory/` (SQLite + markdown files + vector index).

**Surface: 17 CORE verbs + `memory_maintenance` router (escape hatch)**

- CORE tools: visible directly — call them by name.
- `memory_maintenance(operation="...", **kwargs)`: single entry point for all ADMIN/diagnostic tools.
- `memory_advanced(operation="...", **kwargs)`: alias for `memory_maintenance`; interchangeable.

> **Important:** 87 ADMIN + 3 DEPRECATED tools are not removed — they are accessible via the `memory_maintenance`
> router. Calling `memory_maintenance` with an operation name is the supported path. The 3 DEPRECATED tools
> are routed via their replacement verbs and also tracked for audit.

> **There is exactly one `memory_save` MCP tool.** It is registered as a CORE verb in
> `mcp_verbs.py` and exported through `memory_mcp.py`. Do not call `save_pipeline.save_memory`
> directly from agent code — always use the MCP verb so deferred indexing, audit logging,
> and circuit-breaking are applied.

---

## Quick-Start Decision Tree

```
What do you want to do?
│
├─ Save a memory?            → memory_save(content, category, ...)
├─ Search memories?          → memory_search(query, limit, mode, ...)
├─ Read one memory?          → memory_note(note_id, action="read")
├─ Update/delete a memory?   → memory_note(note_id, action="update|delete|patch|supersede|revert_supersede", ...)
├─ Recall recent context?    → memory_recall(query, session_id)
├─ Review beliefs?           → memory_review_beliefs(...)
├─ Curate auto-saves?        → memory_curate_autosave(action="list|promote|discard", ...)
├─ Learn a lesson/skill?     → memory_learn(content, as_skill, skill_name, ...)
├─ Audit recent activity?    → memory_audit(hours, limit, ...)
├─ Run maintenance batch?    → memory_organize(target="safe_default|full|compact|dedup", dry_run)
├─ Share between agents?     → memory_share(note_id, action="list|share|import|stats", share_with)
├─ Explore knowledge graph?  → memory_graph(query, action="explore|traverse|shortest_path|stats", ...)
├─ View profile/stats?       → memory_profile(action="stats|user|agents|skills|arc")
├─ Start a session?          → memory_session_start(query)
├─ Check system health?      → memory_health_check
└─ Advanced / power user?    → memory_advanced(operation="any_admin_operation", **kwargs)
                              or memory_maintenance(operation="any_admin_operation", **kwargs)
```

---

## CORE Verbs — Full Reference

### memory_search
Search memories by semantic + FTS5 hybrid search.

**Args:**
| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| query | str | Yes | — | Natural-language search query |
| limit | int | No | 10 | Max results |
| category | str | No | "" | Filter to category: lessons, decisions, projects, preferences, sessions |
| include_global | bool | No | True | Include global memories |
| mode | str | No | "hybrid" | hybrid, semantic, fts, facts, graph |
| belief_status | str | No | null | For facts mode: active, retracted, deprecated, unconfirmed |
| epistemic_source | str | No | null | For facts mode: agent, auto_save, hook, import, cron |
| fact_type | str | No | null | For facts mode: observation, agent_inference, external_stated, hypothesis, derived |
| memory_source | str | No | null | Filter by source: agent, auto_save, import |
| tenant_id | str | No | "default" | Tenant ID |

**When to use:** This is the primary recall tool. Default for any memory lookup.

---

### memory_save
Save a memory note with sensible defaults.

**Args:**
| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| content | str | Yes | — | Memory content (markdown) |
| category | str | No | "lessons" | lessons, decisions, projects, preferences, sessions |
| title_slug | str | No | "" | Auto-generated if empty |
| tags | list[str] | No | null | Keyword tags |
| pinned | bool | No | False | Pin to hot tier |
| importance | int | No | 3 | 1-5 |
| is_global | bool | No | False | Save to global memory |

**When to use:** After any learning, decision, or event worth remembering. Use `pinned=True` for high-importance permanent notes.

---

### memory_delete
Delete a memory note by ID. Soft-delete by default (recoverable for 30 days).

**Args:**
| Param | Type | Required | Default |
|-------|------|----------|---------|
| note_id | str | Yes | — |
| hard | bool | No | False |

---

### memory_recall
Recall context for the current session or a named thread.

**Args:**
| Param | Type | Required | Default |
|-------|------|----------|---------|
| query | str | No | "" | Empty = recent activity |
| session_id | str | No | "" |
| tenant_id | str | No | "default" |

---

### memory_note
CRUD operations on a specific memory note.

**Args:**
| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| note_id | str | Yes | — | e.g. "lessons/my-note" |
| action | str | No | "read" | read, update, delete, restore, supersede, patch, revert_supersede |
| content | str | No | "" | Required for update |
| category | str | No | "" | For update |
| title_slug | str | No | "" | For update/supersede target |
| tags | list[str] | No | null | For update |
| rationale | str | No | "" | Required for supersede, patch, revert_supersede |
| additions | list[str] | No | null | For patch |
| deletions | list[str] | No | null | For patch |

---

### memory_review_beliefs
Review beliefs that may need agent attention — low confidence, old, or stale.

**Args:**
| Param | Type | Required | Default |
|-------|------|----------|---------|
| min_confidence | float | No | 0.5 |
| belief_status | str | No | "active" |
| older_than_days | float | No | 30.0 |
| limit | int | No | 20 |

---

### memory_curate_autosave
Review auto-saved tool invocations and promote or discard them.

**Args:**
| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| start_date | str | No | "" | ISO date e.g. "2026-06-01" |
| end_date | str | No | "" | ISO date e.g. "2026-07-01" |
| action | str | No | "list" | list, promote, discard |
| note_ids | list[str] | No | null | Required for promote/discard |
| category | str | No | "lessons" | Target category for promotion |

---

### memory_learn
Save a lesson or compile a skill from content.

**Args:**
| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| content | str | Yes | — | |
| as_skill | bool | No | False | Compile as skill |
| skill_name | str | No | "" | Required if as_skill=True |
| category | str | No | "lessons" | |
| tags | list[str] | No | null | |

---

### memory_audit
Review recent memory activity, errors, and system health.

**Args:**
| Param | Type | Required | Default |
|-------|------|----------|---------|
| hours | int | No | 24 |
| limit | int | No | 20 |
| include_errors | bool | No | True |

---

### memory_organize
Run safe memory maintenance batch.

**Args:**
| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| target | str | No | "safe_default" | safe_default, full, compact, dedup |
| dry_run | bool | No | False | |

 Targets:
  - **safe_default**: compact + consolidate + rewrite_links
  - **full**: safe_default + backfill + dedup + purge_expired
  - **compact**: FTS5 compact only
  - **dedup**: KG entity dedup only

> ⚠️ **Automated maintenance:** Most maintenance is already automated via cron (see below).
> Use `memory_organize` only for ad-hoc runs or when cron is not running.

---

### memory_share
Share memories with other agents or view shared pool.

**Args:**
| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| note_id | str | Yes | — | Memory ID to share |
| action | str | No | "list" | list, share, import, stats |
| share_with | str | No | "" | Target agent ID (for action=share) |

---

### memory_graph
Explore the knowledge graph.

**Args:**
| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| query | str | No | "" | For action=explore |
| start | str | No | "" | Starting node (for action=traverse) |
| edge_patterns | str | No | "" | Edge type filter (for action=traverse) |
| max_depth | int | No | 2 | |
| action | str | No | "explore" | explore, traverse, shortest_path, stats |

---

### memory_profile
View user profile, agent scopes, ARC stats, and cached skills.

**Args:**
| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| action | str | No | "stats" | stats, user, agents, skills, arc |
| agent_id | str | No | "" | For action=agents |

---

### memory_session_start
Retrieve the session startup briefing.

**Args:**
| Param | Type | Required | Default |
|-------|------|----------|---------|
| query | str | No | "" | Optional topic filter |

---

### memory_health_check
Unified health-check: returns a JSON dict summarising subsystem state.

Checks DB availability, row counts, vec-index drift, FTS sync status,
connection-pool depth, background-worker liveness, disk space, and
schema version.

**Args:** None.

### memory_advanced (escape hatch)
Power user escape hatch — pass through to any memory_maintenance operation.

**Args:**
| Param | Type | Required | Default |
|-------|------|----------|---------|
| operation | str | Yes | — | Any memory_maintenance operation name |
| kwargs | str | Yes | — | JSON-encoded operation-specific params |

> ⚠️ Prefer a specific verb when one exists (see decision tree above). Use
> `memory_advanced` only when no verb covers your use case.

---

## Admin Tools — Via memory_maintenance Router

All legacy/diagnostic tools are accessible via `memory_maintenance(operation="...", **kwargs)`.

**Common admin operations:**

| Operation | Purpose | Key kwargs |
|-----------|---------|------------|
| `tier_stats` | Hot/warm/cold tier breakdown | — |
| `audit` | Recent activity log | hours, limit |
| `consolidate` | Dedup + contradiction detection | — |
| `rewrite_links` | Fix broken wiki links | — |
| `arc_stats` | ARC stats | — |
| `arc_reset` | Reset ARC (destructive) | conn |
| `review_schedule` | Review due beliefs | — |
| `pinned_decay_check` | Auto-unpin stale notes | dry_run |
| `quality_stats` | Quality gate stats | — |
| `facts_stats` | KG facts stats | — |
| `facts_list` | List KG facts | facts_limit, facts_min_confidence |
| `graph_stats` | KG graph stats | — |
| `profile_stats` | Memory profile stats | — |
| `retention_stats` | Retention stats | — |
| `dedup` | Find near-duplicates or suggest merges | action, threshold |
| `backfill_all` | Rebuild indexes | backfill_mode, source |
| `check_integrity` | DB integrity check | deep |
| `compact` | FTS5 compact | dry_run |
| `detect_contradictions` | Find contradictions | min_confidence, contradiction_mode |
| `auto_summarize` | Summarize long notes | min_length, dry_run |
| `daily_digest` | Daily auto-save digest | date |
| `purge_expired` | Purge soft-deleted notes | — |
| `purge_auto_saves` | Purge stale auto-saves | — |
| `reinforce` | Reinforce beliefs | memory_ids, success |
| `compile_skill` | Compile a skill from memory | lesson_slug, skill_name |
| `ingest` | Ingest a file or web page URL | file_path, url, category, tags |
| `check_concept_drift` | Detect embedding drift | threshold |
| `flags_status` | Feature flags status | — |
| `config_drift` | Config drift across all flags | severity_floor, persist, compare_to_last |
| `phase_errors` | Search phase errors | — |
| `circuit_breaker_status` | Circuit breaker health | limit, since_ts |
| `compliance_check` | Compliance audit | session_id |
| `crdt_sync` | CRDT sync | agent_id, remote_notes_json |
| `crdt_status` | CRDT sync status | — |
| `list_drift_alarms` | List concept drift alarms | acknowledged, alarm_level, limit |
| `temporal_contradictions` | Temporal contradictions | since_ts, until_ts, limit |
| `temporal_query` | Query KG as of a time | as_of, fact_id, query, limit |
| `session_admin_stats` | Database stats for sessions/threads/compactions | type |
| `recall_stats` | Retrieve recall context, trace, or status | action, query, limit |
| `background_task_status` | Status of deferred background tasks | memory_id |
| `policy_hash_status` | Fleet posture diff — local policy hash vs peers | peer_timeout_s, max_concurrent, cache_ttl_s, force_refresh, include_full_policy, since_ts |

**Full list with all parameters:** Call `memory_maintenance(operation="help")` to get a
parameter reference for any admin operation.

> **Fleet policy-hash diffusion:** `policy_hash_status` (Plan 1) returns a
> unified fleet posture diff so an operator can confirm every node runs the
> same drift policy. It computes the local policy hash and then classifies
> each peer as **aligned** (same hash), **divergent** (different hash — with
> `delta_keys`), **unreachable** (HTTP/timeout error), or **pending** (no hash
> yet).
>
> - **Parameters:** `peer_timeout_s=5` (per-peer fetch timeout), `max_concurrent=4`
>   (parallel fetch fan-out), `cache_ttl_s=60` (peer-hash cache TTL),
>   `force_refresh=False` (bypass cache), `include_full_policy=False` (include
>   each peer's full policy dict in the diff), `since_ts=None` (audit filter).
> - **Peers:** read from `SyncManager().status()["peers"]`.
> - **Peer fetch:** the hash for each peer is fetched over HTTP from that
>   peer's sync-server route `GET /crdt/policy_hash` (served by
>   `infra/sync_server.py`; requires sync auth). Optional `?include_full=1`
>   returns the full policy, mirroring the peer-facing admin tool
>   `memory_admin_policy_hash`.
> - **`memory_admin_policy_hash`:** the hidden admin tool (reachable only via
>   the sync-server HTTP route or this `policy_hash_status` op — it is NOT on
>   the direct MCP surface). Returns `{"policy_hash", "scope",
>   "schema_version", ("full_policy")}`.
>
> This is the operator-facing counterpart to the `MEMORY_TOML_HOT_RELOAD`
> opt-in (documented in `docs/reference/configuration.md`): if hot-reload is
> OFF somewhere, that node can silently diverge from the fleet until restart —
> `policy_hash_status` is how you detect it.

---

## Automated Maintenance

> **This system maintains itself. You should not need to call maintenance tools manually.**

The following cron jobs run automatically (installed via `bash cron/install_crontab.sh`):

| Schedule | Cron Job | What it does |
|----------|----------|--------------|
| Every 15 min | `background_worker.py` | Process task queue (backfill, embedding, etc.) |
| Every 15 min | `cron_health_check.py` | FTS drift, KG orphans, circuit breaker |
| Daily 00:00 | `cron_daily_digest` | Roll auto-saves into daily note |
| Daily 00:30 | `cron_purge_auto_saves` | Purge stale inbox items |
| Daily 01:00 Sun | `cron_integrity_check` | DB health + FTS consistency |
| Daily 01:30 | `cron_backfill_all` | Incremental index rebuild |
| Daily 02:00 | `cron_backup` | SQLite backup (7-day retention) |
| Daily 02:15 | `cron_backup_validate` | Verify backup integrity |
| Daily 03:00 | `cron_heartbeat` | Decay, tier assignment, archive |
| Daily 04:00 Sun | `cron_consolidate` | Dedup + contradiction detection |
| Daily 04:30 | `cron_rewrite_links` | Fix broken wiki links |
| Sun 05:00 | `cron_pinned_decay` | Auto-unpin stale notes |
| Sun 06:00 | `cron_concept_drift` | Embedding drift detection |
| Mon 07:00 | `cron_quality_filter` | Quality gate stats |
| Mon 07:30 | `cron_auto_summarize` | Summarize long notes |
| Mon 08:00 | `cron_retention_stats` | Adaptive retention stats |
| Daily 09:00 | `cron_auto_share` | Share opt-in memories |
| Every hour :05 | `cron_sync` | Two-way sync |
| Every hour :15 | `cron_crdt_sync` | Multi-peer CRDT sync |
| Every 5 min | `cron_daemon_watchdog` | Restart crashed daemons |

**Logs:** `memory/worker.log`, `memory/health-check.log`, `memory/heartbeat.log`,
`memory/integrity.log`, `memory/watchdog.log`, `memory/watchdog-daemon.log`

**Check cron status:**
```bash
crontab -l | grep agentic-memory
# or
bash cron/install_crontab.sh --show
```

**Manual maintenance (rare):**
- Only call `memory_maintenance` directly if cron is **not running** or you need
  an immediate result.
- For scheduled cleanup, use `memory_organize(target="safe_default")` as a one-off.
- For full rebuild, use `memory_organize(target="full")`.

---

## Search Modes

| mode | When to use | How it works |
|------|-------------|--------------|
| `hybrid` (default) | General-purpose search | FTS5 keyword + semantic vector fusion (RRF) |
| `semantic` | Conceptual/meaning search | Vector-only (usearch) |
| `fts` | Exact keyword/value search | SQLite FTS5 |
| `facts` | Knowledge graph queries | KG facts with belief/type filters |
| `graph` | Graph exploration | Graph RAG expansion |

**Late interaction (always-on when `MEMORY_LATE_INTERACTION=1`):**
- Character 3-gram overlap + positional proximity — lightweight ColBERT approximation
- No neural model needed
- Returns `avg_dist` per result (mean token-positional distance of best matches)
  — lower values indicate tighter topical coherence

---

## Categories Reference

| Category | Use for |
|----------|---------|
| `lessons` | Learned patterns, gotchas, best practices |
| `decisions` | Architectural or product decisions with rationale |
| `projects` | Periodic project notes (goal, approach, outcome) |
| `preferences` | User/agent preferences that should persist |
| `sessions` | Session-specific context (auto-cleaned) |

---

## Memory ID Format

All memory IDs follow the pattern: `<category>/<title-slug>`
- Auto-generated from content if not specified
- Title slug: lowercase, hyphens, no special chars
- Example: `lessons/path-traversal-fix-2026-06-30`

---

## Auto-Save

- Auto-save is **on by default** since 2026-06-22.
- Auto-saved notes go to `auto_saves/` and are promoted to main categories manually or via `memory_curate_autosave`.
- Daily digest rolls auto-saves into a single note.

**Check auto-save health:**
- `memory_audit(hours=1)` — recent activity including auto-saves
- `memory_curate_autosave(action="list")` — list pending auto-saves

---

## Common Workflows

### After a session:
```
memory_session_start(query="<subsystem>")  # get briefing
memory_organize(target="safe_default")      # run maintenance (usually not needed — cron handles it)
memory_save(category="sessions", importance=3)  # save session summary
```

### After a bug fix:
```
memory_save(category="lessons", tags=["debugging"], importance=4)
memory_learn(content="...", as_skill=False)
```

### After making a decision:
```
memory_save(category="decisions", importance=4, tags=[...])
```

### After finding a flaky test:
```
memory_save(category="lessons", tags=["flaky"], pinned=True)
```

### Periodic checkpoint (weekly):
```
memory_organize(target="safe_default")   # if cron isn't running
memory_maintenance(operation="duplicates", threshold=0.85)
```

---

## Troubleshooting

**"Tool not found" / "Invalid" errors:**
- The tool might be an ADMIN tool. Use `memory_maintenance(operation="...")` instead.
- Check available tools: call `memory_maintenance(operation="help")` for the admin surface.
- Verify the server is running: check `memory/worker.log` and `memory/health-check.log`.

**Empty search results:**
- Try `mode="semantic"` if FTS5 isn't matching
- Check if `include_global=False` is set and you need global results
- Run `memory_maintenance(operation="rebuild", scope="fts")` to rebuild FTS5 index

**Slow searches:**
- Check `memory_maintenance(operation="tier_stats")` — hot tier may be full
- Run `memory_maintenance(operation="pinned_decay_check", dry_run=True)` to see stale pins

**Auto-save not working:**
- Check `memory_audit(hours=1, include_errors=True)` for errors
- Check `memory/worker.log` for daemon status

---

## Schema Version

Current: **v37** (38 migrations, 100% down-migration coverage)
