# Pluggable Audit Sink

Phase 3 — fan-out every audit event to local and external sinks without
blocking the tool-call hot path. Sinks are additive to the existing
`memory_audit_log` local DB write path.

## Architecture

```
enqueue_audit()  ──→  memory_audit_log (local DB, always on)
                 │
                 └──→ dispatch_to_sinks() ──→ bounded queue
                                                  │
                                            dispatch thread
                                           ┌────┼────┐
                                           │    │    │
                                      FileSink  │  HttpSink
                                           PromSink
```

*   `AuditSink` is a `typing.Protocol` with `emit(event)` and `flush()`.
*   `dispatch_to_sinks()` is fire-and-forget — enqueues the event into a
    bounded `queue.Queue` (max 10 000 entries) and never blocks the caller.
*   A dedicated daemon thread drains the queue, applies
    **OWASP A09-001 redaction** to every event (defense-in-depth), then
    fans out to all configured sinks.
*   If the queue is full the event is silently dropped (DEBUG log) —
    losing one forwarded event is acceptable, blocking a tool call is not.

## Sinks

| Sink | Always-on | Config | Behaviour |
|------|-----------|--------|-----------|
| `FileAuditSink` | Yes | `path` (default: `<memory_dir>/audit_sink.jsonl`), `max_bytes` (50 MB), `backups` (5) | Rolling JSONL — one JSON object per line. Rotates when size exceeds `max_bytes`. |
| `PrometheusAuditSink` | Yes | None | In-process counters keyed by `(tool, status)`. Exposed via `PROM_AUDIT_SINK_TEXT` module-level registry for external scrapers. |
| `HttpAuditSink` | No (`[audit.sinks.http].url` must be set) | `url`, `headers`, `auth`, `timeout_s`, `max_retries`, `backoff_base_s`, `backoff_cap_s`, `payload_format` | POSTs each event to a SIEM HTTP intake (Splunk HEC / Elasticsearch / Datadog). Retries on network errors and 5xx with exponential backoff. 4xx is not retried. |

### Payload Format

*   `splunk` (default): `{"event": <event dict>, "time": <ts>}`
*   `raw`: the entire event dict POSTed as JSON

## PII Redaction (OWASP A09-001)

Redaction is applied at two layers — both in `enqueue_audit()` (before
serializing into `memory_audit_log`) and again in the sink dispatch thread
(before forwarding to any external sink), so secrets never leave the
process.

### Key-name patterns (covered by `_SECRET_KEY_RE`)

| Pattern | Example |
|---------|---------|
| `token` | `{"token": "sk-..."}` |
| `secret` | `{"secret": "..."}` |
| `password` / `passwd` | `{"password": "s3cret"}` |
| `api_key` / `apikey` | `{"api_key": "..."}` |
| `authorization` / `auth` | `{"Authorization": "Bearer ..."}` |
| `credential` | `{"credential": "..."}` |

### Value patterns (covered by `_SECRET_VALUE_RE`)

| Pattern | Description |
|---------|-------------|
| `sk-[A-Za-z0-9]{20,}` | OpenAI-style secret keys |
| base64 string ≥40 chars | Long base64 tokens |
| hex string ≥40 chars | Long hex tokens (≥160 bits) |

Redaction is recursive: nested dicts and lists are traversed. The
original event dict is never mutated (a defensive copy is made).

For the full test matrix see `eval/test_audit_sink_principal_redact.py`.

## Configuration (`memory.toml`)

```toml
[audit.sinks.http]
# url = "https://splunk.example.com/services/collector/event"
# headers = { "Authorization" = "Splunk XXXX" }
# auth = "Splunk XXXX"
# timeout_s = 5.0
# max_retries = 5
# backoff_base_s = 1.0
# backoff_cap_s = 30.0
# payload_format = "splunk"
```

All keys except `url` have sensible defaults. The HTTP sink is disabled
when `url` is absent/empty. File and Prometheus sinks are always active
and need no configuration.

## Lifetime Management

| Function | Purpose |
|----------|---------|
| `dispatch_to_sinks(event)` | Enqueue an event for fan-out |
| `flush_sinks(timeout)` | Best-effort flush all sinks |
| `reload_sinks()` | Drop cached sink list so next config load picks up changes |
| `AuditSink.emit(event)` | Forward one event to the external destination |
| `AuditSink.flush()` | Best-effort flush of any buffered state |

## Behavioural Properties

*   **Fire-and-forget**: never blocks the caller, never raises into the
    caller.
*   **Bounded queue**: max 10 000 events. Full queue → silent drop.
*   **Retry (HTTP)**: exponential backoff (base 1s, cap 30s, max 5
    retries). 4xx responses are not retried.
*   **Thread-safe**: sinks hold their own locks; the dispatch thread is
    the sole consumer of the queue.
*   **No-crash guarantee**: a single misbehaving sink cannot crash the
    process — all `emit` calls are wrapped in try/except.
*   **Defense in depth**: redaction is applied at both the local log
    (`enqueue_audit`) and the sink dispatch thread, so PII is masked
    even if a caller bypasses `enqueue_audit`.

## Source Files

| File | Role |
|------|------|
| `infra/audit_sink.py` | `AuditSink` Protocol, redaction, dispatch thread |
| `infra/audit_sink_file.py` | JSONL rolling file sink |
| `infra/audit_sink_http.py` | HTTP POST SIEM sink |
| `infra/audit_sink_prom.py` | Prometheus in-process counters |
| `infra/audit.py` | `enqueue_audit` → wired `dispatch_to_sinks` call |

## Test Coverage

| File | Tests | Focus |
|------|-------|-------|
| `eval/test_audit_sink_http.py` | 11 | Payload shape, auth headers, retry logic |
| `eval/test_audit_sink_drops_on_5xx.py` | 4 | Drop-after-retries, no-crash guarantee, warning logging |
| `eval/test_audit_sink_principal_redact.py` | 14 | Key-name redaction, value pattern redaction, recursion, immutability |

Run: `python -m pytest eval/test_audit_sink_http.py eval/test_audit_sink_drops_on_5xx.py eval/test_audit_sink_principal_redact.py -v`
