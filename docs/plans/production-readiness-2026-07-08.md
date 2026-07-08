# Production Readiness Implementation Plan
**Codename:** FORGE  
**Created:** 2026-07-08  
**Status:** In execution — 4 parallel streams active  
**Target branch:** `feat/production-readiness-forge` off current `main`

---

## Executive Summary

Four independent work streams dispatched in parallel. No inter-stream dependencies. Each stream is a complete, verifiable unit of work.

| Stream | Scope | Effort | Risk | Parallel-safe |
|--------|-------|--------|------|---------------|
| **A — Config Refactor** | 130 flat fields → nested dataclasses, full backwards compat | 2–3 days | Low | Yes |
| **B — Code Hygiene** | Dead code removal + orchestrator.py docstring phase | 1 day | Low | Yes |
| **C — Production Readiness** | docs/production_readiness.md + CI validation tests | 2–3 days | Low | Yes |
| **D — Polish & Story** | TS harness abstraction, SDK versioning, README, CrewAI upgrade | 3–4 days | Medium | Yes (D1/D2/D3 independent) |

**Total effort:** ~7 working days across 4 parallel tracks.  
**Completion gate:** Full suite (0 failures) + mypy + ruff green on every modified file before merge.

---

## Stream A — Config Refactor

**Goal:** Group 130 flat `MemoryConfig` fields into logical nested dataclasses without changing any behavior or TOML key.

### Provenance

`infra/config.py` lines 298–401: `MemoryConfig` is a `frozen=True` dataclass with 130 fields across ~30 logical groups. Callers access fields via dot-notation (`cfg.temporal_half_life`, `cfg.write_journal`, etc.). The TOML parser (`_parse_toml`) maps flat TOML keys (e.g., `[search] temporal_half_life = 180`) directly to the flat dataclass via `cfg.set(**section_dict)`.

### Design

**Step A1 — Define nested config dataclasses**

File: `infra/config.py` (insert before `MemoryConfig`)

