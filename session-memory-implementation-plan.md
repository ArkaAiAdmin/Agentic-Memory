# Session Memory System — Implementation Plan

> Version: 2026-06-26  
> Status: Draft for review  
> Estimated total: 7 sprints (~5–6 weeks at 1 sprint/week)

---

## Design Corrections Applied

The following issues from the architecture review are resolved in this plan. They are **non-negotiable constraints**, not optional refinements.

| # | Issue From Review | Resolution in This Plan |
|---|---|---|
| C-1 | SessionManager writes bypass `save_memory` (Rule #1 violation) | All DB writes from SessionManager route through `save_memory` via a new internal category pass-through. SessionManager never opens a raw connection to write session/thread/compaction rows. |
| C-2 | Decision thread extraction requires LLM calls on every save | Deferred to Sprint 6 as opt-in behind `MEMORY_SESSION_DECISION_LLM=1`. Heuristic-only path ships in Sprint 4. No LLM call in the default path. |
| C-3 | Thread-boosting silently biases search results | Implemented as a separate MCP tool `memory_thread_context(session_id)` rather than a silent search modifier. Users call it explicitly. |
| C-4 | Backfill inference of `parent_session_id` from filenames is fragile | Backfill uses `NULL` for unlinkable sessions. Parent links are only created when compaction notes contain an explicit `parent_session_id` field (added in Sprint 1). |
| C-5 | `thread_events.content` bloat | `thread_events` stores a 300-char summary inline; full content is accessed via `memory_id` FK. Events without a linked memory store the full text only if under 300 chars. |
| C-6 | No CRDT merge semantics for sessions/threads tables | Sprint 1 schema adds `version_vector TEXT NOT NULL DEFAULT '{}'` to `sessions`, `decision_threads`, and `thread_events`. Merge behavior is add-wins (2P-Set semantics) for threads; session rows are last-writer-wins per field. |
| C-7 | No `session_id` FK on `thread_events` | Added in Sprint 1 schema. Thread events belong to both a thread and a session; the session FK enables efficient session-scoped queries without a join through threads. |
| C-8 | Unrestricted JSON in metadata columns | Sprint 1 schema adds `CHECK (json_valid(metadata))` constraints (SQLite 3.38+). PII scrub helper runs on all metadata writes in Sprint 2. |

---

## Sprint 0: Preparation (1–2 days, no code)

### Objectives
- Establish branch, feature flags, and test infrastructure before any schema change.

### Todo

| # | Task | Command / File | Done When |
|---|---|---|---|
| 0.1 | Create feature branch `feature/session-memory-v22` from `main` | `git checkout -b feature/session-memory-v22` | Branch exists |
| 0.2 | Add feature flag `MEMORY_SESSION_MEMORY=0` to `memory.toml` and `config.py` defaults | `memory.toml`, `config.py` | Flag reads as `0` by default |
| 0.3 | Add `MEMORY_SESSION_DECISION_LLM=0` flag (deferred opt-in) | same files | Flag reads as `0` by default |
| 0.4 | Verify `SCHEMA_VERSION = 21` in `migration_runner.py` | `migration_runner.py:79` | Confirmed |
| 0.5 | Confirm all 26 tests in `eval/test_pipeline.py` still pass | `./venv/bin/python -m pytest eval/test_pipeline.py -q` | 26/26 pass |
| 0.6 | Create skeleton `eval/test_session_manager.py`, `eval/test_session_migration.py` | new files | Files exist, all tests xfail |

---

## Sprint 1: Schema v22 + Migration (3–4 days)

### Objective
Add the four new tables with full FK integrity, CRDT-ready version vectors, JSON validation, and a working forward+down migration.

### New / Modified Files
- `migrations/022_session_memory.sql` (new)
- `migrations/022_session_memory.down.sql` (new)
- `migration_runner.py` (bump SCHEMA_VERSION)
- `docs/reference/schema.md` (document v22 tables)

### Todo

| # | Task | Detail | Done When |
|---|---|---|---|
| 1.1 | Write `022_session_memory.sql` forward migration | Tables: `sessions`, `decision_threads`, `thread_events`, `session_compaction_log`. All include `version_vector TEXT NOT NULL DEFAULT '{}'`. Thread events have both `thread_id` and `session_id` FK. All `metadata JSON` columns have `CHECK (json_valid(metadata))`. Indexes as specified in proposal. | Migration file parses cleanly |
| 1.2 | Write `022_session_memory.down.sql` | DROP TABLE in reverse order: `thread_events`, `session_compaction_log`, `decision_threads`, `sessions`. No data migration needed (new tables). | Down migration runs without error |
| 1.3 | Bump `SCHEMA_VERSION` to 22 | `migration_runner.py:79` | Reads 22 |
| 1.4 | Write migration tests | `eval/test_session_migration.py`: test forward from 21→22, test down from 22→21, test idempotency (running twice is no-op), test all FK constraints reject invalid refs | All pass |
| 1.5 | Run full migration against test DB | `python -c "from migration_runner import run_migrations; ..."` with tempfile DB | All 4 tables created, indexes present |
| 1.6 | Update `docs/reference/schema.md` | Document all 4 new tables with column descriptions and FK relationships | Doc updated |
| 1.7 | Run existing test suite | `./venv/bin/python -m pytest eval/ -q` | No regressions |

### Schema (final)

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    project_root TEXT,
    agent_id TEXT,
    parent_session_id TEXT REFERENCES sessions(id),
    summary_note_id TEXT REFERENCES memories(id),
    status TEXT DEFAULT 'active' CHECK (status IN ('active','compacted','ended','failed')),
    version_vector TEXT NOT NULL DEFAULT '{}',
    metadata JSON DEFAULT '{}' CHECK (json_valid(metadata))
);

