"""Tests for feature flag visibility (Phase 5)."""

from __future__ import annotations

import json
import logging

import pytest

from config import get_feature_flags, log_feature_flags_at_startup, reset_config


class TestFeatureFlags:
    """Test feature flag enumeration + warnings."""

    def test_all_flags_present(self):
        flags = get_feature_flags()
        expected_keys = {
            "multi_agent",
            "summarization",
            "user_profile",
            "self_directed",
            "adaptive_retention",
            "temporal_ssm_enabled",
            "consolidation",
            "quality_gates",
            "saga_enabled",
            "temporal_tiers",
            "crdt_enabled",
            "llm_extraction",
            "feature_temporal_kg",
            "fts5_cache",
            "query_cache",
            "reranker_disabled",
            "contextual_retrieval",
        }
        assert expected_keys.issubset(set(flags.keys())), f"missing: {expected_keys - set(flags.keys())}"

    def test_each_flag_has_required_keys(self):
        flags = get_feature_flags()
        required = {"value", "env_var", "toml_path", "default", "warnings"}
        for name, meta in flags.items():
            assert required.issubset(set(meta.keys())), f"{name} missing keys: {required - set(meta.keys())}"
            # Most flags are bool, but some (e.g. neural_forget_mode) are str.
            assert isinstance(meta["value"], (bool, str, int, float))
            assert isinstance(meta["warnings"], list)

    def test_disabled_temporal_kg_has_warning(self, monkeypatch):
        monkeypatch.setenv("MEMORY_TEMPORAL_KG", "0")
        reset_config()
        flags = get_feature_flags()
        kg_warnings = flags["feature_temporal_kg"]["warnings"]
        assert len(kg_warnings) >= 1
        assert "temporal" in kg_warnings[0].lower()

    def test_enabled_flags_have_empty_warnings(self):
        flags = get_feature_flags()
        for name, meta in flags.items():
            if meta["value"] is True:
                assert meta["warnings"] == [], f"{name} should have no warnings when enabled"

    def test_log_feature_flags_emits_json(self, caplog):
        caplog.set_level(logging.INFO)
        log_feature_flags_at_startup()
        messages = [r.getMessage() for r in caplog.records]
        flag_msgs = [m for m in messages if m.startswith("feature_flags_snapshot=")]
        assert len(flag_msgs) >= 1
        raw = flag_msgs[0].split("=", 1)[1]
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        assert len(parsed) >= 15
