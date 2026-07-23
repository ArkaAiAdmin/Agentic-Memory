---
kind: logging_system
name: Centralized Python logging with text/JSON modes and pluggable audit sinks
category: logging_system
scope:
    - '**'
source_files:
    - infra/memory_config.py
    - infra/log.py
    - infra/audit_sink.py
    - infra/audit_sink_file.py
    - infra/audit_sink_http.py
    - infra/audit_sink_prom.py
    - docker-compose.yml
---

## What system/approach is used
Agentic Memory uses the standard-library `logging` module as its sole logging framework. There is no third-party logger (no structlog, loguru, or similar). The application configures a single root logger once at process start and obtains child loggers via `logging.getLogger(__name__)`. Two output modes are supported:
- **Text mode** (default): `basicConfig` with `%(asctime)s [%(name)s] %(levelname)s: %(message)s`.
- **Structured JSON mode**: a custom `_JsonFormatter` emitting `{ts, level, logger, msg, exception?}` when `LOG_FORMAT=json`.

A separate, non-blocking **audit sink subsystem** (`infra/audit_sink.py`) fans out structured audit events to pluggable sinks (file JSONL, Prometheus counters, HTTP HEC) on a background thread with a bounded queue and SOC2-compliant dead-letter file.

## Key files and packages
- `infra/memory_config.py::configure_logging()` — idempotent root logger bootstrap; reads `LOG_LEVEL`, `LOG_FORMAT`; validates config.
- `infra/log.py::setup_logging(name, ...)` — convenience wrapper that calls `configure_logging()` only if no handlers exist, then returns a named logger; allows per-call overrides of level/format.
- `infra/audit_sink.py` — `AuditSink` Protocol + fan-out dispatcher + PII redaction + dead-letter writer.
- `infra/audit_sink_file.py` — rolling JSONL file sink (50 MB rotation, N backups).
- `infra/audit_sink_http.py` — HTTP sink for Splunk HEC / Elasticsearch / Datadog intake (loaded lazily from `memory.toml`).
- `infra/audit_sink_prom.py` — Prometheus counter sink.
- `docker-compose.yml` — sets `LOG_LEVEL: INFO` for container runs.
- `cron/*.py` — every cron job imports `from infra.log import setup_logging` and calls it at entry.
- `background/*.py`, `agentic_memory/*.py`, `knowledge_graph/*.py`, `kg/*.py`, `fact/*.py` — all use `logger = logging.getLogger(__name__)` after importing `logging`.

## Architecture and conventions
1. **Single bootstrap point.** `configure_logging()` is guarded by `if not logging.getLogger().handlers: return`, so it is safe to call from any import path without clobbering test-configured handlers. It is invoked automatically by `setup_logging()` when the first handler is missing.
2. **Environment-driven configuration.**
   - `LOG_LEVEL` — one of `DEBUG|INFO|WARNING|ERROR|CRITICAL`; invalid values are corrected to `INFO` and warned.
   - `LOG_FORMAT` — `text` (default) or `json`.
3. **Per-module loggers.** Every module declares `logger = logging.getLogger(__name__)` at top-of-file and logs through that instance rather than calling `logging.info(...)` directly.
4. **Cron/script entrypoints** go through `infra.log.setup_logging(name)` which centralizes the format/level override logic and ensures `configure_logging()` has run.
5. **Audit vs. operational logs.** Operational logs go to the root logger (stdout/stderr). Audit events are emitted separately via `dispatch_to_sinks(event)` in `infra/audit_sink.py`, which writes to local JSONL files, Prometheus metrics, and optionally an HTTP endpoint — never blocking the hot path. Failed dispatches are persisted to `memory/audit_sink_dead_letter.jsonl`.
6. **PII redaction.** All audit payloads pass through `redact_event()` / `redact_audit_value()`, masking keys matching token/password patterns and high-entropy string values, before reaching any sink.
7. **No external log aggregation library.** Routing to SIEMs is done via the HTTP audit sink, not via a logging driver.

## Rules developers should follow
- **Never call `logging.basicConfig` directly.** Use `infra.log.setup_logging(name)` or rely on `configure_logging()` being called once at process start.
- **Always obtain loggers via `logging.getLogger(__name__)`** at module scope; do not create ad-hoc loggers inside functions.
- **Control verbosity with environment variables**, not code changes: set `LOG_LEVEL` and `LOG_FORMAT`.
- **For audit-worthy operations** (tool invocations, auth decisions, config drift), emit structured events via `dispatch_to_sinks({...})` instead of plain `logger.info(...)`, so they reach the sink pipeline and dead-letter file.
- **Do not raise from sink `emit`/`flush` implementations.** Sinks must be fire-and-forget; errors are caught and recorded in the dead-letter file.
- **When adding a new sink**, implement the `AuditSink` Protocol (`emit`, `flush`) and register it in `_build_sinks()`.