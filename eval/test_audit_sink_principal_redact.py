"""Tests for PII / secret redaction at the audit sink layer.

Run:
    cd /Users/arka/.config/agentic-memory-audit
    /Users/arka/.config/agentic-memory/venv/bin/python -m pytest eval/test_audit_sink_principal_redact.py -v
"""

import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from infra.audit_sink import (
    REDACTED_MASK,
    redact_audit_value,
    redact_event,
)


class TestRedactAuditValue:
    def test_skips_non_sensitive_dict(self):
        d = {"tool": "memory_search", "query": "hello"}
        assert redact_audit_value(d) == d

    def test_redacts_secret_key(self):
        d = {"token": "sk-abc123", "query": "hello"}
        result = redact_audit_value(d)
        assert result["token"] == REDACTED_MASK
        assert result["query"] == "hello"

    def test_redacts_case_insensitive_key(self):
        d = {"ApiKey": "12345", "Authorization": "Bearer xyz"}
        result = redact_audit_value(d)
        assert result["ApiKey"] == REDACTED_MASK
        assert result["Authorization"] == REDACTED_MASK

    def test_redacts_nested_secret(self):
        d = {"nested": {"password": "s3cret", "user": "admin"}}
        result = redact_audit_value(d)
        assert result["nested"]["password"] == REDACTED_MASK
        assert result["nested"]["user"] == "admin"

    def test_redacts_openai_style_token_in_value(self):
        d = {"token": "sk-proj-A" * 10}
        result = redact_audit_value(d)
        assert result["token"] == "sk-proj-A" * 10  # key match takes priority
        # But if key is innocuous, value pattern should trigger:
        d2 = {"input": "sk-" + "A" * 30}
        result2 = redact_audit_value(d2)
        assert result2["input"] == REDACTED_MASK

    def test_redacts_long_hex_value(self):
        d = {"input": "abcdef0123456789" * 3}  # 48 hex chars >=40
        result = redact_audit_value(d)
        assert result["input"] == REDACTED_MASK

    def test_short_hex_passes_through(self):
        d = {"input": "abc123"}  # 6 hex chars < 40
        result = redact_audit_value(d)
        assert result["input"] == "abc123"

    def test_list_redaction(self):
        d = {"items": [{"token": "xyz"}, {"name": "safe"}]}
        result = redact_audit_value(d)
        assert result["items"][0]["token"] == REDACTED_MASK
        assert result["items"][1]["name"] == "safe"

    def test_non_dict_non_list_passes(self):
        assert redact_audit_value("hello") == "hello"
        assert redact_audit_value(42) == 42
        assert redact_audit_value(None) is None


class TestRedactEvent:
    def test_redacts_args(self):
        event = {
            "tool": "memory_search",
            "args": {"api_key": "sk-123"},
        }
        result = redact_event(event)
        assert result["args"]["api_key"] == REDACTED_MASK
        assert result["tool"] == "memory_search"

    def test_redacts_principal(self):
        event = {
            "tool": "memory_save",
            "principal": {"token": "tkn_secret", "id": "user-1"},
        }
        result = redact_event(event)
        assert result["principal"]["token"] == REDACTED_MASK
        assert result["principal"]["id"] == "user-1"

    def test_skips_args_none(self):
        event = {"tool": "memory_search", "args": None}
        result = redact_event(event)
        assert result["args"] is None

    def test_skips_missing_principal(self):
        event = {"tool": "memory_search", "args": {}}
        result = redact_event(event)
        assert "principal" not in result

    def test_does_not_mutate_original(self):
        original = {"tool": "memory_search", "args": {"token": "sk-xxx"}}
        redact_event(original)
        assert original["args"]["token"] == "sk-xxx"
