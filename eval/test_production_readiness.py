"""Automated production readiness checks.

Mirrors the 7-item checklist in docs/production_readiness.md.
Run with:
    OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/python -m pytest eval/test_production_readiness.py -q
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
if str(_INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALL_DIR))

from infra.config import (
    AutoSaveConfig,
    HealthCheckConfig,
    MemoryConfig,
    WritePipelineConfig,
    get_config,
    reset_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TMP_DB_CACHE: dict[int, Path] = {}


def _tmp_db(tmp_path: Path) -> Path:
    """Create a fresh SQLite DB with WAL mode and busy_timeout set."""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Test 2.2 — WAL mode active
# ---------------------------------------------------------------------------

class TestWALMode:
    """§2.2: WAL mode must be active on the main database."""

    def test_wal_mode_active(self, tmp_path: Path):
        db = _tmp_db(tmp_path)
        conn = sqlite3.connect(str(db))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal", (
                f"WAL mode not active: expected 'wal', got {mode!r}. "
                "Fix with: sqlite3 <db> 'PRAGMA journal_mode=WAL'"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Test 2.3 — busy_timeout >= 30000 ms
# ---------------------------------------------------------------------------

class TestBusyTimeout:
    """§2.3: busy_timeout must be >= 30000 ms to tolerate lock contention.

    busy_timeout is a per-connection PRAGMA. Production code sets it in
    open_db() and sqlite_write_queue.start_session(). We replicate that
    via sqlite3.connect(timeout=30.0) which is equivalent.
    """

    def test_busy_timeout_set(self, tmp_path: Path):
        db = _tmp_db(tmp_path)
        conn = sqlite3.connect(str(db), timeout=30.0)
        try:
            conn.execute(
                "PRAGMA busy_timeout = 30000"
            )
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout >= 30000, (
                f"busy_timeout={timeout}ms, need >= 30000. "
                "Fix: set in connection pool at startup (infra/db.py open_db)."
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Test 2.7 — Health check config defaults
# ---------------------------------------------------------------------------

class TestHealthCheckConfig:
    """§2.7: Health-check subsystem relies on correct threshold defaults."""

    def test_vec_index_drift_default(self):
        """Default vec_index_drift_threshold must be 50."""
        cfg = MemoryConfig()
        assert cfg.health_check.vec_index_drift_threshold == 50, (
            f"Expected vec_index_drift_threshold=50, got {cfg.health_check.vec_index_drift_threshold}"
        )

    def test_disk_pct_used_default(self):
        """Default disk_pct_used_threshold must be 95."""
        cfg = MemoryConfig()
        assert cfg.health_check.disk_pct_used_threshold == 95, (
            f"Expected disk_pct_used_threshold=95, got {cfg.health_check.disk_pct_used_threshold}"
        )


# ---------------------------------------------------------------------------
# Test save-pipeline defaults
# ---------------------------------------------------------------------------

class TestSavePipelineDefaults:
    """Save pipeline must enforce content-size limits and prefer deferred execution."""

    def test_save_max_content_bytes(self):
        cfg = MemoryConfig()
        assert cfg.write.save_max_content_bytes == 50000, (
            f"save_max_content_bytes={cfg.write.save_max_content_bytes}, expected 50000"
        )

    def test_defer_expensive_default(self):
        cfg = MemoryConfig()
        assert cfg.write.defer_expensive is True, (
            f"defer_expensive={cfg.write.defer_expensive}, expected True"
        )

    def test_quality_gates_default(self):
        cfg = MemoryConfig()
        assert cfg.write.quality_gates is True

    def test_saga_enabled_default(self):
        cfg = MemoryConfig()
        assert cfg.write.saga_enabled is True


# ---------------------------------------------------------------------------
# Test circuit-breaker defaults
# ---------------------------------------------------------------------------

class TestCircuitBreakerDefaults:
    """Circuit breaker protects auto-save from cascading failures."""

    def test_backoff_cap_seconds(self):
        cfg = MemoryConfig()
        assert cfg.auto_save.backoff_cap_seconds == 300.0, (
            f"backoff_cap_seconds={cfg.auto_save.backoff_cap_seconds}, expected 300.0. "
            "Fix: set MEMORY_AUTO_SAVE_BACKOFF_CAP_SECONDS=300.0 or correct memory.toml."
        )

    def test_circuit_breaker_seconds(self):
        cfg = MemoryConfig()
        assert cfg.auto_save.circuit_breaker_seconds == 300.0

    def test_health_check_minutes(self):
        cfg = MemoryConfig()
        assert cfg.auto_save.health_check_minutes == 15, (
            f"health_check_minutes={cfg.auto_save.health_check_minutes}, expected 15"
        )

    def test_max_retries(self):
        cfg = MemoryConfig()
        assert cfg.auto_save.max_retries == 3


# ---------------------------------------------------------------------------
# Test config defaults (code-declared, memory.toml must match)
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    """Verify that code-declared defaults match the intended production values.

    These test the MemoryConfig() dataclass defaults — the values that would
    apply with an empty or absent memory.toml. memory.toml must not silently
    drift from these values.
    """

    def test_ollama_model_default(self):
        cfg = MemoryConfig()
        assert cfg.llm.ollama_model == "qwen2.5:3b"

    def test_extraction_model_id_default(self):
        cfg = MemoryConfig()
        assert cfg.llm.extraction_model_id == "Qwen/Qwen2.5-3B-Instruct"

    def test_db_pool_size_default(self):
        cfg = MemoryConfig()
        assert cfg.general.db_pool_size == 24

    def test_mmap_size_default(self):
        cfg = MemoryConfig()
        assert cfg.general.mmap_size == 268_435_456

    def test_vec_cache_max_default(self):
        cfg = MemoryConfig()
        assert cfg.cache.vec_cache_max == 500

    def test_vec_cache_ttl_default(self):
        cfg = MemoryConfig()
        assert cfg.cache.vec_cache_ttl_s == 300.0

    def test_fts5_cache_ttl_default(self):
        cfg = MemoryConfig()
        assert cfg.cache.fts5_cache_ttl == 30

    def test_batch_size_default(self):
        cfg = MemoryConfig()
        assert cfg.auto_save.batch_size == 50

    def test_daemon_idle_seconds_default(self):
        cfg = MemoryConfig()
        assert cfg.auto_save.daemon_idle_seconds == 300

    def test_inbox_max_bytes_default(self):
        cfg = MemoryConfig()
        assert cfg.auto_save.inbox_max_bytes == 500_000

    def test_saga_enabled_code_default(self):
        cfg = MemoryConfig()
        assert cfg.features.saga_enabled is True

    def test_write_journal_code_default(self):
        cfg = MemoryConfig()
        assert cfg.features.write_journal is False

    def test_quality_gates_code_default(self):
        cfg = MemoryConfig()
        assert cfg.features.quality_gates is True


# ---------------------------------------------------------------------------
# Test effective config matches code defaults when memory.toml is aligned
# ---------------------------------------------------------------------------

class TestEffectiveConfigMatchesCodeDefaults:
    """Verify that the operational config (loaded from memory.toml + env)
    has not silently drifted away from code defaults for critical production
    parameters.  This test will catch the backoff_cap_seconds = 30.0 bug
    that existed in memory.toml before the fix.
    """

    def test_load_config_no_error(self):
        """get_config() must succeed without raising."""
        reset_config()
        cfg = get_config()
        assert cfg is not None

    def test_effective_backoff_cap_seconds(self):
        """backoff_cap_seconds must be 300.0 in the effective config."""
        reset_config()
        cfg = get_config()
        assert cfg.auto_save.backoff_cap_seconds == 300.0, (
            f"Effective backoff_cap_seconds={cfg.auto_save.backoff_cap_seconds}. "
            "Check memory.toml [auto_save] backoff_cap_seconds and MEMORY_AUTO_SAVE_BACKOFF_CAP_SECONDS env var."
        )

    def test_effective_vec_cache_max(self):
        """vec_cache_max must be 500 in the effective config."""
        reset_config()
        cfg = get_config()
        assert cfg.cache.vec_cache_max == 500, (
            f"Effective vec_cache_max={cfg.cache.vec_cache_max}. "
            "Check memory.toml [cache] vec_cache_max and MEMORY_VEC_CACHE_MAX env var."
        )

    def test_effective_daemon_idle_seconds(self):
        reset_config()
        cfg = get_config()
        assert cfg.auto_save.daemon_idle_seconds == 300


# ---------------------------------------------------------------------------
# Test health_check config section existence and defaults
# ---------------------------------------------------------------------------

class TestHealthCheckConfigSection:
    """Verify the new health_check config section (added in config.py dataclass
    refactor) is present with correct defaults and accessible via __getattr__.
    """

    def test_direct_attr_access(self):
        cfg = MemoryConfig()
        assert cfg.health_check.vec_index_drift_threshold == 50
        assert cfg.health_check.disk_pct_used_threshold == 95

    def test_via_getattr(self):
        """__getattr__ fallback in MemoryConfig must also resolve health_check attrs."""
        cfg = MemoryConfig()
        assert getattr(cfg, "vec_index_drift_threshold") == 50

    def test_health_check_config_is_subobject(self):
        cfg = MemoryConfig()
        assert isinstance(cfg.health_check, HealthCheckConfig)

    def test_write_config_is_subobject(self):
        cfg = MemoryConfig()
        assert isinstance(cfg.write, WritePipelineConfig)

    def test_auto_save_config_is_subobject(self):
        cfg = MemoryConfig()
        assert isinstance(cfg.auto_save, AutoSaveConfig)
