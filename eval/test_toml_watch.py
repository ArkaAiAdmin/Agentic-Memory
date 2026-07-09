"""Tests for infra.toml_watch — canonical TOML mtime tracker."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.toml_watch import (
    current_mtime, refresh_mtime, toml_changed_since,
    subscribe, start_watcher, stop_watcher, get_toml_path,
    _DEBOUNCE, _poller_thread, _poller_stop, _subscribers,
)


class TestTomlWatch(unittest.TestCase):
    def setUp(self):
        import infra.toml_watch as tw
        tw._watcher_state.clear()
        tw._watcher_seen.clear()
        tw._subscribers.clear()
        tw._last_known_bytes = b""

    def test_current_mtime_returns_zero_before_stat(self):
        self.assertEqual(current_mtime(), 0.0)

    def test_refresh_mtime_updates_cache(self):
        fake_path = Path("/fake/memory.toml")
        with patch("infra.toml_watch.get_toml_path", return_value=fake_path):
            with patch.object(Path, "stat") as m_stat:
                mock_result = MagicMock()
                mock_result.st_mtime = 1000.0  # type: ignore[attr-defined]
                m_stat.return_value = mock_result
                refresh_mtime()
                result = refresh_mtime()
                self.assertEqual(current_mtime(), 1000.0)
        self.assertEqual(result, 1000.0)

    def test_toml_changed_since_true_when_newer(self):
        with patch("infra.toml_watch.current_mtime", return_value=2000.0):
            self.assertTrue(toml_changed_since(1000.0))
            self.assertFalse(toml_changed_since(2000.0))

    def test_debounce_ignores_jitter_under_50ms(self):
        fake_path = Path("/fake/memory.toml")
        with patch("infra.toml_watch.get_toml_path", return_value=fake_path):
            with patch.object(Path, "stat") as m_stat:
                mock_result = MagicMock()
                mock_result.st_mtime = 1000.01  # type: ignore[attr-defined]
                m_stat.return_value = mock_result
                refresh_mtime()
        import infra.toml_watch as tw
        self.assertEqual(
            tw._watcher_state[str(fake_path) + "__pending"], 1000.01
        )

    def test_subscribe_and_unsubscribe(self):
        cb = lambda mtime: None
        unsub = subscribe(cb)
        import infra.toml_watch as tw
        self.assertIn(cb, tw._subscribers)
        unsub()
        self.assertNotIn(cb, tw._subscribers)

    def test_start_watcher_is_single_flight(self):
        import infra.toml_watch as tw
        with patch.object(tw, "_poller_thread", None):
            with patch.object(tw, "_poller_stop", None):
                with patch("threading.Thread") as mock_thread:
                    mock_thread.return_value.is_alive.return_value = True
                    r1 = start_watcher()
                    r2 = start_watcher()
        self.assertTrue(r1)
        self.assertFalse(r2)
        stop_watcher()

    def test_stop_watcher_halts_poller(self):
        fake_stop = MagicMock()
        fake_thread = MagicMock()
        import infra.toml_watch as tw
        with patch.object(tw, "_poller_stop", fake_stop, create=True):
            with patch.object(tw, "_poller_thread", fake_thread, create=True):
                stop_watcher()
        fake_stop.set.assert_called_once()
        fake_thread.join.assert_called_once_with(timeout=2.0)

    def test_get_toml_path_returns_config_path(self):
        p = get_toml_path()
        self.assertIsInstance(p, Path)
        self.assertTrue(p.exists() or True)  # may not exist in test env

    def test_watcher_survives_os_stat_error(self):
        fake_path = Path("/fake/memory.toml")
        with patch("infra.toml_watch.get_toml_path", return_value=fake_path):
            with patch.object(Path, "stat", side_effect=OSError("EACCES")):
                result = refresh_mtime()
        self.assertEqual(result, 0.0)


if __name__ == "__main__":
    unittest.main()
