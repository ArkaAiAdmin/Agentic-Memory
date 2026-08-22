"""Tests for memory.toml config file and MemoryConfig dataclass.

Verifies:
  1. MemoryConfig defaults (22 fields)
  2. Loading from a TOML file
  3. Env var overrides (MEMORY_*)
  4. Missing config file returns defaults
  5. All tests use temp directories for isolation
"""

import os
import sys
import textwrap
from pathlib import Path

import pytest

_INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
if str(_INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALL_DIR))

from config import (
    MemoryConfig,
    _read_toml,
    _deep_get,
    _parse_bool,
    _parse_int,
    _parse_float,
    _resolve,
    reset_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(tmp_path: Path, content: str) -> Path:
    """Write a memory.toml file and return its path."""
    toml_path = tmp_path / "memory.toml"
    toml_path.write_text(textwrap.dedent(content))
    return toml_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMemoryConfigDefaults:
    """Verify MemoryConfig has all 21 nested config fields with correct defaults."""

    def test_field_count(self):
        """MemoryConfig should have the expected number of top-level fields."""
        import dataclasses

        fields = dataclasses.fields(MemoryConfig)
        # Post-refactor: 21 nested config dataclass fields (general, search,
        # kg, graph_cache, write, embedding, auto_save, sync, api,
        # quality_gates, sharing, cache, llm, hybrid, rerank, features,
        # user_profile, recall, semantic_kg, rate_limits, health_check).
        assert len(fields) == 21, (
            f"Expected 21 nested config fields, got {len(fields)}: {[f.name for f in fields]}"
        )

    def test_default_db_path(self):
        cfg = MemoryConfig()
        assert cfg.general.db_path == "memory/memory.db"

    def test_default_wal_checkpoint(self):
        cfg = MemoryConfig()
        assert cfg.general.wal_checkpoint_startup is True

    def test_default_unindexed_safety_net(self):
        cfg = MemoryConfig()
        assert cfg.general.unindexed_safety_net_limit == 1000

    def test_default_temporal_half_life(self):
        cfg = MemoryConfig()
        assert cfg.search.temporal_half_life == 180.0

    def test_default_temporal_decay_mode(self):
        cfg = MemoryConfig()
        assert cfg.search.temporal_decay_mode == "exponential"

    def test_default_late_interaction(self):
        cfg = MemoryConfig()
        assert cfg.search.late_interaction is True

    def test_default_knowledge_graph(self):
        cfg = MemoryConfig()
        assert cfg.search.knowledge_graph is True

    def test_default_graph_rag_hops(self):
        cfg = MemoryConfig()
        assert cfg.search.graph_rag_hops == 3

    def test_default_graph_rag_expansions(self):
        cfg = MemoryConfig()
        assert cfg.search.graph_rag_expansions == 5

    def test_default_query_cache(self):
        cfg = MemoryConfig()
        assert cfg.search.query_cache is True

    def test_default_reranker_disabled(self):
        cfg = MemoryConfig()
        assert cfg.search.reranker_disabled is False

    def test_default_contextual_retrieval(self):
        cfg = MemoryConfig()
        assert cfg.search.contextual_retrieval is True

    def test_default_multi_agent(self):
        cfg = MemoryConfig()
        assert cfg.features.multi_agent is True

    def test_default_summarization(self):
        cfg = MemoryConfig()
        assert cfg.features.summarization is True

    def test_default_user_profile(self):
        cfg = MemoryConfig()
        assert cfg.features.user_profile is True

    def test_default_self_directed(self):
        cfg = MemoryConfig()
        assert cfg.features.self_directed is True

    def test_default_adaptive_retention(self):
        cfg = MemoryConfig()
        assert cfg.features.adaptive_retention is True

    def test_default_consolidation(self):
        cfg = MemoryConfig()
        assert cfg.features.consolidation is True

    def test_default_quality_gates(self):
        cfg = MemoryConfig()
        assert cfg.features.quality_gates is True

    def test_default_fts5_cache(self):
        cfg = MemoryConfig()
        assert cfg.cache.fts5_cache is True

    def test_default_fts5_cache_ttl(self):
        cfg = MemoryConfig()
        assert cfg.cache.fts5_cache_ttl == 30

    def test_default_shared_pool_ttl_days(self):
        cfg = MemoryConfig()
        assert cfg.sharing.shared_pool_ttl_days == 30


class TestReadToml:
    """Test _read_toml helper."""

    def test_missing_file_returns_empty(self, tmp_path):
        result = _read_toml(tmp_path / "nonexistent.toml")
        assert result == {}

    def test_valid_toml_parsed(self, tmp_path):
        toml_path = _write_toml(
            tmp_path,
            """
            [general]
            db_path = "custom/path.db"
            wal_checkpoint_startup = true

            [search]
            temporal_half_life = 90.0

            [features]
            multi_agent = true
        """,
        )
        result = _read_toml(toml_path)
        assert result["general"]["db_path"] == "custom/path.db"
        assert result["general"]["wal_checkpoint_startup"] is True
        assert result["search"]["temporal_half_life"] == 90.0
        assert result["features"]["multi_agent"] is True


class TestDeepGet:
    """Test _deep_get helper for nested dict access."""

    def test_simple_key(self):
        assert _deep_get({"a": 1}, "a") == 1

    def test_nested_key(self):
        assert _deep_get({"a": {"b": 2}}, "a.b") == 2

    def test_missing_key(self):
        assert _deep_get({"a": 1}, "b") is None

    def test_missing_nested_key(self):
        assert _deep_get({"a": {"b": 1}}, "a.c") is None

    def test_deeply_nested(self):
        d = {"a": {"b": {"c": {"d": 42}}}}
        assert _deep_get(d, "a.b.c.d") == 42

    def test_non_dict_intermediate(self):
        assert _deep_get({"a": "string"}, "a.b") is None


class TestParseHelpers:
    """Test _parse_bool, _parse_int, _parse_float."""

    def test_parse_bool_true_values(self):
        for v in ("1", "true", "True", "TRUE", "yes", "Yes", "on", "ON"):
            assert _parse_bool(v) is True, f"_parse_bool({v!r}) should be True"

    def test_parse_bool_false_values(self):
        for v in ("0", "false", "False", "no", "No", "off", "OFF", ""):
            assert _parse_bool(v) is False, f"_parse_bool({v!r}) should be False"

    def test_parse_int_valid(self):
        assert _parse_int("42") == 42
        assert _parse_int("0") == 0
        assert _parse_int("-5") == -5

    def test_parse_int_invalid(self):
        with pytest.raises(ValueError):
            _parse_int("not_a_number")

    def test_parse_float_valid(self):
        assert _parse_float("3.14") == pytest.approx(3.14)
        assert _parse_float("0.0") == 0.0
        assert _parse_float("-1.5") == -1.5


class TestResolve:
    """Test _resolve: env var > TOML > default."""

    def test_default_used_when_nothing_set(self, tmp_path):
        toml_data = {}
        result = _resolve(
            "NONEXISTENT_KEY_XYZ", "general.db_path", "default.db", toml_data
        )
        assert result == "default.db"

    def test_toml_value_used(self, tmp_path):
        toml_data = {"general": {"db_path": "toml/path.db"}}
        result = _resolve(
            "NONEXISTENT_KEY_XYZ", "general.db_path", "default.db", toml_data
        )
        assert result == "toml/path.db"

    def test_env_var_overrides_toml(self, tmp_path):
        toml_data = {"general": {"db_path": "toml/path.db"}}
        os.environ["TEST_RESOLVE_OVERRIDE"] = "env_value"
        try:
            result = _resolve(
                "TEST_RESOLVE_OVERRIDE", "general.db_path", "default.db", toml_data
            )
            assert result == "env_value"
        finally:
            os.environ.pop("TEST_RESOLVE_OVERRIDE", None)

    def test_env_var_bool_parsing(self):
        os.environ["TEST_RESOLVE_BOOL"] = "true"
        try:
            result = _resolve("TEST_RESOLVE_BOOL", "features.x", False, {}, parser=None)
            assert result is True
        finally:
            os.environ.pop("TEST_RESOLVE_BOOL", None)

    def test_env_var_int_parsing(self):
        os.environ["TEST_RESOLVE_INT"] = "99"
        try:
            result = _resolve("TEST_RESOLVE_INT", "general.x", 0, {}, parser=None)
            assert result == 99
        finally:
            os.environ.pop("TEST_RESOLVE_INT", None)

    def test_env_var_unparseable_falls_back_with_warning(self, capsys):
        """C7 fix: an unparseable env var (e.g. ``'30 days'``) falls
        back to the default, but the operator sees a warning on
        stderr naming the env var, the value, and the failure.
        """
        import sys
        import io

        os.environ["TEST_RESOLVE_BAD"] = "30 days"
        # Capture stderr directly since the module uses ``print(..., file=sys.stderr)``
        # which may not be captured by capsys in all test runners.
        captured_err = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured_err
        try:
            result = _resolve("TEST_RESOLVE_BAD", "general.x", 30.0, {}, parser=None)
        finally:
            sys.stderr = old_stderr
        assert result == 30.0, "should fall back to default"
        err_text = captured_err.getvalue()
        assert "TEST_RESOLVE_BAD" in err_text, (
            f"expected warning to mention env var, got: {err_text!r}"
        )
        assert "30 days" in err_text
        assert "could not be parsed" in err_text
        os.environ.pop("TEST_RESOLVE_BAD", None)

    def test_int_toml_value_accepted_for_float_default(self):
        """TOML ``30`` (int) should be accepted when the dataclass
        field is ``float`` — Python's int/float are interchangeable
        in arithmetic, and TOML users routinely omit the trailing
        ``.0``.  No warning is expected.
        """
        toml_data = {"general": {"x": 30}}
        result = _resolve("NONEXISTENT_X_Y_Z", "general.x", 30.0, toml_data)
        assert result == 30


import config as _config_mod


class TestMissingConfigFile:
    """Test that missing memory.toml returns all defaults."""

    def test_missing_toml_returns_defaults(self, tmp_path):
        """When memory.toml doesn't exist, all fields should be defaults."""
        reset_config()
        original = getattr(_config_mod, "_TOML_PATH")
        # Save ALL MEMORY_* env vars (the system has many more than the
        # 5 originally listed; leaving any set will override defaults)
        saved = {}
        for key in list(os.environ):
            if key.startswith("MEMORY_"):
                saved[key] = os.environ.pop(key)
        setattr(_config_mod, "_TOML_PATH", tmp_path / "nonexistent.toml")
        setattr(_config_mod, "_instance", None)
        try:
            cfg = _config_mod.get_config()
            # db_path default ends with memory.db (under MEMORY_HOME/data or legacy memory/)
            assert cfg.db_path.endswith("memory.db")
            # H10 fix: _resolve defaults now match the MemoryConfig dataclass
            # (previously 12 fields had stale False defaults that silently
            # diverged when TOML was absent; all now default to True).
            assert cfg.wal_checkpoint_startup is True
            assert cfg.temporal_half_life == 180.0
            assert cfg.knowledge_graph is True
            assert cfg.multi_agent is True
            assert cfg.fts5_cache is True
        finally:
            setattr(_config_mod, "_TOML_PATH", original)
            setattr(_config_mod, "_instance", None)
            for key, val in saved.items():
                if val is not None:
                    os.environ[key] = val


class TestLoadFromToml:
    """Test loading MemoryConfig from a TOML file."""

    def test_custom_toml_values(self, tmp_path):
        """TOML values override defaults."""
        toml_path = _write_toml(
            tmp_path,
            """
            [general]
            db_path = "/custom/db.sqlite"
            wal_checkpoint_startup = true

            [search]
            temporal_half_life = 60.0
            knowledge_graph = true

            [features]
            multi_agent = true
            summarization = true
        """,
        )
        original = getattr(_config_mod, "_TOML_PATH")
        saved_env = {}
        for key in list(os.environ):
            if key.startswith("MEMORY_"):
                saved_env[key] = os.environ.pop(key)
        setattr(_config_mod, "_TOML_PATH", toml_path)
        setattr(_config_mod, "_instance", None)
        try:
            cfg = _config_mod.get_config()
            assert cfg.db_path == "/custom/db.sqlite"
            assert cfg.wal_checkpoint_startup is True
            assert cfg.temporal_half_life == 60.0
            assert cfg.knowledge_graph is True
            assert cfg.multi_agent is True
            assert cfg.summarization is True
        finally:
            setattr(_config_mod, "_TOML_PATH", original)
            setattr(_config_mod, "_instance", None)
            os.environ.update(saved_env)

    def test_partial_toml_fills_defaults(self, tmp_path):
        """Partial TOML only overrides specified fields; rest are defaults."""
        toml_path = _write_toml(
            tmp_path,
            """
            [search]
            temporal_half_life = 30.0
        """,
        )
        original = getattr(_config_mod, "_TOML_PATH")
        saved_env = {}
        for key in list(os.environ):
            if key.startswith("MEMORY_"):
                saved_env[key] = os.environ.pop(key)
        setattr(_config_mod, "_TOML_PATH", toml_path)
        setattr(_config_mod, "_instance", None)
        try:
            cfg = _config_mod.get_config()
            # Overridden
            assert cfg.temporal_half_life == 30.0
            # Still default
            assert cfg.db_path.endswith("memory.db")
            assert cfg.knowledge_graph is True
            # H10 fix: _resolve default is True (matches dataclass)
            assert cfg.multi_agent is True
        finally:
            setattr(_config_mod, "_TOML_PATH", original)
            setattr(_config_mod, "_instance", None)
            os.environ.update(saved_env)


class TestEnvVarOverrides:
    """Test that MEMORY_* env vars override TOML and defaults."""

    def test_db_path_override(self, tmp_path):
        saved = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = "/env/custom.db"
        try:
            setattr(_config_mod, "_instance", None)
            cfg = _config_mod.get_config()
            assert cfg.db_path == "/env/custom.db"
        finally:
            if saved is not None:
                os.environ["MEMORY_DB_PATH"] = saved
            else:
                os.environ.pop("MEMORY_DB_PATH", None)
            setattr(_config_mod, "_instance", None)

    def test_bool_override(self):
        os.environ["MEMORY_KNOWLEDGE_GRAPH"] = "1"
        try:
            setattr(_config_mod, "_instance", None)
            cfg = _config_mod.get_config()
            assert cfg.knowledge_graph is True
        finally:
            os.environ.pop("MEMORY_KNOWLEDGE_GRAPH", None)
            setattr(_config_mod, "_instance", None)

    def test_int_override(self):
        os.environ["MEMORY_GRAPH_RAG_HOPS"] = "7"
        try:
            setattr(_config_mod, "_instance", None)
            cfg = _config_mod.get_config()
            assert cfg.graph_rag_hops == 7
        finally:
            os.environ.pop("MEMORY_GRAPH_RAG_HOPS", None)
            setattr(_config_mod, "_instance", None)

    def test_float_override(self):
        os.environ["MEMORY_TEMPORAL_HALF_LIFE"] = "45.5"
        try:
            setattr(_config_mod, "_instance", None)
            cfg = _config_mod.get_config()
            assert cfg.temporal_half_life == 45.5
        finally:
            os.environ.pop("MEMORY_TEMPORAL_HALF_LIFE", None)
            setattr(_config_mod, "_instance", None)


class TestResetConfig:
    """Test the reset_config singleton reset."""

    def test_reset_clears_singleton(self):
        cfg1 = _config_mod.get_config()
        _config_mod.reset_config()
        cfg2 = _config_mod.get_config()
        # After reset, a new instance is created (different object)
        assert cfg1 is not cfg2
        # But same values
        assert cfg1.db_path == cfg2.db_path


class TestTomlIntegration:
    """Verify that importing/using subsystem flags correctly respects TOML config via __getattr__ hooks."""

    def test_toml_flags_integration(self, tmp_path):
        reset_config()
        # Clear any cached *_ENABLED values that earlier tests may have
        # left in target module __dicts__. As of the 2026-06-20 fix,
        # make_lazy_getattr caches in the target module (not the caller),
        # so importlib.reload() clears the cache; reset_all is a
        # belt-and-suspenders safety net for any test that imported a
        # lazy-config module without reloading.
        from infra.memory_common import reset_all_lazy_config_attrs

        reset_all_lazy_config_attrs()
        original_toml = getattr(_config_mod, "_TOML_PATH")

        # Write TOML with feature flags enabled
        toml_path = _write_toml(
            tmp_path,
            """
            [search]
            late_interaction = true
            knowledge_graph = true
            reranker_disabled = false

            [cache]
            fts5_cache = true

            [features]
            quality_gates = true
            user_profile = true
            consolidation = true
            summarization = true
            multi_agent = true
            adaptive_retention = true
            self_directed = true
        """,
        )

        # Clear any environment variables that might override this
        saved_env = {}
        for key in list(os.environ):
            if key.startswith("MEMORY_"):
                saved_env[key] = os.environ.pop(key)

        setattr(_config_mod, "_TOML_PATH", toml_path)
        setattr(_config_mod, "_instance", None)

        try:
            # Force config reload
            cfg = _config_mod.get_config()
            assert cfg.late_interaction is True
            assert cfg.features.quality_gates is True

            # Now import and verify dynamic properties
            import search_pipeline

            # Force reload/cleanup of sys.modules for these if they are already imported
            import importlib

            importlib.reload(search_pipeline)
            assert search_pipeline._LATE_INTERACTION_ENABLED is True
            assert search_pipeline._GRAPH_RAG_ENABLED is True

            import quality_gates

            importlib.reload(quality_gates)
            assert quality_gates.QUALITY_GATES_ENABLED is True

            import user_profile

            importlib.reload(user_profile)
            assert user_profile.PROFILE_ENABLED is True

            import consolidation

            importlib.reload(consolidation)
            assert consolidation.CONSOLIDATION_ENABLED is True

            import summarization

            importlib.reload(summarization)
            assert summarization.SUMMARIZATION_ENABLED is True

            import memory_sharing

            importlib.reload(memory_sharing)
            assert memory_sharing.MULTI_AGENT_ENABLED is True

            import knowledge_graph

            importlib.reload(knowledge_graph)
            assert knowledge_graph.KG_ENABLED is True

            import adaptive_retention

            importlib.reload(adaptive_retention)
            assert adaptive_retention.ADAPTIVE_RETENTION_ENABLED is True

            import self_directed

            importlib.reload(self_directed)
            assert self_directed.SELF_DIRECTED_ENABLED is True

            import infra.cache

            importlib.reload(infra.cache)
            assert infra.cache.SEARCH_CACHE_TTL_ENABLED is True

            import infra.saga

            importlib.reload(infra.saga)
            assert infra.saga.SAGA_ENABLED is True

            import infra.reranker

            importlib.reload(infra.reranker)
            # Force a fresh config read so the lazy resolve below sees
            # the test's TOML (reranker_disabled = false), not a stale
            # singleton from an earlier test that set
            # MEMORY_RERANKER_DISABLED=1.
            import config as _force_cfg

            setattr(_force_cfg, "_instance", None)
            setattr(_force_cfg, "_TOML_PATH", toml_path)
            _cfg = _force_cfg.get_config()
            assert _cfg.reranker_disabled is False
            # The canonical assertion. Safe now that make_lazy_getattr
            # caches in the target module's __dict__ (so importlib.reload
            # clears the cache), instead of the caller's globals.
            assert infra.reranker.RERANKER_ENABLED is True

        finally:
            setattr(_config_mod, "_TOML_PATH", original_toml)
            setattr(_config_mod, "_instance", None)
            os.environ.update(saved_env)


class TestNestedConfigBackwardsCompat:
    """Legacy flat-field access via __getattr__ still works."""

    def test_flat_access_equals_nested(self):
        """cfg.temporal_half_life must equal cfg.search.temporal_half_life."""
        cfg = MemoryConfig()
        assert cfg.temporal_half_life == cfg.search.temporal_half_life
        assert cfg.temporal_half_life == 180.0

    def test_backwards_compat_all_common_fields(self):
        """Spot-check: common legacy field names resolve via __getattr__."""
        cfg = MemoryConfig()
        assert cfg.db_path == cfg.general.db_path
        assert cfg.knowledge_graph == cfg.search.knowledge_graph
        assert cfg.write_journal == cfg.write.write_journal
        assert cfg.multi_agent == cfg.features.multi_agent
        assert cfg.fts5_cache == cfg.cache.fts5_cache

    def test_missing_attr_raises(self):
        cfg = MemoryConfig()
        with pytest.raises(AttributeError):
            _ = cfg.nonexistent_field_xyz


class TestNestedConfigDirectAccess:
    """Verify direct nested config attribute access."""

    def test_search_nested_values(self):
        cfg = MemoryConfig()
        assert cfg.search.temporal_half_life == 180.0
        assert cfg.search.forgetting_curve_half_life == 30.0
        assert cfg.search.deep_rerank_timeout == 30.0

    def test_feature_flags_nested(self):
        cfg = MemoryConfig()
        assert cfg.features.quality_gates is True
        assert cfg.features.feature_temporal_kg is True
        assert cfg.features.saga_enabled is True

    def test_health_check_nested(self):
        cfg = MemoryConfig()
        assert cfg.health_check.vec_index_drift_threshold == 50
        assert cfg.health_check.disk_pct_used_threshold == 95

    def test_llm_config_nested(self):
        cfg = MemoryConfig()
        assert cfg.llm.provider == "none"
        assert cfg.llm.extraction_model_id == "Qwen/Qwen2.5-3B-Instruct"

    def test_auto_save_nested(self):
        cfg = MemoryConfig()
        assert cfg.auto_save.max_retries == 3
        assert cfg.auto_save.circuit_breaker_seconds == 300.0

    def test_write_config_nested(self):
        cfg = MemoryConfig()
        assert cfg.write.write_journal is False
        assert cfg.write.defer_expensive is True


class TestHealthCheckConfigDefaults:
    """Verify default health-check thresholds."""

    def test_drift_default(self):
        cfg = MemoryConfig()
        assert cfg.health_check.vec_index_drift_threshold == 50

    def test_disk_default(self):
        cfg = MemoryConfig()
        assert cfg.health_check.disk_pct_used_threshold == 95


class TestHealthCheckDynamicThresholds:
    """Verify health-check thresholds can be overridden via env/TOML."""

    def test_drift_threshold_override(self, tmp_path):
        reset_config()
        original_toml = getattr(_config_mod, "_TOML_PATH")
        toml_path = _write_toml(
            tmp_path,
            """
            [health_check]
            vec_index_drift_threshold = 100
            disk_pct_used_threshold = 98
            """,
        )
        saved_env = {}
        for key in list(os.environ):
            if key.startswith("MEMORY_"):
                saved_env[key] = os.environ.pop(key)
        setattr(_config_mod, "_TOML_PATH", toml_path)
        setattr(_config_mod, "_instance", None)
        try:
            cfg = _config_mod.get_config()
            assert cfg.health_check.vec_index_drift_threshold == 100
            assert cfg.health_check.disk_pct_used_threshold == 98
        finally:
            setattr(_config_mod, "_TOML_PATH", original_toml)
            setattr(_config_mod, "_instance", None)
            os.environ.update(saved_env)

    def test_drift_threshold_via_env(self):
        os.environ["MEMORY_VEC_INDEX_DRIFT_THRESHOLD"] = "200"
        try:
            setattr(_config_mod, "_instance", None)
            cfg = _config_mod.get_config()
            assert cfg.health_check.vec_index_drift_threshold == 200
        finally:
            os.environ.pop("MEMORY_VEC_INDEX_DRIFT_THRESHOLD", None)
            setattr(_config_mod, "_instance", None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
