"""Phase 3 (Pluggable Audit Sink): sink interface + fan-out dispatcher.

Defines ``AuditSink`` (a :class:`typing.Protocol`) plus a bounded,
fire-and-forget dispatch queue that forwards every audit event to all
configured sinks **without ever blocking the audit hot path**.

Sinks are additive to the existing local ``memory_audit_log`` write path:
``enqueue_audit`` (infra/audit.py) still writes the local log, and also
hands the event to ``dispatch_to_sinks``. If the dispatch queue is full
the event is silently dropped (DEBUG log) — losing one forwarded event is
acceptable, blocking a tool call is not.

Built-in sinks:
  * ``FileAuditSink``      — rolling JSONL, always available.
  * ``PrometheusAuditSink`` — Prometheus counters, always available.
  * ``HttpAuditSink``      — Splunk HEC / Elasticsearch / Datadog intake,
                             enabled when ``[audit.sinks.http]`` is set in
                             memory.toml.

PII redaction (OWASP A09-001) is applied at the sink layer too, so secrets
never leave the process even on the way to an external SIEM.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time as _time
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Bounded queue: matches the existing pattern in infra/audit.py. A burst of
# 10k forwarded events is enough headroom without risking OOM on a runaway
# caller. Drops are logged at DEBUG; the caller never blocks on enqueue.
AUDIT_SINK_QUEUE_MAXSIZE = 10_000

# How often the dispatch thread wakes up to drain the queue.
AUDIT_SINK_FLUSH_INTERVAL_S = 0.1

# --- OWASP A09-001: secret redaction for audit payloads -------------------
# Centralized here (and re-used by infra/audit.py) so the local log AND the
# external sinks share one redaction implementation.
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|api_key|apikey|authorization|auth|passwd|credential)",
    re.IGNORECASE,
)

# High-entropy string values that look like bearer/secret tokens regardless
# of their key name.
_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9]{20,})"  # OpenAI-style sk- tokens
    r"|([A-Za-z0-9+/]{40,}={0,2})"  # long base64 token
    r"|([A-Fa-f0-9]{40,})"  # long hex token (>=160 bits)
)

REDACTED_MASK = "***REDACTED***"


def redact_audit_value(args: Any) -> Any:
    """Recursively mask secret values (mirrors infra/audit.py _redact_args).

    A value is masked when its dict KEY matches ``_SECRET_KEY_RE`` or the
    value is a string matching ``_SECRET_VALUE_RE``. Non-sensitive args are
    returned unchanged (deeply, so the caller's structure is not mutated).
    """
    if isinstance(args, dict):
        redacted: dict = {}
        for key, value in args.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                redacted[key] = REDACTED_MASK
            else:
                redacted[key] = redact_audit_value(value)
        return redacted
    if isinstance(args, (list, tuple)):
        return [redact_audit_value(v) for v in args]
    if isinstance(args, str) and _SECRET_VALUE_RE.search(args):
        return REDACTED_MASK
    return args


def redact_event(event: dict) -> dict:
    """Return a PII-safe copy of an audit event for forwarding to sinks."""
    e = dict(event)
    if e.get("args") is not None:
        e["args"] = redact_audit_value(e["args"])
    if isinstance(e.get("principal"), dict):
        e["principal"] = redact_audit_value(e["principal"])
    return e


@runtime_checkable
class AuditSink(Protocol):
    """Interface every audit sink must satisfy."""

    def emit(self, event: dict) -> None:
        """Forward one audit event. Must never raise into the caller."""
        ...

    def flush(self) -> None:
        """Best-effort flush of any buffered state. Must not block long."""
        ...


# --- dispatch plumbing -----------------------------------------------------
_SINKS: Optional[list[AuditSink]] = None
_SINKS_LOCK = threading.Lock()
_SINK_QUEUE: "queue.Queue[dict]" = queue.Queue(maxsize=AUDIT_SINK_QUEUE_MAXSIZE)
_SINK_SHUTDOWN = threading.Event()
_SINK_THREAD: Optional[threading.Thread] = None
_SINK_LOCK = threading.Lock()

# --- SOC2 CC7.2 dead-letter log --------------------------------------------
# Persistent JSONL for audit events that could not be forwarded to any
# sink. Written directly to disk (not through dispatch_to_sinks) so the
# record survives queue-full drops and sink failures alike.
_DEAD_LETTER_PATH = Path(__file__).resolve().parent.parent / "memory" / "audit_sink_dead_letter.jsonl"
_DEAD_LETTER_LOCK = threading.Lock()


def record_dead_letter(event: dict, error: str, sink_name: str) -> None:
    """Append one failed dispatch entry to the dead-letter JSONL.

    Each line is a self-contained JSON object with ts/sink/error/event.
    Failures to write the dead-letter itself are logged but never raised
    (a dead-letter write failure must not crash the dispatch thread).
    """
    entry = {
        "ts": _time.time(),
        "sink": sink_name,
        "error": error,
        "event": event,
    }
    try:
        _DEAD_LETTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _DEAD_LETTER_LOCK:
            with open(_DEAD_LETTER_PATH, "a") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
    except Exception as exc:
        logger.warning("dead-letter write failed: %s", exc)


def _load_http_sink_config() -> Optional[dict]:
    """Read ``[audit.sinks.http]`` from memory.toml. Returns None if unset."""
    try:
        from infra.config import _deep_get, _read_toml, _resolve_toml_path

        toml = _read_toml(_resolve_toml_path())
        cfg = _deep_get(toml, "audit.sinks.http")
        if isinstance(cfg, dict) and cfg.get("url"):
            return cfg
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("audit sink http config load failed: %s", exc)
    return None


def _build_sinks() -> list[AuditSink]:
    """Construct the configured sink list. File + Prom are always on; HTTP
    is added only when ``[audit.sinks.http].url`` is present."""
    from infra.audit_sink_file import FileAuditSink
    from infra.audit_sink_prom import PrometheusAuditSink

    sinks: list[AuditSink] = [FileAuditSink(), PrometheusAuditSink()]
    http_cfg = _load_http_sink_config()
    if http_cfg:
        from infra.audit_sink_http import HttpAuditSink

        sinks.append(HttpAuditSink(http_cfg))
    return sinks


def _get_sinks() -> list[AuditSink]:
    global _SINKS
    if _SINKS is None:
        with _SINKS_LOCK:
            if _SINKS is None:
                try:
                    _SINKS = _build_sinks()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception("failed to build audit sinks: %s", exc)
                    _SINKS = []
    return _SINKS


def _ensure_dispatch_thread() -> None:
    global _SINK_THREAD
    with _SINK_LOCK:
        if _SINK_THREAD is not None and _SINK_THREAD.is_alive():
            return
        _SINK_SHUTDOWN.clear()
        _SINK_THREAD = threading.Thread(
            target=_dispatch_loop,
            name="audit-sink-dispatch",
            daemon=True,
        )
        _SINK_THREAD.start()


def _dispatch_loop() -> None:
    while not _SINK_SHUTDOWN.is_set():
        try:
            event = _SINK_QUEUE.get(timeout=AUDIT_SINK_FLUSH_INTERVAL_S)
        except queue.Empty:
            continue
        # Redact here (defense-in-depth) so every sink receives a PII-safe
        # copy even if a caller bypassed enqueue_audit redaction.
        redacted = redact_event(event)
        for sink in _get_sinks():
            try:
                sink.emit(redacted)
            except Exception as exc:
                logger.warning(
                    "audit sink %s.emit failed: %s",
                    type(sink).__name__,
                    exc,
                )
                record_dead_letter(
                    redacted,
                    str(exc),
                    type(sink).__name__,
                )


def dispatch_to_sinks(event: dict) -> None:
    """Fire-and-forget fan-out to all configured sinks.

    Never blocks and never raises: if the bounded queue is full the event is
    dropped (DEBUG log) and a dead-letter record is written so SOC2 CC7.2
    evidence is not silently lost.
    """
    _ensure_dispatch_thread()
    try:
        _SINK_QUEUE.put_nowait(event)
    except queue.Full:
        logger.debug(
            "audit sink queue full (%d entries), dropping event for %s",
            _SINK_QUEUE.qsize(),
            event.get("tool"),
        )
        record_dead_letter(event, "dispatch queue full", "dispatch-queue")


def flush_sinks(timeout: float = 5.0) -> None:
    """Best-effort flush of every configured sink."""
    deadline = __import__("time").time() + timeout
    for sink in _get_sinks():
        try:
            sink.flush()
        except Exception as exc:
            logger.warning("audit sink %s.flush failed: %s", type(sink).__name__, exc)
        # Yield a little so the dispatch thread can drain before flushing.
        remaining = deadline - __import__("time").time()
        if remaining > 0:
            __import__("time").sleep(min(0.02, remaining))


def reload_sinks() -> None:
    """Forget the cached sink list so config changes take effect on next use."""
    global _SINKS
    with _SINKS_LOCK:
        _SINKS = None


__all__ = [
    "AuditSink",
    "REDACTED_MASK",
    "redact_audit_value",
    "redact_event",
    "dispatch_to_sinks",
    "flush_sinks",
    "reload_sinks",
    "AUDIT_SINK_QUEUE_MAXSIZE",
]
