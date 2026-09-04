# Security Residuals & Risk Acceptance Ledger (B1–B13)

This document formalizes the accepted security residuals, operational risk boundaries, and migration procedures for the `agentic-memory` Python kernel and its interaction with the `agentic-memory-ide` desktop harness.

---

## 1. Memory Kernel Residuals (B11–B13)

| ID | Domain | Severity | Residual Description & Rationale | Mitigations & Compensating Controls |
|---|---|---|---|---|
| **B11** | Auth / Token | Low | **API Token Strength Warn-Only Default:** For local developer convenience, short or weak API tokens log warnings on startup rather than aborting by default. | Production environments can enforce hard startup failure by setting `MEMORY_API_STRICT_TOKEN=1`. Timing attacks are mitigated across all REST, WebSocket, and sync handlers using UTF-8 byte-encoded constant-time comparison (`timing_safe_compare` via `hmac.compare_digest`) with non-ASCII error handling to prevent 500 crashes. |
| **B12** | Sync Plane | Low | **Loopback Sync Authentication Opt-Out:** Outbound multi-agent sync on default bind `127.0.0.1:9877` (as well as peer ports like OPENCODE on `9878` and AMI on `9880`) enforces Bearer token authentication by default across all interfaces. | Unauthenticated loopback sync can be explicitly enabled for legacy local configurations via `MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK=1`. Non-loopback interfaces always refuse unauthenticated startup regardless of flags. |
| **B13** | CQRS Journal | Low | **Write Journal Backlog Fail-Closed:** The CQRS write journal fails closed (`write_journal_fallback_sync = false`) by default when backlog capacity is exceeded, preserving crash consistency and strict queue ordering. | Optional synchronous fallback can be enabled in `memory.toml` (`write_journal_fallback_sync = true`) if data availability is preferred over strict queue order. |

---

## 2. Migration Guide & Operational Notes

### A. Multi-Agent Sync Daemons (A9)
- **Change:** As of the SEC-1 / A9 hardening, the sync server on `127.0.0.1:9877` (and peers on 9878/9880) enforces Bearer token authentication by default across all interfaces, including loopback.
- **Impact (Sync-Plane Specific):** Mutating requests to the sync server without valid authentication are rejected: `401 Unauthorized` when the `Authorization: Bearer <token>` header is missing or no token is configured on a non-loopback listener, and `403 Forbidden` when an invalid token is provided. (Note: The REST API plane at `infra/api_server.py` returns `401 Unauthorized` for both missing and invalid tokens).
- **Migration:**
  - *Recommended:* Configure `MEMORY_SYNC_TOKEN` or `MEMORY_API_TOKEN` in the environment of all sync clients and send `Authorization: Bearer <token>`.
  - *Opt-out (local dev only):* Set `MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK=1` (or `MEMORY_SYNC_TOKEN_REQUIRED=0`) on the sync listener to restore legacy unauthenticated loopback access. **Nuance:** This opt-out strictly applies to loopback interfaces (`127.0.0.1`, `::1`); remote/non-loopback interfaces always refuse unauthenticated startup and reject requests regardless of flags.

### B. REST API Rate Limit Restoration (A10)
- **Change:** A default rate limit of 600 requests per minute (~10 req/s) is now active per client IP/principal.
- **Impact:** High-throughput bulk ingestors or rapid automated test suites may encounter `429 Too Many Requests`.
- **Migration:**
  - *Bulk Ingestion:* Set `MEMORY_API_RATE_LIMIT=0` to completely disable rate limiting during bulk data loads.
  - *Tuning:* Set `MEMORY_API_RATE_LIMIT=<N>` to adjust the allowable requests per minute.

### C. CQRS Write Journal Backlog Exceptions (A10)
- **Change:** With `write_journal_fallback_sync = false` (the default), writes reject when the async queue is full rather than silently falling back to synchronous database locking.
- **Impact:** Callers will receive a structured error / `RuntimeError` if write volume exceeds queue drainage capacity:
  - Backlog threshold exceeded: `RuntimeError("write_journal backlog at {pending} pending entries (threshold={JOURNAL_PENDING_THRESHOLD}). Worker may be down — check background_worker and drain journal.db, or raise MEMORY_WRITE_JOURNAL_MAX_PENDING.")`
  - Journal file size exceeded: `RuntimeError("write_journal is full: size {current} bytes exceeds JOURNAL_MAX_SIZE_BYTES ({JOURNAL_MAX_SIZE_BYTES}). Refusing new enqueue. Drain pending entries via the reconciliation daemon, raise JOURNAL_MAX_SIZE_BYTES, or rebuild the journal DB.")`
- **Operational Handling:**
  - Client applications and dashboards should catch this condition, apply backpressure, and retry with exponential backoff.
  - If immediate persistence availability is preferred over queue ordering, enable synchronous fallback in `memory.toml`:
    ```toml
    [features]
    write_journal_fallback_sync = true
    ```

---

## 3. Desktop IDE Harness Residuals (B1–B10 Cross-Reference)

The following residuals govern native OS capability primitives in the desktop IDE harness (`agentic-memory-ide`). For full technical rationales and IPC test fixtures, refer to `docs/SECURITY_RESIDUAL.md` in the `agentic-memory-ide` repository.

- **B1 (Native Process):** Shell string execution accepted alongside argv for pipeline/redirection workflows; gated by mutating approval.
- **B2 (Native Process):** Agent CLI tool execution accepted by design; gated by explicit user confirmation and scoped YOLO mode.
- **B3 (Native FS):** Temp file permissions inherit OS user umask (`0644`/`0600`); mitigated by three-anchor sandboxing.
- **B4 (Process/PTY):** Process group signaling PID reuse risk bounded by checking `libc::getpgid(pid) > 0` before negative PGID signaling with ESRCH check.
- **B5 (Native Process):** Shell-quoted strings used strictly for UI display formatting, never passed to shell execution.
- **B6 (Environment):** Child processes inherit developer environment; IPC authentication tokens are injected explicitly.
- **B7 (Memory/Buffer):** Process output buffering capped at 10MB; stdin writes capped at 1MB (`MAX_STDIN_WRITE_BYTES`).
- **B8 (Lifecycle):** Abnormal process crash mitigation managed via `AgentResourceRegistry` and window event hooks.
- **B9 (Native FS):** Symlink loop traversal prevented via bounded recursion depth and canonical path deduplication.
- **B10 (Process Table):** Command line visibility in OS process table accepted as standard developer OS behavior; secrets passed via headers/env.