CREATE TABLE decision_threads (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    title TEXT NOT NULL,
    status TEXT DEFAULT 'open' CHECK (status IN ('open','resolved','superseded','deferred')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    superseded_by TEXT REFERENCES decision_threads(id),
    version_vector TEXT NOT NULL DEFAULT '{}',
    metadata JSON DEFAULT '{}' CHECK (json_valid(metadata))
);

CREATE TABLE thread_events (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES decision_threads(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('claim','evidence','decision','question','pivot')),
    content TEXT NOT NULL,
    content_summary TEXT DEFAULT '',          -- 300-char summary for listing
    memory_id TEXT REFERENCES memories(id),
    confidence REAL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    created_at TEXT NOT NULL,
    version_vector TEXT NOT NULL DEFAULT '{}',
    UNIQUE(thread_id, seq)
);

CREATE TABLE session_compaction_log (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    compacted_at TEXT NOT NULL,
    tokens_before INTEGER,
    tokens_after INTEGER,
    summary_note_id TEXT REFERENCES memories(id),
    recovered_note_ids TEXT NOT NULL,         -- JSON array of note_id strings
    metadata JSON DEFAULT '{}' CHECK (json_valid(metadata)),
    version_vector TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_sessions_project ON sessions(project_root, started_at DESC);
CREATE INDEX idx_threads_session ON decision_threads(session_id, status);
CREATE INDEX idx_thread_events_thread ON thread_events(thread_id, seq);
CREATE INDEX idx_compaction_session ON session_compaction_log(session_id);
```

---

## Sprint 2: SessionManager Core (3–4 days)

### Objective
Build `SessionManager` as a pure orchestration layer that calls `save_memory` for all persistent writes.

### New / Modified Files
- `session_manager.py` (new)
- `session_models.py` (new — dataclasses)
- `eval/test_session_manager.py` (expand from skeleton)

### Critical Design Rule
**SessionManager NEVER writes directly to session/thread/compaction tables.** It calls `save_memory(category="sessions", ...)` for session summaries, `save_memory(category="decisions", ...)` for thread events, and a new internal helper `_save_system_record(table, row)` which itself calls `save_memory` with a synthetic category. This ensures every write passes through the saga, FTS update, cache invalidation, and contradiction check.

### Todo

| # | Task | Detail | Done When |
|---|---|---|---|
| 2.1 | Create `session_models.py` | Dataclasses: `Session`, `DecisionThread`, `ThreadEvent`, `CompactionLog`, `SessionContext`. All fields typed. No DB logic here. | Models import cleanly |
| 2.2 | Add `_save_system_record()` internal helper in `session_manager.py` | Calls `save_memory` with `category="system"`, `tags=["session","internal"]`, `pinned=False`. Content is a YAML/JSON structured payload. This is the **only** write path from SessionManager to the DB. | Helper exists, calls save_memory |
| 2.3 | Implement `SessionManager.start_session()` | 1. Query for active session in same project (`status='active'`) — crash recovery. 2. If found, return it (resume). 3. Else insert new session row (via `_save_system_record`). 4. Load open decision threads for project. 5. Return `SessionContext`. | Returns SessionContext; crash recovery works |
| 2.4 | Implement `SessionManager.record_event()` | Validate event_type enum, assign `seq = max(existing seq) + 1` (with flock), store 300-char summary in `content_summary`. Call `_save_system_record` for the event row. | Events appended with correct seq |
| 2.5 | Implement `SessionManager.resolve_thread()` | Update thread status, set `resolved_at`, optional `superseded_by` FK. Via `_save_system_record`. | Thread status transitions correctly |
| 2.6 | Implement `SessionManager.end_session()` | 1. Generate summary via lightweight heuristic (no LLM — concatenate thread resolutions). 2. Call `save_memory(category="sessions", pinned=True)` for summary. 3. Set `session.summary_note_id`. 4. Close open threads (status → 'deferred'). 5. Set `session.ended_at`, `status='ended'`. All via `_save_system_record`. | Session summary saved as pinned memory |
| 2.7 | Implement `SessionManager.compact_session()` | Accept `tokens_before`, `tokens_after`, `summary_note_id`, `recovered_note_ids`. Insert into `session_compaction_log` via `_save_system_record`. Update session `status='compacted'`, set `parent_session_id` for next session. | Compaction logged; parent link set |
| 2.8 | Add PII scrub helper | `_scrub_metadata(d: dict) -> dict`: recursively strip keys matching `password`, `token`, `secret`, `api_key`, `auth`, `credential` (case-insensitive). Called on all metadata dicts before `_save_system_record`. | PII keys removed from metadata |
| 2.9 | Unit tests | `eval/test_session_manager.py`: test start/resume, event append, thread resolve, session end, compaction, PII scrub, crash recovery (active session found). All use tempfile DB. | All pass |

---

## Sprint 3: Bidirectional Hooks (2–3 days)

### Objective
Wire SessionManager into the existing hook system. No new hooks — enhance existing ones to use the new session entity.

### Modified Files
- `hooks/memory-session-start.py` (enhance)
- `hooks/memory-session-end.py` (enhance)
- `memory_bootstrap.py` (update to read session table)

### Todo

| # | Task | Detail | Done When |
|---|---|---|---|
| 3.1 | Enhance `memory-session-start.py` Phase 1 | Replace `_load_last_session_save()` with `SessionManager.start_session(project_root, agent_id, parent_session_id=...)`. The returned `SessionContext` includes `session`, `active_threads`, `recent_kg_entities`. | Hook starts session entity on SessionStart |
| 3.2 | Enhance `memory-session-end.py` `Stop` handler | Call `SessionManager.end_session(session_id, summary_content)`. Summary content = concatenation of recent auto-save summaries + tool-call summary (no LLM). | Session entity closed on Stop |
| 3.3 | Add session_id to hook_data propagation | `memory-session-start.py` stores `session_id` in a per-session state file (`memory/sessions/.current_session.json` with flock). Other hooks (proactive-context, search-on-demand) can read it. | Session ID available to all hooks in same session |
| 3.4 | Update `memory_bootstrap.py` | Add `_get_recent_sessions(project_root, limit=3)` — reads `sessions` table via direct SQL (read-only, no save_memory needed for reads). Prepends session history to bootstrap briefing. | Bootstrap includes session history |
| 3.5 | Update `context_monitor.py` `pre_compaction()` | Call `SessionManager.compact_session()` after writing the compaction note. Pass `tokens_before` (from context estimate), `tokens_after` (from preserved note count × avg tokens), `summary_note_id` (the note just saved), `recovered_note_ids` (list of note IDs from auto-saves). | Compaction logged to DB |
| 3.6 | Integration test | Simulate: session start → tool use → session end. Assert: session row exists with status='ended', summary_note_id set, threads deferred. | Test passes |

---

## Sprint 4: Decision Thread Extraction — Heuristic Only (3 days)

### Objective
Track decisions without LLM calls. Pattern matching only. LLM enrichment deferred.

### New / Modified Files
- `save/decision_extraction.py` (new)
- `save_pipeline.py` (integrate into `_run_post_save_hooks`)
- `eval/test_decision_extraction.py` (new)

### Todo

| # | Task | Detail | Done When |
|---|---|---|---|
| 4.1 | Create `save/decision_extraction.py` | `_extract_decision_candidates(content: str, category: str) -> list[DecisionCandidate]`. Heuristic patterns only: regex for `(?:I\|we\|the team) (?:decided\|chose\|picked\|went with)`, ADR markers (`# ADR`, `## Decision`), RFC markers, tradeoff table headers (`\| Option \|`), and resolution markers (`**Decision:**`, `→ chosen`). No LLM calls. | Heuristic extracts test candidates |
| 4.2 | Define `DECISION_CATEGORIES` | `{"decisions", "lessons", "projects", "architecture"}` in `config.py`. | Set exists |
| 4.3 | Integrate into `_run_post_save_hooks` | After `_hook_auto_backlink_with_flush`, call `_track_decisions(note_id, content, category)`. `_track_decisions`: extract candidates → for each, call `SessionManager.record_event` or `resolve_thread`. | Decisions tracked on save |
| 4.4 | Thread continuity across sessions | When `start_session()` loads open threads, their last 3 events are included in the `SessionContext`. Session start briefing shows: "Continuing: [thread title] — last: [event summary]" | Session briefing includes thread context |
| 4.5 | Unit tests | `eval/test_decision_extraction.py`: test each heuristic pattern, test false-positive rejection, test category gating, test thread resolution on save | All pass |
| 4.6 | Run full test suite | `./venv/bin/python -m pytest eval/ -q` | No regressions |

---

## Sprint 5: Session-Aware Search — Separate Tool (2–3 days)

### Objective
Expose session and thread context as an explicit retrieval tool. Do NOT silently boost search results.

### New / Modified Files
- `mcp_session.py` (new — MCP tool module)
- `tool_registry.py` (register new CORE tool)
- `eval/test_session_search.py` (new)

### New MCP Tool

```python
@mcp.tool()
def memory_thread_context(
    session_id: str = "",
    thread_id: str = "",
    include_events: bool = True,
    event_limit: int = 10,
) -> dict:
    """Return active decision threads and recent events for a session or thread.

    This is an explicit retrieval tool — it does not modify search_memories behavior.
    Call it when you need decision context alongside memory results.
    """
```

### Todo

| # | Task | Detail | Done When |
|---|---|---|---|
| 5.1 | Create `mcp_session.py` | Tools: `memory_thread_context`, `memory_list_threads(session_id, status)`, `memory_resolve_thread(thread_id, resolution)`. All CORE tools. | Module imports, tools registered |
| 5.2 | Register in `tool_registry.py` | Add 3 tools to `CORE_TOOLS` list (was 15, now 18). | Registry updated |
| 5.3 | Implement `memory_thread_context` | Query: load threads for session (or specific thread), load last N events per thread, load linked memories. Return structured dict. | Returns thread+event+memory data |
| 5.4 | Implement `memory_list_threads` | Filter by session_id + optional status. Return lightweight thread list for overview. | Returns filtered thread list |
| 5.5 | Implement `memory_resolve_thread` | Call `SessionManager.resolve_thread()`. Via `save_memory` path (call through MCP → SessionManager). | Thread resolves via MCP call |
| 5.6 | Unit tests | `eval/test_session_search.py`: test thread_context with events, test list_threads filter, test resolve via MCP | All pass |
| 5.7 | Update `tool_registry.py` count check | Update `_CORE_TOOL_COUNT = 18`, `_ADMIN_TOOL_COUNT = 70` | Counts reflect reality |

---

## Sprint 6: LLM Decision Extraction (Opt-In, 2 days)

### Objective
Optional LLM enrichment for decision candidates. Off by default. No impact on default path.

### Gate
Only activated when `MEMORY_SESSION_DECISION_LLM=1` in `memory.toml`.

### New / Modified Files
- `save/decision_extraction.py` (add LLM path)
- `eval/test_decision_extraction.py` (add LLM-gated tests)

### Todo

| # | Task | Detail | Done When |
|---|---|---|---|
| 6.1 | Add LLM enrichment path | `_enrich_candidates_with_llm(candidates, content)` — called only when flag is set. Prompt: "From this note, extract structured decisions: title, claim, alternatives considered, confidence (0–1). Return JSON." | LLM path calls without error when flag=1 |
| 6.2 | Add token/time budget guard | Max 200 tokens output, 5s timeout. On failure: fall back to heuristic-only candidate (never block the save). | Timeout tested, fallback works |
| 6.3 | Add LLM-gated tests | Tests marked `@pytest.mark.skipif(not os.getenv("MEMORY_SESSION_DECISION_LLM"), reason="LLM extraction disabled")` | Tests pass when flag=1, skip when flag=0 |
| 6.4 | Document flag in `memory.toml` example | Add section with description, default, and tradeoff note ("adds ~100–200ms per save in decisions/lessons/projects/architecture categories") | Documented |

---

## Sprint 7: Compaction as First-Class Event + Admin Tools (3 days)

### Objective
Wire compaction logging through SessionManager. Add admin tools for session/thread observability. Complete the Phase 5 + Phase 7 work from the proposal.

### New / Modified Files
- `mcp_maintenance.py` (add session/thread ops)
- `context_monitor.py` (wire compact_session call)
- `mcp_rebuild.py` (show compaction stats)
- `eval/test_session_admin.py` (new)

### New Admin Operations

| Operation | What it does |
|---|---|
| `memory_maintenance(operation="session_stats")` | Count of sessions by status, avg session duration, compaction count, avg tokens preserved |
| `memory_maintenance(operation="thread_stats")` | Count of threads by status, avg resolution time (open→resolved), supersession rate |
| `memory_maintenance(operation="compaction_stats")` | Total compactions, avg token delta, sessions without compaction log (zombie detection) |
| `memory_maintenance(operation="list_active_threads")` | Filter: project_root, status, agent_id. Returns thread_id, title, last_event_at, event_count |
| `memory_maintenance(operation="recover_session")` | Resume from parent_session_id: load parent session summary + open threads, return recovery briefing |

### Todo

| # | Task | Detail | Done When |
|---|---|---|---|
| 7.1 | Wire `context_monitor.py` → `SessionManager.compact_session` | After `pre_compaction()` saves the pinned note, call `SessionManager.compact_session(session_id, tokens_before, tokens_after, summary_note_id, recovered_note_ids)` | Compaction logged to DB on every compaction |
| 7.2 | Add `session_stats` op | Query sessions table with GROUP BY status. Return summary dict. | Admin tool returns stats |
| 7.3 | Add `thread_stats` op | Query decision_threads + thread_events. Compute avg resolution time. | Admin tool returns stats |
| 7.4 | Add `compaction_stats` op | Query session_compaction_log. Identify sessions with status='active' but no compaction log older than 24h (zombie alert). | Admin tool returns stats + zombie list |
| 7.5 | Add `list_active_threads` op | Filter by project_root, status, agent_id via sessions FK chain. | Admin tool returns filtered threads |
| 7.6 | Add `recover_session` op | Given `session_id`, walk `parent_session_id` chain to root, load summary notes + open threads, return recovery briefing. | Admin tool returns recovery context |
| 7.7 | Add session-aware `memory_recall_context` enhancement | `memory_recall_context(session_id=...)` loads active threads for the session and prepends thread context to the recall output. Uses `SessionManager` read path (no new writes). | Recall includes thread context when session_id given |
| 7.8 | Backfill script | `backfill/session_backfill.py`: cluster existing `sessions/*.md` files by date proximity → create session rows. `parent_session_id` only set when compaction note contains explicit `parent_session_id` field. `--dry-run` flag required. Run with `--incremental` after dry-run. | Backfill script runs, produces session rows |
| 7.9 | Zombie alert in cron | `cron/cron_session_health.py` (new): flag sessions with `status='active'` and `started_at > 24h ago` and no compaction log. Write to `memory/worker.log`. | Cron job exists, flags zombies |
| 7.10 | End-to-end integration test | Simulate: start session → save decisions → compact → start child session → verify parent link, thread continuity, compaction log entry. | E2E test passes |
| 7.11 | Full test suite | `./venv/bin/python -m pytest eval/ -q` | No regressions, coverage ≥ 70% for new modules |
| 7.12 | Update `docs/architecture.md` | Add "Session Memory System" section covering: session lifecycle, decision threads, compaction as event, hooks, search integration. | Doc section complete |

---

## Rollout Gate Checklist

Before flipping `MEMORY_SESSION_MEMORY=1`:

- [ ] All 11 new/expanded test files pass: `eval/test_session_migration.py`, `eval/test_session_manager.py`, `eval/test_decision_extraction.py`, `eval/test_session_search.py`, `eval/test_session_admin.py`, plus all existing 183 test files
- [ ] `SCHEMA_VERSION = 22` migration runs clean on a copy of production DB (not on production itself — use a snapshot)
- [ ] Shadow mode validated: run with `MEMORY_SESSION_MEMORY=1` + `MEMORY_SESSION_SHADOW=1` (SessionManager runs but `_save_system_record` is a no-op). Compare session table output to hook log output from current system. Match rate > 95%.
- [ ] Canary: enable for 1 project for 1 week. Validate: session start/end logs, thread creation on decisions, compaction entries appear.
- [ ] Document rollback: `migrate_down(conn, target_version=21)` reverses cleanly.

---

## Risk Register (resolved or mitigated)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Rule #1 violation (bypass save_memory) | Was high | Critical | Resolved: all SessionManager writes route through `save_memory` via `_save_system_record` |
| LLM cost on every save | Was high | Medium | Deferred to opt-in Sprint 6; default path is heuristic-only |
| Thread seq conflicts under concurrent writes | Medium | High | `version_vector` on thread_events; seq assignment uses flock; merge is add-wins (concurrent events both preserved, seq gap tolerated) |
| PII in metadata JSON | Low | High | `_scrub_metadata` runs on all metadata writes in Sprint 2 |
| Zombie sessions (crashes during session) | Medium | Low | Crash recovery in `start_session()` finds active sessions and resumes them; cron alert catches survivors > 24h |
| Backfill creates wrong parent links | Medium | Medium | Backfill only sets `parent_session_id` when compaction note explicitly contains it; otherwise NULL |

---

## Files Summary

| File | Sprint | New/Modified |
|---|---|---|
| `migrations/022_session_memory.sql` | 1 | New |
| `migrations/022_session_memory.down.sql` | 1 | New |
| `migration_runner.py` | 1 | Modified (bump) |
| `session_models.py` | 2 | New |
| `session_manager.py` | 2 | New |
| `save/decision_extraction.py` | 4 | New |
| `save_pipeline.py` | 4 | Modified (hook integration) |
| `hooks/memory-session-start.py` | 3 | Modified |
| `hooks/memory-session-end.py` | 3 | Modified |
| `memory_bootstrap.py` | 3 | Modified |
| `context_monitor.py` | 7 | Modified |
| `mcp_session.py` | 5 | New |
| `mcp_maintenance.py` | 7 | Modified |
| `mcp_rebuild.py` | 7 | Modified |
| `backfill/session_backfill.py` | 7 | New |
| `cron/cron_session_health.py` | 7 | New |
| `tool_registry.py` | 5 | Modified |
| `docs/reference/schema.md` | 1 | Modified |
| `docs/architecture.md` | 7 | Modified |
| `memory.toml` | 0 | Modified (add flags) |
| `config.py` | 0 | Modified (add flags) |
| `eval/test_session_migration.py` | 1 | New |
| `eval/test_session_manager.py` | 2 | New |
| `eval/test_decision_extraction.py` | 4+6 | New |
| `eval/test_session_search.py` | 5 | New |
| `eval/test_session_admin.py` | 7 | New |

**Total: 7 new modules, 10 modified modules, 5 new test files, 2 migration files, 2 doc updates.**

---

## Success Criteria (from proposal, validated)

| Metric | Target | Measurement |
|---|---|---|
| Session start latency | < 200ms | `time.time()` around SessionManager.start_session in tests |
| Decision thread continuity | 100% explicit parent links | FK integrity test: every `superseded_by` and `parent_session_id` references a real row |
| Compaction traceability | 100% logged | Every call to `pre_compaction()` produces a `session_compaction_log` row |
| Zombie sessions | < 5% | `session_stats` op: count(active > 24h) / count(active) |
| Thread resolution rate | > 80% within 3 sessions | `thread_stats` op: count(resolved within 3 sessions) / count(created) |
