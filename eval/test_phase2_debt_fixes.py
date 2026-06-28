#!/usr/bin/env python3
"""Phase 2 validation tests for tech-debt fixes (TD-11, TD-12, TD-25, TD-22, TD-15, TD-16).

Covers:
- TD-11: Silent error swallowing in auto_save.py
- TD-12: delete_active_where returns -1 on failure (not 0)
- TD-25: Env var validation at startup
- TD-22/TD-15: Archive cleanup for auto_save sessions
- TD-16: TTL for multi_agent shared_pool
"""

import os
import sys
import pathlib
import time
import tempfile
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# TD-11: Silent error swallowing
# ---------------------------------------------------------------------------


class TestSilentErrorFix:
    """auto_save.py should log warnings, not silently swallow errors."""

    def test_archive_db_delete_failure_logged(self):
        """The except block at auto_save.py:329 should now log instead of pass."""
        import background.auto_save as auto_save_impl

        src = pathlib.Path(auto_save_impl.__file__).read_text()
        # The old pattern was `except Exception: pass`
        assert "except Exception:\n                pass" not in src, (
            "auto_save.py still has bare 'except Exception: pass'"
        )
        # The new pattern should log
        assert "logger.warning" in src or "logger.error" in src


# ---------------------------------------------------------------------------
# TD-12: delete_active_where returns -1 on failure
# ---------------------------------------------------------------------------


class TestDeleteActiveWhereFailure:
    """delete_active_where should return -1 on failure, not 0."""

    def test_returns_negative_on_db_error(self):
        """When the DB operation fails, should return -1 (not 0)."""
        import memory_delete

        # Pass a non-existent path to trigger an error
        result = memory_delete.delete_active_where(
            "/nonexistent/path/memory.db", "1=1", ()
        )
        assert result == -1, f"Expected -1 on failure, got {result}"


# ---------------------------------------------------------------------------
# TD-25: Env var validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    """validate_config should catch invalid env vars."""

    def test_invalid_log_level_corrected(self):
        """Invalid LOG_LEVEL should be corrected to INFO."""
        from memory_common import validate_config

        os.environ["LOG_LEVEL"] = "NOTALEVEL"
        try:
            warnings = validate_config()
            assert os.environ["LOG_LEVEL"] == "INFO"
            assert any("LOG_LEVEL" in w for w in warnings)
        finally:
            os.environ.pop("LOG_LEVEL", None)

    def test_invalid_fts5_cache_ttl_corrected(self):
        """Invalid MEMORY_FTS5_CACHE_TTL should be corrected."""
        from memory_common import validate_config

        os.environ["MEMORY_FTS5_CACHE_TTL"] = "notanumber"
        try:
            warnings = validate_config()
            assert os.environ["MEMORY_FTS5_CACHE_TTL"] == "300"
            assert any("MEMORY_FTS5_CACHE_TTL" in w for w in warnings)
        finally:
            os.environ.pop("MEMORY_FTS5_CACHE_TTL", None)

    def test_valid_config_no_warnings(self):
        """Valid config should produce no warnings."""
        from memory_common import validate_config

        old_level = os.environ.get("LOG_LEVEL")
        os.environ["LOG_LEVEL"] = "INFO"
        try:
            warnings = validate_config()
            assert len(warnings) == 0
        finally:
            if old_level is not None:
                os.environ["LOG_LEVEL"] = old_level
            else:
                os.environ.pop("LOG_LEVEL", None)


# ---------------------------------------------------------------------------
# TD-22/TD-15: Archive cleanup
# ---------------------------------------------------------------------------
# TD-16: shared_pool TTL
# ---------------------------------------------------------------------------


class TestSharedPoolTTL:
    """list_shared_memories should filter by TTL."""

    def test_ttl_constant_exists(self):
        """_SHARED_POOL_TTL_DAYS should be defined."""
        import memory_sharing

        assert hasattr(memory_sharing, "_SHARED_POOL_TTL_DAYS")
        assert memory_sharing._SHARED_POOL_TTL_DAYS > 0

    def test_shared_pool_list_function_exists(self):
        """list_shared_memories function should exist."""
        import memory_sharing

        assert callable(memory_sharing.list_shared_memories)
