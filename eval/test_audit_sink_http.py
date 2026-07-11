"""Tests for HttpAuditSink — payload shape, retries, auth headers.

Run:
    cd /Users/arka/.config/agentic-memory-audit
    /Users/arka/.config/agentic-memory/venv/bin/python -m pytest eval/test_audit_sink_http.py -v
"""

import json
import sys
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

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
        "max_retries": 2,
        "backoff_base_s": 0.01,
        "backoff_cap_s": 0.05,
        "payload_format": "splunk",
    }
    cfg.update(overrides)
    return HttpAuditSink(cfg)


class TestPayloadShape:
    def test_splunk_format_wraps_event(self):
        sink = _sink(payload_format="splunk")
        body = json.loads(sink._build_body(EVENT))
        assert body["event"] == EVENT
        assert body["time"] == EVENT["ts"]

    def test_raw_format_passes_through(self):
        sink = _sink(payload_format="raw")
        body = json.loads(sink._build_body(EVENT))
        assert body == EVENT

    def test_splunk_is_default(self):
        sink = _sink()
        assert sink.payload_format == "splunk"


class TestAuthHeaders:
    def test_auth_alias_sets_authorization(self):
        sink = _sink(auth="Splunk deadbeef")
        assert sink.headers["Authorization"] == "Splunk deadbeef"

    def test_headers_dict_merged(self):
        sink = _sink(headers={"X-Custom": "val1", "X-Other": "val2"})
        assert sink.headers["X-Custom"] == "val1"
        assert sink.headers["X-Other"] == "val2"

    def test_auth_does_not_override_explicit_header(self):
        sink = _sink(
            headers={"Authorization": "Explicit token"},
            auth="Splunk deadbeef",
        )
        assert sink.headers["Authorization"] == "Explicit token"


class TestRetries:
    def test_successful_post_no_retry(self):
        sink = _sink()
        mock_resp = MagicMock(status_code=200)
        with patch.object(sink._session, "post", return_value=mock_resp) as mock_post:
            sink.emit(EVENT)
        mock_post.assert_called_once()

    def test_retries_on_5xx_then_succeeds(self):
        sink = _sink(max_retries=3, backoff_base_s=0.005, backoff_cap_s=0.02)
        mock_resp_ok = MagicMock(status_code=200)
        mock_resp_5xx = MagicMock(status_code=503)
        responses = [mock_resp_5xx, mock_resp_5xx, mock_resp_ok]
        with patch.object(sink._session, "post", side_effect=responses) as mock_post:
            sink.emit(EVENT)
        assert mock_post.call_count == 3

    def test_no_retry_on_4xx(self):
        sink = _sink(max_retries=3, backoff_base_s=0.005, backoff_cap_s=0.02)
        mock_resp = MagicMock(status_code=400)
        with patch.object(sink._session, "post", return_value=mock_resp) as mock_post:
            sink.emit(EVENT)
        mock_post.assert_called_once()

    def test_retries_on_network_error(self):
        sink = _sink(max_retries=2, backoff_base_s=0.005, backoff_cap_s=0.02)
        with patch.object(
            sink._session, "post", side_effect=ConnectionError("reset")
        ) as mock_post:
            sink.emit(EVENT)
        assert mock_post.call_count == 3  # initial + 2 retries

    def test_payload_and_headers_sent(self):
        sink = _sink(headers={"X-Custom": "val"})
        mock_resp = MagicMock(status_code=200)
        with patch.object(sink._session, "post", return_value=mock_resp) as mock_post:
            sink.emit(EVENT)
        mock_post.assert_called_once_with(
            "https://sink.example.com/event",
            data=ANY,
            headers={"X-Custom": "val"},
            timeout=1.0,
        )
        body = json.loads(mock_post.call_args[1]["data"])
        assert body["event"]["tool"] == "memory_search"