```python
@dataclass(frozen=True)
class GeneralDBConfig:
    db_path: str = "memory/memory.db"
    wal_checkpoint_startup: bool = True
    wal_checkpoint_interval_s: int = 300
    mmap_size: int = 268_435_456
    unindexed_safety_net_limit: int = 1000
    db_pool_size: int = 24
    agent_id: str = ""

@dataclass(frozen=True)
class SearchConfig:
    temporal_half_life: float = 180.0
    temporal_decay_mode: str = "exponential"
    late_interaction: bool = True
    knowledge_graph: bool = True
    graph_rag_hops: int = 3
    graph_rag_expansions: int = 5
    embedding_score_threshold: float = 0.25
    kg_llm_fallback_min_entities: int = 2
    rerank_weights: str = ""
    query_type_weights: str = ""
    query_cache: bool = True
    search_parallel_enabled: bool = True
    reranker_disabled: bool = False
    llm_allow_remote_code: bool = False
    deep_rerank_timeout: float = 30.0
    contextual_retrieval: bool = True
    contextual_enrichment: bool = True
    forgetting_curve: bool = True
    forgetting_curve_half_life: float = 30.0
    vec_rebuild_threshold: int = 15
    vec_rebuild_adaptive: bool = True

@dataclass(frozen=True)
class KGConfig:
    entity_min_occurrences: int = 2
    kg_coccurr_entity_cap: int = 20
    kg_edge_weight_increment: float = 0.1
    kg_edge_weight_cap: float = 10.0
    ner_spacy_enabled: bool = False   # was in features, moved here

@dataclass(frozen=True)
class GraphCacheConfig:
    graph_cache_max: int = 50
    graph_cache_ttl_s: float = 60.0

@dataclass(frozen=True)
class WritePipelineConfig:
    write_journal: bool = False   # keep at top-level for TOML compat
    quality_gates: bool = True
    saga_enabled: bool = True
    defer_expensive: bool = True
    save_max_content_bytes: int = 1_000_000
    save_max_tags: int = 50
    save_max_category_len: int = 100
    save_max_slug_len: int = 200

@dataclass(frozen=True)
class EmbeddingConfig:
    backend: str = "auto"
    model_id: str = "Potion-8M"
    model_revision: str = ""
    idle_unload_seconds: int = 600

@dataclass(frozen=True)
class AutoSaveConfig:
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 300.0
    circuit_breaker_seconds: float = 300.0
    failure_window_seconds: float = 60.0
    batch_interval_seconds: float = 5.0
    batch_size: int = 50
    daemon_idle_seconds: int = 300
    inbox_max_bytes: int = 500_000
    preview_max: int = 200
    params_max: int = 2000
    health_check_minutes: int = 15
    allowlist: str = ""
    denylist: str = ""
    keyword_routing: bool = False
    always_sessions: bool = False

@dataclass(frozen=True)
class HealthCheckConfig:
    vec_index_drift_threshold: int = 50
    disk_pct_used_threshold: int = 95

@dataclass(frozen=True)
class SyncConfig:
    enable_server: bool = False
    listen_host: str = "127.0.0.1"
    listen_port: int = 9877
    peers: str = ""
    interval_minutes: int = 5

@dataclass(frozen=True)
class APIConfig:
    enable_server: bool = False
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    api_token: str = ""
    insecure_loopback: bool = False
    dashboard_address: str = "127.0.0.1:8080"

@dataclass(frozen=True)
class QualityGatesConfig:
    min_content_length: int = 20
    max_duplicate_similarity: float = 0.90
    min_relevance_score: float = 0.30

@dataclass(frozen=True)
class MemorySharingConfig:
    shared_pool_ttl_days: int = 30
    shared_pool_max_size: int = 1000

@dataclass(frozen=True)
class CacheConfig:
    fts5_cache: bool = True
    fts5_cache_ttl: int = 30
    vec_cache_max: int = 500
    vec_cache_ttl_s: float = 300.0

@dataclass(frozen=True)
class LLMConfig:
    provider: str = "none"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:0.5b"
    ollama_timeout_s: int = 30
    llama_cpp_host: str = "http://localhost:8080"
    llama_cpp_model: str = ""
    llama_cpp_timeout_s: int = 30
    extraction_model_id: str = "Qwen/Qwen3-0.6B"
    extraction_max_tokens: int = 256
    extraction_hybrid_threshold: float = 0.5
    extraction_force: bool = False

@dataclass(frozen=True)
class HybridSearchConfig:
    fts_weight: float = 0.5
    semantic_weight: float = 0.3
    rrf_k: int = 60
    semantic_overfetch: int = 50
    rank_proxy_scale: float = 100.0

@dataclass(frozen=True)
class RerankConfig:
    half_life_days: float = 7.0
    cross_encoder_blend: float = 0.7
    late_interaction_blend: float = 0.3
    topic_similarity_threshold: float = 0.75
    concept_drift_threshold: float = 0.3
    temporal_decay_weight: float = 0.15

@dataclass(frozen=True)
class FeatureFlagsConfig:
    multi_agent: bool = True
    summarization: bool = True
    user_profile: bool = True
    self_directed: bool = True
    adaptive_retention: bool = True
    neural_forget_mode: str = "formula"
    neural_forget_weights: str = ""
    temporal_ssm_enabled: bool = False
    temporal_ssm_weights: str = ""
    consolidation: bool = True
    temporal_tiers: bool = True
    crdt_enabled: bool = True
    legacy_note_crdt: bool = False
    llm_extraction: bool = True
    feature_temporal_kg: bool = True
    feature_temporal_kg_llm: bool = True
    temporal_kg_llm_tier: str = "light"
    feature_belief_layer: bool = True
    self_editing: bool = True
    knowledge_compilation: bool = True
    graph_centrality_boost: bool = True
    graph_communities: bool = True
    graph_evolution_tracking: bool = True
    session_memory: bool = False
    session_decision_llm: bool = False

@dataclass(frozen=True)
class UserProfileConfig:
    ctr_data_window_days: int = 90
    exploration_mode: str = "off"
    window_days: int = 30
    max_size: int = 1000
    recency_half_life_days: int = 14

@dataclass(frozen=True)
class RecallConfig:
    max_tokens: int = 4000
    tier1_hot_days: int = 7
    tier_fallback_threshold: float = 0.5

@dataclass(frozen=True)
class SemanticKGConfig:
    max_claims_semantic: int = 100
    semantic_threshold: float = 0.7
    kg_dedup_threshold: float = 0.85

@dataclass(frozen=True)
class RateLimitsConfig:
    requests_per_minute: int = 60
    burst: int = 10
```

