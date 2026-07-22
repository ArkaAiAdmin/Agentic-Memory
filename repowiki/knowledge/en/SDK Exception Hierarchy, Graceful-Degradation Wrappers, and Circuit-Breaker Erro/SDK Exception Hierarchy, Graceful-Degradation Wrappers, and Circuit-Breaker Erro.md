---
kind: error_handling
name: SDK Exception Hierarchy, Graceful-Degradation Wrappers, and Circuit-Breaker Error Isolation
category: error_handling
scope:
    - '**'
source_files:
    - agentic_memory/exceptions.py
    - infra/safe_call.py
    - infra/error_counter.py
    - background/circuit_breaker.py
    - infra/api_server.py
    - agentic_memory/client.py
    - agentic_memory/maintenance.py
---

Agentic Memory uses a layered error-handling strategy that separates user-facing SDK errors from internal operational failures, and wraps fragile subsystems with graceful-degradation helpers so the rest of the system stays responsive.

1. What system/approach is used
- A typed Python exception hierarchy under `agentic_memory/exceptions.py` provides domain exceptions (`AgenticMemoryError`, `ValidationError`, `NotFoundError`, `AgenticConnectionError`, `AgenticIntegrityError`, `MaintenanceError`, `SyncError`, `AgenticPermissionError`, `CircuitBreakerOpen`, `ConfigError`) plus backward-compat aliases that shadow builtins (`ConnectionError`, `IntegrityError`, `PermissionError`).
- Internal infrastructure exposes reusable wrappers for best-effort calls: `infra/safe_call.safe_call(func, fallback, log_level, err_label, raise_on)` returns a sentinel on any caught exception (with optional re-raise for specific types), centralizing the try/except + warning pattern that previously lived in 30+ call sites.
- Per-phase error counting via `infra/error_counter.ErrorCounter` tracks swallowed exceptions per subsystem phase, exposing recent entries and phase counts for observability.
- The auto-save subsystem owns an explicit circuit breaker (`background/circuit_breaker.py`) that records failures, applies exponential backoff, opens a circuit to skip saves for a configured window, persists state to the audit log, and writes a cross-process sentinel file so TypeScript plugins can also detect the open state. Flock/database-lock contention is explicitly filtered out so transient lock waits do not trip the breaker.

2. Key files and packages
- `agentic_memory/exceptions.py` — public SDK exception classes and builtin-shim aliases
- `infra/safe_call.py` — graceful-degradation wrapper used across MCP handlers and background code
- `infra/error_counter.py` — thread-safe in-process error counter with recent-entry buffer
- `background/circuit_breaker.py` — auto-save circuit breaker, backoff, sentinel file, audit-persistence, shared-memory mirroring
- `infra/api_server.py` — HTTP layer converts unhandled exceptions into JSON `{"error": ...}` responses via `_error(message, status_code)`; request validation raises `ValueError` which the server maps to 4xx
- `agentic_memory/client.py` — validates inputs and raises `ValidationError`; catches JSON parse errors and re-raises as `ValueError`
- `agentic_memory/maintenance.py` — raises `MaintenanceError` for failed maintenance operations (heartbeat, tier migration, contradiction detection)

3. Architecture and conventions
- Public API surface: callers catch `AgenticMemoryError` or its subclasses; internal infra code raises these instead of bare `Exception`.
- Graceful degradation: non-critical paths wrap calls with `safe_call(..., fallback=...)` so one failing feature does not crash the handler; only exceptional cases where failure must propagate use plain try/except.
- Observability-first swallowing: when exceptions are intentionally caught (e.g., optional integrations, cron jobs), they are logged at WARNING/INFO and optionally recorded through `infra.error_counter.increment(phase, error)` so silent failures remain visible.
- Operational resilience: the auto-save circuit breaker isolates cascading failures by temporarily disabling saves after a configurable failure window, persisting state to the audit log and a sentinel file, and ignoring flock contention as non-fatal.
- HTTP boundary: `APIRequestHandler._error` serializes errors as JSON with appropriate status codes; authentication/authorization failures return 401/403 rather than raising up the stack.

4. Rules developers should follow
- Prefer raising a specific subclass of `AgenticMemoryError` from SDK-facing code; avoid bare `Exception` / `ValueError` leaks to clients.
- Use `infra.safe_call.safe_call` for optional/failure-tolerant work (hooks, external LLM calls, index rebuilds); pass `err_label` so logs are attributable.
- For critical failures that must abort the caller, use a plain try/except so type checkers can follow control flow; do not swallow them inside `safe_call`.
- When catching exceptions in long-running workers or cron jobs, log at WARNING and consider calling `infra.error_counter.increment(phase, error)` to keep error rates visible.
- Treat flock/database-lock contention as transient — do not let it count toward circuit-breaker thresholds (the breaker already filters these).
- At the HTTP boundary, convert domain errors to JSON via `_error` with the correct status code; never let raw Python tracebacks escape to the wire.