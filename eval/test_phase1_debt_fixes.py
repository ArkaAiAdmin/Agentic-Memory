#!/usr/bin/env python3
"""Phase 1 validation tests for tech-debt fixes (TD-06, TD-19, TD-17).

Covers:
- TD-06: datetime.utcnow() replaced with datetime.now(timezone.utc)
- TD-19: Stale M4 fix comment removed from arc_cache.py
- TD-17: Logging variable names standardized to 'logger'
"""
import datetime
import sys
import pathlib


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# TD-06: datetime.utcnow() → datetime.now(timezone.utc)
# ---------------------------------------------------------------------------

class TestDatetimeUtcnowFix:
    """memory_delete._now_iso and _now_dt must use timezone-aware UTC."""

    def test_now_iso_returns_aware_utc(self):
        from memory_delete import _now_iso
        ts = _now_iso()
        dt = datetime.datetime.fromisoformat(ts)
        # Must be timezone-aware (not naive)
        assert dt.tzinfo is not None, f"_now_iso() returned naive datetime: {ts}"
        # Must be UTC
        assert dt.tzinfo == datetime.timezone.utc, (
            f"_now_iso() tzinfo is {dt.tzinfo}, expected UTC"
        )

    def test_now_dt_returns_aware_utc(self):
        from memory_delete import _now_dt
        dt = _now_dt()
        assert dt.tzinfo is not None, "_now_dt() returned naive datetime"
        assert dt.tzinfo == datetime.timezone.utc

    def test_no_utcnow_calls_in_memory_delete(self):
        """Ensure utcnow() is fully removed from memory_delete.py."""
        src_path = pathlib.Path(__file__).resolve().parent.parent / "memory_delete.py"
        src = src_path.read_text()
        # Allow utcnow in comments/docstrings but not in code
        lines = src.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            assert "utcnow()" not in stripped, (
                f"memory_delete.py:{i} still uses utcnow(): {stripped}"
            )

    def test_no_utcnow_in_auto_save(self):
        """auto_save._now_iso uses datetime.datetime.now() — verify no utcnow."""
        src_path = pathlib.Path(__file__).resolve().parent.parent / "auto_save.py"
        src = src_path.read_text()
        lines = src.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            assert "utcnow()" not in stripped, (
                f"auto_save.py:{i} still uses utcnow(): {stripped}"
            )


# ---------------------------------------------------------------------------
# TD-19: Stale M4 fix comment removed
# ---------------------------------------------------------------------------

class TestM4CommentRemoved:
    """arc_cache.py must not contain the stale M4 fix comment."""

    def test_m4_comment_removed(self):
        src_path = pathlib.Path(__file__).resolve().parent.parent / "infra" / "arc_cache.py"
        src = src_path.read_text()
        assert "M4 fix" not in src, "arc_cache.py still contains stale 'M4 fix' comment"

    def test_find_project_root_import_preserved(self):
        """The actual import must still be there — only the comment was removed."""
        src_path = pathlib.Path(__file__).resolve().parent.parent / "infra" / "arc_cache.py"
        src = src_path.read_text()
        assert "from memory_common import find_project_root" in src


# ---------------------------------------------------------------------------
# TD-17: Logging variable names standardized
# ---------------------------------------------------------------------------

class TestLoggingStandardization:
    """All modules should use 'logger' as the logging variable name."""

    def test_reranker_uses_logger(self):
        src_path = pathlib.Path(__file__).resolve().parent.parent / "infra" / "reranker.py"
        src = src_path.read_text()
        assert "logger = logging.getLogger(__name__)" in src
        # Ensure no 'log = logging.getLogger' remains
        lines = src.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not stripped.startswith("log = logging.getLogger"), (
                f"reranker.py:{i} still uses 'log' instead of 'logger'"
            )

    def test_audit_uses_logger(self):
        src_path = pathlib.Path(__file__).resolve().parent.parent / "infra" / "audit.py"
        src = src_path.read_text()
        assert "logger = logging.getLogger(__name__)" in src
        # Ensure no _AUDIT_LOGGER remains
        assert "_AUDIT_LOGGER" not in src, "audit.py still references _AUDIT_LOGGER"

    def test_reranker_logger_references_work(self):
        """Verify reranker module can be imported and logger is accessible."""
        import infra.reranker
        assert hasattr(reranker, "logger")
        assert reranker.logger.name == "infra.reranker"

    def test_audit_logger_references_work(self):
        """Verify audit module can be imported and logger is accessible."""
        import infra.audit
        assert hasattr(audit, "logger")
        assert audit.logger.name == "infra.audit"
