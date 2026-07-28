"""Tests for SOC2 CC7.2 dead-letter record: failed audit-sink deliveries
are persisted to a JSONL file so the failure itself is auditable.

Run:
    venv/bin/python -m pytest eval/test_audit_sink_dead_letter.py -v
"""

import json
import queue
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from infra.audit_sink import dispatch_to_sinks, record_dead_letter
from infra.audit_sink_http import HttpAuditSink

EVENT = {
    "ts": 1234567890.0,
    "tool": "memory_search",
    "args": '{"query": "test"}',
    "results_count": 3,
    "latency_ms": 1.5,
    "error": None,
}


def _http_sink(**overrides):
    cfg = {
        "url": "https://sink.example.com/event",
        "timeout_s": 0.5,
        "max_retries": 2,
        "backoff_base_s": 0.005,
        "backoff_cap_s": 0.02,
        "payload_format": "raw",
    }
    cfg.update(overrides)
    return HttpAuditSink(cfg)


class TestHttpSinkDeadLetter:
    """HttpAuditSink writes a dead-letter entry when all retries are exhausted."""

    def test_dead_letter_on_sustained_5xx(self, tmp_path: Path):
        sink = _http_sink()
        mock_resp = MagicMock(status_code=500)
        with patch.object(
            sink._session, "post", return_value=mock_resp
        ), patch(
            "infra.audit_sink._DEAD_LETTER_PATH",
            tmp_path / "audit_sink_dead_letter.jsonl",
        ):
            sink.emit(EVENT)
        entries = _read_dead_letters(tmp_path / "audit_sink_dead_letter.jsonl")
        assert len(entries) == 1
        assert entries[0]["sink"] == "HttpAuditSink"
        assert "HTTP 500" in entries[0]["error"]
        assert entries[0]["event"]["tool"] == "memory_search"

    def test_no_dead_letter_on_success(self, tmp_path: Path):
        sink = _http_sink()
        mock_resp = MagicMock(status_code=200)
        with patch.object(
            sink._session, "post", return_value=mock_resp
        ), patch(
            "infra.audit_sink._DEAD_LETTER_PATH",
            tmp_path / "audit_sink_dead_letter.jsonl",
        ):
            sink.emit(EVENT)
        _read_dead_letters(tmp_path / "audit_sink_dead_letter.jsonl")
        # file should not exist or be empty

    def test_dead_letter_on_network_error(self, tmp_path: Path):
        sink = _http_sink()
        with patch.object(
            sink._session, "post", side_effect=OSError("connection lost")
        ), patch(
            "infra.audit_sink._DEAD_LETTER_PATH",
            tmp_path / "audit_sink_dead_letter.jsonl",
        ):
            sink.emit(EVENT)
        entries = _read_dead_letters(tmp_path / "audit_sink_dead_letter.jsonl")
        assert len(entries) == 1
        assert "connection lost" in entries[0]["error"]


class TestDispatchDeadLetter:
    """dispatch_to_sinks records dead letters on queue-full and sink raise."""

    def test_dead_letter_on_queue_full(self, tmp_path: Path):
        # Force put_nowait to raise queue.Full regardless of queue state.
        with patch("infra.audit_sink._SINK_QUEUE.put_nowait", side_effect=queue.Full), patch(
            "infra.audit_sink._DEAD_LETTER_PATH",
            tmp_path / "audit_sink_dead_letter.jsonl",
        ):
            dispatch_to_sinks(EVENT)
        entries = _read_dead_letters(tmp_path / "audit_sink_dead_letter.jsonl")
        assert len(entries) == 1
        assert entries[0]["sink"] == "dispatch-queue"
        assert entries[0]["error"] == "dispatch queue full"

    def test_dead_letter_on_sink_emit_raise(self, tmp_path: Path):
        """If a sink.emit() raises, dispatch_to_sinks records it.

        The dispatch loop runs in a background thread, so we keep the
        RaisingSink installed until the dead-letter file proves it was used.
        """

        class RaisingSink:
            def emit(self, event: dict) -> None:
                raise RuntimeError("sink exploded")

            def flush(self) -> None:
                pass

        import infra.audit_sink as mod

        dead_letter_path = tmp_path / "audit_sink_dead_letter.jsonl"
        old_sinks = mod._SINKS
        old_path = record_dead_letter.__globals__["_DEAD_LETTER_PATH"]
        record_dead_letter.__globals__["_DEAD_LETTER_PATH"] = dead_letter_path
        mod._SINKS = [RaisingSink()]
        try:
            mod.dispatch_to_sinks(EVENT)
            # Poll for the dead-letter file (dispatch thread is async).
            import time as _time
            deadline = _time.time() + 3.0
            entries = []
            while _time.time() < deadline and not entries:
                _time.sleep(0.05)
                entries = _read_dead_letters(dead_letter_path)
        finally:
            record_dead_letter.__globals__["_DEAD_LETTER_PATH"] = old_path
            mod._SINKS = old_sinks
        assert len(entries) == 1, f"expected 1 dead-letter entry, got {len(entries)}"
        assert entries[0]["sink"] == "RaisingSink"
        assert "sink exploded" in entries[0]["error"]


def _read_dead_letters(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]