**Step A2 — Refactor `MemoryConfig` to use nested configs**

```python
@dataclass(frozen=True)
class MemoryConfig:
    """Immutable, validated configuration — logically grouped into sub-configs.
    
    All TOML keys remain unchanged. ``[search] temporal_half_life`` still
    maps to ``cfg.search.temporal_half_life``. The flat dataclass is
    fully replaced; callers must be updated to use nested access.
    """

    general: GeneralDBConfig = field(default_factory=GeneralDBConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    kg: KGConfig = field(default_factory=KGConfig)
    graph_cache: GraphCacheConfig = field(default_factory=GraphCacheConfig)
    write: WritePipelineConfig = field(default_factory=WritePipelineConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    auto_save: AutoSaveConfig = field(default_factory=AutoSaveConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    api: APIConfig = field(default_factory=APIConfig)
    quality_gates: QualityGatesConfig = field(default_factory=QualityGatesConfig)
    sharing: MemorySharingConfig = field(default_factory=MemorySharingConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    hybrid: HybridSearchConfig = field(default_factory=HybridSearchConfig)
    rerank: RerankConfig = field(default_factory=RerankConfig)
    features: FeatureFlagsConfig = field(default_factory=FeatureFlagsConfig)
    user_profile: UserProfileConfig = field(default_factory=UserProfileConfig)
    recall: RecallConfig = field(default_factory=RecallConfig)
    semantic_kg: SemanticKGConfig = field(default_factory=SemanticKGConfig)
    rate_limits: RateLimitsConfig = field(default_factory=RateLimitsConfig)
    health_check: HealthCheckConfig = field(default_factory=HealthCheckConfig)
```

**Step A3 — Backwards-compat accessor shim**

Add to `MemoryConfig`:

```python
def __getattr__(self, name: str) -> Any:
    """Allow legacy flat access: cfg.temporal_half_life → cfg.search.temporal_half_life.
    
    Searches all sub-configs for the field. Raises AttributeError if not found.
    Performance: <1µs per call; called at config load time, not hot path.
    """
    for sub in (
        self.general, self.search, self.kg, self.graph_cache, self.write,
        self.embedding, self.auto_save, self.sync, self.api, self.quality_gates,
        self.sharing, self.cache, self.llm, self.hybrid, self.rerank,
        self.features, self.user_profile, self.recall, self.semantic_kg,
        self.rate_limits, self.health_check,
    ):
        if hasattr(sub, name):
            return getattr(sub, name)
    raise AttributeError(f"MemoryConfig has no attribute '{name}'")
```

This preserves all existing callers (`cfg.temporal_half_life`, `cfg.write_journal`, etc.) without modification. The shim is a migration aid and can be removed after one release cycle.

**Step A4 — TOML loader update**

The existing `_parse_toml()` in `config.py` already reads `[section]` blocks. Update it to group keys into the correct nested config:

The TOML format stays identical:
```toml
[general]
db_path = "memory/memory.db"
wal_checkpoint_startup = true

[search]
temporal_half_life = 180.0
knowledge_graph = true

[features]
write_journal = false
multi_agent = true
```

The loader maps `[general]` keys into `GeneralDBConfig(...)`, `[search]` into `SearchConfig(...)`, etc. No TOML schema change.

**Step A5 — Wire health-check thresholds into `mcp_maintenance.py`**

File: `mcp_maintenance.py` (lines 211–216)

Replace hardcoded thresholds with config reads:

```python
# Before (lines 211-216):
degraded = bool(
    status["db"].get("accessible") is False
    or int(status["vec_index"].get("drift", 0)) > 50
    or int(status.get("disk", {}).get("pct_used", 0)) > 95
)

# After:
from infra.config import get_config
_cfg = get_config()
_degraded_drift  = _cfg.health_check.vec_index_drift_threshold   # backwards-compat via __getattr__
_degraded_disk   = _cfg.health_check.disk_pct_used_threshold

degraded = bool(
    status["db"].get("accessible") is False
    or int(status["vec_index"].get("drift", 0)) > _degraded_drift
    or int(status.get("disk", {}).get("pct_used", 0)) > _degraded_disk
)
```

