"""Tests that HttpAuditSink drops events after exhausting retries without crash.

Run:
    cd /Users/arka/.config/agentic-memory-audit
    /Users/arka/.config/agentic-memory/venv/bin/python -m pytest eval/test_audit_sink_drops_on_5xx.py -v
"""

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from infra.audit_sink_http import HttpAuditSink

EVENT = {
    "ts": 1234567890.0,
    "tool": "memory_search",
    "args": '{"query": "test"}',
    "results_count": 3,
    "latency_ms": 1.5,
    "error": None,
}


def _sink(**overrides) -> HttpAuditSink:
    cfg = {
        "url": "https://sink.example.com/event",
        "timeout_s": 1.0,
        "max_retries": 3,
        "backoff_base_s": 0.005,
        "backoff_cap_s": 0.02,
        "payload_format": "raw",
    }
    cfg.update(overrides)
    return HttpAuditSink(cfg)


class TestDropOn5xx:
    def test_drops_after_all_retries_exhausted(self):
        """emit never raises even when every attempt returns 500."""
        sink = _sink(max_retries=3)
        mock_resp = MagicMock(status_code=500)
        with patch.object(sink._session, "post", return_value=mock_resp) as mock_post:
            sink.emit(EVENT)
        assert mock_post.call_count == 4  # initial + 3 retries

    def test_no_exception_propagated(self):
        """Caller never sees an exception from emit on 5xx."""
        sink = _sink(max_retries=2)
        mock_resp = MagicMock(status_code=503)
        with patch.object(sink._session, "post", return_value=mock_resp):
            sink.emit(EVENT)

    def test_drops_on_persistent_network_error(self):
        """emit handles repeated network errors without crash."""
        sink = _sink(max_retries=2)
        with patch.object(
            sink._session, "post", side_effect=OSError("connection lost")
        ):
            sink.emit(EVENT)

    def test_logs_warning_on_drop(self):
        """A warning is logged when an event is dropped after retries."""
        sink = _sink(max_retries=1)
        mock_resp = MagicMock(status_code=500)
        logger = logging.getLogger("infra.audit_sink_http")
        with patch.object(logger, "warning") as mock_warn:
            with patch.object(sink._session, "post", return_value=mock_resp):
                sink.emit(EVENT)
        mock_warn.assert_called_once()
        assert "dropped event" in mock_warn.call_args[0][0]
