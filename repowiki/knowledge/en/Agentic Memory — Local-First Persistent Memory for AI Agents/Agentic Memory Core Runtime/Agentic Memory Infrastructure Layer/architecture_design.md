Children are organised as independent sub-packages (database_persistence, search_embedding, audit_logging, auth_rbac, cache_locking, metrics_monitoring, shared_memory_state, services_api, sync_distributed, utilities) plus three top-level bootstrappers:
- `_bootstrap_path.py` inserts the install root into `sys.path` so project modules (`memory_*`, `config`, `save_pipeline`, etc.) are importable before any other infra code runs.
- `_shim.py` + per-file shims re-export relocated packages for backward compatibility via a custom `types.ModuleType` proxy.
- `_lazy_imports.py` is the single registry of deferred imports used to break cycles between `memory_common → config → memory_common` and similar pairs; callers import through `infra._lazy_imports` instead of direct module paths.

Cross-child wiring points:
- `infra.infrastructure` is the common decorator layer (`@with_audit`, `@with_memory_connection`) consumed by every MCP tool in `services_api`; it resolves the active DB path via `memory_common.get_memory_paths()` and routes audit events through `audit.audit(...)` which fans out to sinks defined in `audit_sink_file.py` / `audit_sink_http.py` / `audit_sink_prom.py`.
- `config_drift` drives policy enforcement using the execution scope resolved by `shared_memory_state`; its escape hatches are gated by the same lock primitives from `cache_locking`.
- `sync_distributed` peers talk over HTTP endpoints served by `services_api`'s threaded server and share state through the fixed-layout segment in `shared_memory_state`.
- `search_embedding` backends (usearch/Numpy/SPLADE/ColBERT) are selected at runtime via the lazy-import registry so heavy model packages are never loaded unless needed.
- `metrics_monitoring` exposes a Prometheus exporter alongside the main server in `services_api`, while `alert.py` delivers notifications on error thresholds observed across children.