Also update `cron/cron_health_check.py` to use the same config values so the cron and MCP tool stay consistent.

**TOML config (new keys, additive — no existing keys change):**

```toml
[health_check]
vec_index_drift_threshold = 50    # warn when vec_keys lag memories by this many
disk_pct_used_threshold = 95      # warn when disk usage exceeds this %
```

Add tests:
- `test_health_check_config_defaults`: assert defaults are 50 and 95
- `test_health_check_dynamic_thresholds`: set config to custom values, run `memory_health_check`, assert degraded flag fires at the custom threshold

**Step A6 — Verification**

- `mypy infra/config.py` → 0 errors
- `ruff check infra/config.py` → 0 errors  
- `python -m pytest eval/test_config_loading.py -q` → 0 failures
- Add new test: `test_nested_config_backwards_compat` that accesses legacy flat names and asserts they resolve correctly
- Add new test: `test_nested_config_direct_access` that accesses `cfg.search.temporal_half_life` etc.

---

## Stream B — Code Hygiene

### B-1: Dead Code Removal

**B-1a — `mcp_sdk.py` dead string literal (lines 5–16)**

File: `mcp_sdk.py`

Remove lines 5–16 (the `"""MCP tool: memory_sdk_demo..."""` string literal). It is not a module docstring (the first statement in the file is already `from __future__ import annotations` on line 1, then `import logging`, then `logger = ...` on line 4). Python evaluates it as a no-op expression and discards the result.

```python
# Remove (lines 5-16):
"""
MCP tool: memory_sdk_demo. ...
"""
```

**B-1b — `migration_runner.py` unreachable elif branch (lines 515–538)**

File: `infra/migration_runner.py`

Replace the dead branch with a comment explaining why it's unreachable, so future maintainers don't re-introduce the bug:

```python
# NOTE: the "no such column" guard below is UNREACHABLE in practice —
# SQLite's "duplicate column" and "no such column" errors are mutually
# exclusive for the same statement. The first branch (line ~490) already
# catches "duplicate column" via the "duplicate column name" substring
# match. This elif is kept as a no-op guard to document the original
# intent (idempotent ADD COLUMN) without silently swallowing real errors.
elif False:  # pragma: no cover — unreachable by design
    ...
```

### B-2: Docstring Sprint — `search/orchestrator.py`

**B-2a — Module docstring**

Insert at the top of `search/orchestrator.py` (after `from __future__`):

```python
"""12-phase hybrid search orchestrator for agentic-memory.

Pipeline phases (executed in order):
  Phase 0  — Input normalization & query type detection
  Phase 1  — FTS5 BM25 retrieval
  Phase 2  — Vector (usearch) retrieval
  Phase 3  — ColBERT late-interaction retrieval
  Phase 4  — Reciprocal Rank Fusion (RRF) merge
  Phase 5  — Cross-encoder reranking (optional)
  Phase 6  — Temporal decay application
  Phase 7  — Neural forget curve adjustment
  Phase 8  — KG concept/centrality boost
  Phase 9  — Final score computation & ranking
  Phase 10 — Result envelope construction
  Phase 11 — Error counter & latency logging

Error handling: each phase is individually isolated. On failure, the
phase increments its error counter (via ``infra.error_counter``) and
the pipeline falls through to the next phase with degraded results.
No single phase failure kills the search.

Thread safety: uses module-level ``_db_columns_cache`` (RLock) and
``_phase_latencies`` (RLock) for cross-call shared state.
"""
```

**B-2b — Function docstrings (top 10 public functions)**

These functions are the public surface of the orchestrator:

1. `search_memories()` — main entry point
2. `_parse_search_query()` — query normalization
3. `_get_memories_columns()` — PRAGMA cache
4. `_reciprocal_rank_fusion()` — RRF merge
5. `_apply_temporal_decay()` — temporal scoring
6. `_apply_neural_forget_curve()` — forget curve
7. `_apply_concept_boost()` — KG boost
8. `_apply_centrality_boost()` — graph centrality
9. `_compute_final_score()` — weighted final score
10. `_build_search_result_envelope()` — result serialization

Each needs a one-line summary + parameter/return types. Format: Google-style.

---

