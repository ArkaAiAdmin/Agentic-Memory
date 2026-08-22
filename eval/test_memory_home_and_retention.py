"""Tests for MEMORY_HOME cross-platform resolution and backup retention policy."""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from infra.memory_config import get_memory_home, get_global_memory_dir
from cron.cron_backup import enforce_backup_retention
from infra.api_server import _write_kernel_discovery, _remove_kernel_discovery


def test_get_memory_home_env_override():
    """Explicit MEMORY_HOME env var takes highest precedence."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"MEMORY_HOME": tmpdir}):
            home = get_memory_home()
            assert home == Path(tmpdir)


def test_get_memory_home_platform_fallbacks():
    """Verify platform-specific default directories."""
    # Darwin
    with patch.dict(os.environ, {}, clear=True):
        with patch("sys.platform", "darwin"):
            with patch("pathlib.Path.home", return_value=Path("/Users/test")):
                home = get_memory_home()
                assert home == Path("/Users/test/Library/Application Support/AgenticMemory")

    # Linux (with XDG_DATA_HOME)
    with patch.dict(os.environ, {"XDG_DATA_HOME": "/custom/xdg"}, clear=True):
        with patch("sys.platform", "linux"):
            home = get_memory_home()
            assert home == Path("/custom/xdg/AgenticMemory")

    # Linux (default ~/.local/share)
    with patch.dict(os.environ, {}, clear=True):
        with patch("sys.platform", "linux"):
            with patch("pathlib.Path.home", return_value=Path("/home/test")):
                home = get_memory_home()
                assert home == Path("/home/test/.local/share/AgenticMemory")


def test_get_global_memory_dir_resolution():
    """Verify global memory dir priority."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "memory.db").write_text("dummy")

        with patch.dict(os.environ, {"MEMORY_HOME": tmpdir}):
            global_dir = get_global_memory_dir()
            assert global_dir == data_dir


def test_enforce_backup_retention_policy():
    """Test N=5 or <= 14 days retention logic."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backup_dir = Path(tmpdir) / "backups"
        backup_dir.mkdir(parents=True)

        now = time.time()
        # Create 10 dummy backup files with varying mtimes
        # 3 recent (< 14 days)
        for i in range(3):
            f = backup_dir / f"memory-recent-{i}.db"
            f.write_text("x" * 1024)
            os.utime(f, (now - (i * 86400), now - (i * 86400)))

        # 7 old (> 20 days)
        for i in range(7):
            f = backup_dir / f"memory-old-{i}.db"
            f.write_text("x" * 1024)
            os.utime(f, (now - ((20 + i) * 86400), now - ((20 + i) * 86400)))

        res = enforce_backup_retention(backup_dir)
        # Should keep 5 backups: 3 recent + 2 newest of the old
        assert res["total_backups"] == 5
        assert res["removed"] == 5
        remaining_files = list(backup_dir.glob("memory-*.db"))
        assert len(remaining_files) == 5


def test_discovery_file_lifecycle():
    """Test atomic kernel.json write, mode 0600, and cleanup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"MEMORY_HOME": tmpdir}):
            disc_file = _write_kernel_discovery(port=54321, token="ami_test_tok", pid=99999)
            assert disc_file is not None
            assert disc_file.exists()
            assert disc_file.name == "kernel.json"

            # Check permissions on unix
            if sys.platform != "win32":
                assert oct(disc_file.stat().st_mode & 0o777) == oct(0o600)

            # Cleanup by matching PID
            _remove_kernel_discovery(pid=99999)
            assert not disc_file.exists()
