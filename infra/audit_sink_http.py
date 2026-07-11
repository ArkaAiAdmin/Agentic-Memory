"""HTTP/HTTPS POST audit sink for Splunk HEC / Elasticsearch / Datadog.

One flexible config (``[audit.sinks.http]`` in memory.toml):

    [audit.sinks.http]
    url = "https://splunk.example.com/services/collector/event"
    headers = { "Authorization" = "Splunk XXXX" }   # or use auth = "..."
    auth = "Splunk XXXX"                              # alias for Authorization
    timeout_s = 5.0
    max_retries = 5
    backoff_base_s = 1.0
    backoff_cap_s = 30.0
    payload_format = "splunk"   # "splunk" -> {"event":..., "time":...}

Behavior:
  * Best-effort delivery. Retries on network error and on 5xx with
    exponential backoff. 4xx responses are NOT retried (logged, dropped).
  * After ``max_retries`` failures the event is dropped — the caller is
    never blocked and never sees the failure.
  * All sends happen from the dispatch thread (off the audit hot path), so
    the backoff sleeps never delay a tool call.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import requests

from infra.audit_sink import AuditSink

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_CAP_S = 30.0


class HttpAuditSink:
    """POST each audit event as JSON to a SIEM HTTP intake endpoint."""

    def __init__(self, cfg: dict) -> None:
        self.url = cfg["url"]
        self.headers = dict(cfg.get("headers") or {})
        # ``auth`` is a convenience alias for the Authorization header.
        auth = cfg.get("auth")
        if auth and "Authorization" not in self.headers:
            self.headers["Authorization"] = auth
        self.timeout = float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
        self.max_retries = int(cfg.get("max_retries", DEFAULT_MAX_RETRIES))
        self.backoff_base = float(cfg.get("backoff_base_s", DEFAULT_BACKOFF_BASE_S))
        self.backoff_cap = float(cfg.get("backoff_cap_s", DEFAULT_BACKOFF_CAP_S))
        fmt = (cfg.get("payload_format") or "splunk").lower()
        self.payload_format = fmt
        self._session = requests.Session()
        self._lock = threading.Lock()

    def _build_body(self, event: dict) -> bytes:
        if self.payload_format == "raw":
            return json.dumps(event, default=str).encode("utf-8")
        # Default "splunk" HEC shape: {event, time}. Compatible enough with
        # ES/Datadog which also accept a JSON body containing an "event" key.
        return json.dumps(
            {"event": event, "time": event.get("ts")},
            default=str,
        ).encode("utf-8")

    def emit(self, event: dict) -> None:
        body = self._build_body(event)
        last_exc: Exception | None = None
        with self._lock:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = self._session.post(
                        self.url,
                        data=body,
                        headers=self.headers,
                        timeout=self.timeout,
                    )
                    if resp.status_code < 500:
                        # 2xx/3xx success; 4xx is a client error we won't retry.
                        if resp.status_code >= 400:
                            logger.warning(
                                "audit http sink got HTTP %s from %s (not retried)",
                                resp.status_code,
                                self.url,
                            )
                        return
                    last_exc = RuntimeError(f"HTTP {resp.status_code}")
                except Exception as exc:  # network error / timeout
                    last_exc = exc
                if attempt < self.max_retries:
                    sleep_s = min(self.backoff_base * (2 ** attempt), self.backoff_cap)
                    time.sleep(sleep_s)
        logger.warning(
            "audit http sink dropped event after %d retries (url=%s): %s",
            self.max_retries,
            self.url,
            last_exc,
        )
        from infra.audit_sink import record_dead_letter
        record_dead_letter(event, str(last_exc), "HttpAuditSink")

    def flush(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover - defensive
            pass