## Stream C — Production Readiness

### C-1: `docs/production_readiness.md`

Write a comprehensive document covering:

**Section 1 — Deployment Topology**
```
Recommended layout:
  memory/
    memory.db          ← main SQLite (WAL mode)
    journal.db         ← CQRS journal (separate disk recommended)
    locks/             ← flock files
    .health_status.json ← cron output
  cron/                 ← install_crontab.sh output
  config/
    memory.toml         ← operator-edited config
```

**Section 2 — Pre-Flight Checklist** (the 7 items from your request)
Each item includes:
- How to verify (command)
- What "good" looks like
- How to fix if broken

```markdown
### 2.1 Separate disks for memory.db and journal.db
Verify:
  df -h memory/memory.db memory/journal.db
Good:
  Both files on different mount points, or journal.db on tmpfs/ramdisk.
Fix:
  Set MEMORY_JOURNAL_DB_PATH=/mnt/fast-disk/journal.db
```

Repeat for all 7 items.

**Section 3 — Operational Runbooks**

| Failure | Symptoms | Recovery |
|---------|----------|----------|
| WAL corruption | `SQLITE_CORRUPT` errors | `sqlite3 memory.db ".recover"` → new DB |
| Journal drift | `memory_health_check` shows `pending > 1000` | `memory_maintenance(operation="reset_stuck_processing")` |
| Circuit breaker open | No auto-saves for >5 min | Check `.auto_save_circuit_sentinel`, fix root cause |
| Vec index drift | `health_check` shows drift > 50 | `python rebuild_vec_index.py` |
| Disk full | `health_check` pct_used > 95 | Cron backup rotation, purge expired |
| Zombie workers | `ps aux | grep background_worker` shows >1 | Kill extras; check cron cadence |

**Section 4 — Backup & Recovery**
- `cron_backup.py` runs daily
- Restore procedure: stop daemon → restore DB → start daemon
- OKF export as portable backup format

**Section 5 — Scaling Limits**
- Single-writer SQLite: safe up to ~100 writes/sec
- CQRS journal removes the write serialization bottleneck
- Connection pool default: 24 connections
- Max recommended agents per DB: 10 concurrent writers

**Section 6 — Security Posture**
- All external input treated as hostile (prompt injection guard)
- Credentials: never logged, never returned in MCP responses
- Sync server: loopback-only by default, TLS optional
- `.auto_save_circuit_sentinel` is a file presence check (no secrets in it)

### C-2: CI Validation Test

File: `eval/test_production_readiness.py`

```python
class TestProductionReadiness:
    """Automated checks that mirror the production readiness checklist.
    
    These tests verify the system is in a deployable state:
    WAL mode, busy_timeout, cron installed, circuit breaker file,
    health check returns 200, vec index consistent.
    """
    
    def test_wal_mode_active(self, tmp_db):
        """PR §2.2: WAL mode is active."""
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal", f"Expected WAL mode, got {mode}"
    
    def test_busy_timeout_set(self, tmp_db):
        """PR §2.3: busy_timeout >= 30000ms."""
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout >= 30000, f"busy_timeout={timeout}ms, need >= 30000"
    
    def test_health_check_returns_healthy(self, tmp_db):
        """PR §2.7: memory_health_check reports healthy on clean DB."""
        ...
    
    def test_vec_index_not_drifting(self, tmp_db):
        """PR §2.6: vec_keys count matches memories count."""
        ...
    
    def test_save_pipeline_returns_under_200ms(self, tmp_db):
        """PR §2.8: synchronous save path completes in <200ms."""
        ...
```

---

## Stream D — Polish & Story

### D-1: TS Plugin Harness Adapter

**Current state:** `plugin/agentic-memory-hooks.ts` already exports pure functions. `plugin/index.ts` is a 63-line adapter that maps OpenCode event names to those functions.

**Problem:** The pure functions in `agentic-memory-hooks.ts` still import from the OpenCode plugin module via `index.ts`. The file comment says "It knows NOTHING about OpenCode's plugin system" — this is aspirational, not actual.

**D-1a — Define harness interface type**

Create `plugin/types.ts`:

