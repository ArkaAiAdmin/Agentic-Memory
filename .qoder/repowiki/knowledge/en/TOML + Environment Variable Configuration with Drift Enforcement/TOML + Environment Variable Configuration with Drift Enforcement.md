---
kind: configuration_system
name: TOML + Environment Variable Configuration with Drift Enforcement
category: configuration_system
scope:
    - '**'
source_files:
    - infra/config.py
    - memory.toml
    - infra/memory_config.py
    - infra/toml_watch.py
    - infra/config_drift.py
    - infra/config_drift_policy.py
    - infra/config_drift_audit.py
    - docs/env_vars.md
---

## What system/approach is used

Agentic Memory uses a single-source-of-truth TOML file (memory.toml) layered with MEMORY_* environment variables for runtime overrides. The configuration loader lives in infra/config.py and exposes a frozen, nested MemoryConfig dataclass built from typed sub-configs (search, write, embedding, sync, api, features, etc.). A companion module infra/memory_config.py resolves process-scoped paths (install root, project root, memory directories). An optional hot-reload watcher (infra/toml_watch.py) polls the TOML mtime and can re-apply drift policy at runtime when MEMORY_TOML_HOT_RELOAD=1. A full config-drift detection subsystem (infra/config_drift.py, infra/config_drift_policy.py, infra/config_drift_tier_patch.py) decomposes every flag into default/TOML/env/effective sources, classifies each by severity tier (integrity/stability/compliance/operational/neutral), and enforces or audits violations based on scope.

## Key files and packages

- infra/config.py — core loader: _resolve() precedence (env > TOML > default), all typed dataclasses, singleton get_config(), feature-flag registry, integrity-critical env-var warnings
- memory.toml — canonical TOML with every section and documented MEMORY_* override comments
- infra/memory_config.py — install-root / project-root / memory-dir resolution, logging bootstrap, env validation
- infra/toml_watch.py — mtime poller, subscriber hook, hot-reload of drift tiers
- infra/config_drift.py — flag decomposition, severity tiers, report building, snapshot persistence, delta diffing
- infra/config_drift_policy.py — enforcement posture per tier (warn / soft_block / hard_fail), escape hatch, progressive escalation
- infra/config_drift_audit.py — JSONL audit sink for drift decisions
- Top-level shims config.py, memory_config.py — backward-compat re-exports that delegate to infra.*

## Architecture and conventions

Resolution order: For every field, MEMORY_* env var (parsed via type-aware _parse_bool/_parse_int/_parse_float) takes priority over TOML dotted path, which takes priority over dataclass default. Parse failures log a warning to stderr and fall back to default rather than raising.

MemoryConfig is composed of frozen sub-dataclasses (GeneralDBConfig, SearchConfig, WritePipelineConfig, EmbeddingConfig, AutoSaveConfig, SyncConfig, APIConfig, QualityGatesConfig, CacheConfig, LLMConfig, HybridSearchConfig, RerankConfig, FeatureFlagsConfig, UserProfileConfig, RecallConfig, SemanticKGConfig, HealthCheckConfig). Legacy flat attribute access (cfg.temporal_half_life) is preserved via __getattr__ forwarding to the appropriate nested config; new code should use nested access (cfg.search.temporal_half_life).

Boolean feature flags live under [features] and are also exposed as top-level booleans on MemoryConfig. get_feature_flags() returns a dict consumed by the dashboard and health checks. Disabling integrity-critical flags (MEMORY_SAGA_ENABLED, MEMORY_CRDT_ENABLED, MEMORY_WRITE_JOURNAL_ENABLED, MEMORY_QUALITY_GATES) emits an explicit security warning on stderr.

When MEMORY_TOML_HOT_RELOAD=1, a background thread polls memory.toml mtime. On change it re-applies [drift_tiers] overrides and resets the drift-policy cache so resolve_policy() picks up new enforcement postures without restart. All reloads are audited to memory/config_drift_audit.jsonl.

Each flag is classified into one of five severity tiers. Per-tier enforcement modes: integrity defaults to hard_fail (process exits if env diverges from TOML on critical flags); stability defaults to soft_block (blocks writes but allows reads); compliance/operational/neutral default to warn. Per-flag tier overrides come from [drift_tiers] in memory.toml. Progressive escalation promotes warn to soft_block to hard_fail after repeated drift hits within a rolling window. An escape-hatch env var provides time-bounded overrides.

Install root: MEMORY_INSTALL_ROOT or ~/.config/agentic-memory/. TOML path: MEMORY_CONFIG_PATH (relative resolved against install root) or <install_root>/memory.toml. DB path: MEMORY_DB_PATH resolved to absolute against install root (never cwd). Project root: traversed upward looking for markers (memory/, .git, .agents, AGENTS.md, CLAUDE.md, package.json, pyproject.toml).

## Rules developers should follow

1. Declare every new setting in three places: the TOML section in memory.toml, the corresponding dataclass field in infra/config.py, and the _b(...) call in _build_config_from_toml mapping MEMORY_* to dotted TOML key.
2. Use MEMORY_* env vars for deployment-time overrides; keep memory.toml as the source of truth checked into version control.
3. Classify new flags into a drift severity tier in _FLAG_TIERS (or via [drift_tiers]) so drift detection covers them.
4. Never read db_path directly — always go through resolve_db_path() so relative paths resolve against the install root, not cwd.
5. Access config via get_config() (singleton); do not construct MemoryConfig manually outside tests. Use reset_config() in test teardown.
6. Prefer nested access (cfg.search.temporal_half_life) over legacy flat access; flat access is deprecated and may be removed.
7. Do not disable integrity-critical flags (MEMORY_SAGA_ENABLED, MEMORY_CRDT_ENABLED, MEMORY_WRITE_JOURNAL_ENABLED, MEMORY_QUALITY_GATES) in production — doing so triggers an explicit security warning and may fail hard depending on drift policy.
8. For runtime-only settings (worker timeouts, queue sizes, CTR tuning) add a MEMORY_* entry in docs/env_vars.md alongside the code location that reads it.