```typescript
/** Interface that any harness adapter must implement. */
export interface HarnessAdapter {
  readonly log: (msg: string) => void
  readonly eventName: (event: string) => string   // maps "session.created" → harness name
  readonly injectIntoSystemPrompt: (lines: string[]) => void
  readonly getState: () => { sessionContext: string; proactiveContext: string }
  readonly spawn: (args: string[], label: string) => Promise<string>
  readonly fireAndForget: (args: string[], label: string) => void
}

/** Context passed to each hook function. */
export interface HookContext {
  readonly adapter: HarnessAdapter
  readonly toolName?: string
  readonly toolArgs?: Record<string, unknown>
  readonly sessionId?: string
  readonly output?: unknown
}
```

**D-1b — Update `agentic-memory-hooks.ts` to accept `HookContext`**

Change all exported function signatures:

```typescript
// Before:
export async function startSession(log: (msg: string) => void): Promise<void>

// After:
export async function startSession(ctx: HookContext): Promise<void>
```

All internal `log(...)` calls become `ctx.adapter.log(...)`. All `spawn` calls become `ctx.adapter.spawn(...)`.

**D-1c — `index.ts` becomes a 30-line OpenCode implementation**

```typescript
import type { HarnessAdapter, HookContext } from "./types"

function makeOpenCodeAdapter(input: PluginInput): HarnessAdapter {
  const log = (msg: string) => input.client.app.log({ 
    level: "info" as const, 
    name: "agentic-memory", 
    data: { message: msg } 
  }).catch(() => {})

  return {
    log,
    eventName: (e: string) => e,   // OpenCode uses native event names
    injectIntoSystemPrompt: (lines: string[]) => {
      input.session.history.systemPrompt.value.push(...lines)
    },
    getState: () => ({ ...state }),
    spawn: async (args, label) => {
      return await captureOutput(args, undefined, label, log)
    },
    fireAndForget: (args, label) => {
      fireAndForget(args, label, log)
    },
  }
}

const HOOKS: Record<string, (ctx: HookContext) => void | Promise<void>> = {
  "tool.execute.after":      (ctx) => onToolAfter(ctx.toolName!, ctx.toolArgs!, ctx.output!, ctx.adapter.log),
  "tool.execute.before":     (ctx) => beforeTool(ctx.toolName!, ctx.toolArgs!, ctx.adapter.log),
  "session.created":         ()    => startSession({ ...ctx, toolName: undefined }),
  "session.idle":            ()    => onIdle(ctx.adapter.log),
  "session.deleted":         (ctx) => endSession(ctx.sessionId!, ctx.adapter.log),
  "experimental.chat.system.transform": (sys) => injectSystemPrompt(sys),
  "experimental.session.compacting":     (ctx) => onCompacting(ctx.sessionId!, ctx.output!, ctx.adapter.log),
}

export default async function AgenticMemoryPlugin(input: PluginInput) {
  const adapter = makeOpenCodeAdapter(input)
  const ctx: HookContext = { adapter }

  return Object.fromEntries(
    Object.entries(HOOKS).map(([event, handler]) => [
      event,
      (...args: unknown[]) => handler({ ...ctx, ...extractArgs(event, args) }),
    ])
  )
}
```

### D-2: SDK Versioning + README + OKF Guide

**D-2a — Add version + changelog to `sdk.py`**

```python
# Top of sdk.py, after module docstring:
__version__ = "1.0.0"
__all__ = ["Memory", "AgentMemory"]

# Changelog in docstring:
"""
Changelog:
  1.0.0 (2026-07-08) — Stable API. Nested config refactor complete.
                        AgentMemory.search gains include_global parameter.
  0.9.0 (2026-06-15) — Bump potion-8M embedding, add AgentMemory class.
  0.8.0 (2026-05-20) — Initial public SDK.
"""
```

**D-2b — Bare-Python README section**

Add to `README.md`:

```markdown
## Quick start — bare Python

No MCP, no OpenCode, no TS plugin required.

```python
from agentic_memory import Memory

m = Memory(db_path="memory.db")  # defaults to memory/memory.db

# Save
note_id = m.add("User prefers dark mode", tags=["preferences"])
print(note_id)  # → preferences/user-prefers-dark-mode

# Search
results = m.search("user preferences")
for r in results:
    print(f"{r['note_id']}: {r['content'][:80]}")

# Agent-scoped
from agentic_memory import AgentMemory
am = AgentMemory(agent_id="coder-1")
am.save("Frontend uses React 18", tags=["frontend", "react"])
```

### D-2c — OKF / Obsidian migration guide**

File: `docs/okf-obsidian-migration.md`

```markdown
# Exporting agentic-memory to Obsidian / Logseq

Your agent's memory lives in plain-text Markdown files. You already
own your data — this guide shows you how to make other tools aware of it.

## Step 1: Export via OKF

```bash
cd ~/.config/agentic-memory
venv/bin/python okf_export.py \
  --db-path memory/memory.db \
  --output-dir ~/ObsidianVault/agent-memory
```

This produces:
  ~/ObsidianVault/agent-memory/
    index.md          ← table of contents
    lessons/          ← one .md per lesson note
    decisions/        ← one .md per decision note
    projects/         ← one .md per project note
    preferences/      ← one .md per preference note
    sessions/         ← one .md per session summary
    ... (one folder per category)
```

Each .md file has YAML frontmatter (tags, importance, created, superseded_by, etc.)
plus the full note body in Markdown. OpenObsidian/L
```

### D-3: CrewAI Crew<=0.102 → ~1.0

**Current state:** `docs/integrations/crewai.md` explicitly says `crewai<1.0`. The integration is `agentic_memory/integrations/crewai/memory.py`.

**Action:** Read current CrewAI memory protocol, update to latest API, fix tests.

---

## Execution Plan — Parallel Dispatch

| Stream | Files Changed | Branch? | Sub-Agent? |
|--------|--------------|---------|-----------|
| A — Config | `infra/config.py`, `eval/test_config_loading.py` | Yes — `feat/config-nested-refactor` | Explore agent first → then implement |
| B — Dead code | `mcp_sdk.py`, `infra/migration_runner.py`, `search/orchestrator.py` | Yes — `feat/code-hygiene-docstrings` | Yes |
| C — Prod readiness | `docs/production_readiness.md`, `eval/test_production_readiness.py`, `cron/install_crontab.sh` | Yes — `feat/production-readiness` | Yes |
| D-1 — TS adapter | `plugin/types.ts`, `plugin/agentic-memory-hooks.ts`, `plugin/index.ts` | Yes — `feat/harness-adapter` | Yes |
| D-2 — SDK + docs | `sdk.py`, `README.md`, `docs/okf-obsidian-migration.md` | Yes — `feat/sdk-versioning-okf-guide` | Yes |
| D-3 — CrewAI | `agentic_memory/integrations/crewai/memory.py`, `eval/test_crewai_memory.py`, `docs/integrations/crewai.md` | Yes — `feat/crewai-v1-upgrade` | Yes (explore first) |

**Merge order:** B first (isolated, lowest risk) → A (config, all tests updated) → C (docs + CI tests) → D-1/D-2/D-3 in any order (independent).

---

## Verification Matrix

After each stream completes:

| Check | Command | Expected |
|-------|---------|----------|
| mypy | `venv/bin/python -m mypy <modified files>` | 0 errors |
| ruff | `venv/bin/python -m ruff check <modified files>` | 0 errors |
| Targeted tests | `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/python -m pytest eval/test_<relevant>.py -q` | 0 failures |
| Full suite | Only after all streams merged to main | 0 failures |

No mypy exception. No ruff exception. No `# type: ignore`. No `# noqa`.

---

## Anti-Patterns to Avoid

1. **No surface changes.** Replacing 130 flat fields with nested dataclasses must not change TOML key names, default values, or runtime behavior. The `__getattr__` shim exists as a migration bridge, not a permanent feature.
2. **No dead-string docstrings.** The `mcp_sdk.py` dead string literal on lines 5-16 is not a docstring — it's a bare expression evaluated and discarded. Removing it is not removing documentation.
3. **No harness coupling in hooks.** `agentic-memory-hooks.ts` already exports pure functions. The task is to formalize the interface it already implicitly follows — not to rewrite the hooks.
4. **No TOML schema changes for config refactor.** `[general]`, `[search]`, `[features]` sections remain. Only the Python dataclass shape changes.
5. **No CrewAI API guessing.** Before writing any integration code, read the installed `crewai` package to find the actual `Memory` protocol in the current version.
