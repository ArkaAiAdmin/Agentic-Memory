"""Tests for cli.py — command dispatcher and entry points."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


class TestCliCommands(unittest.TestCase):
    """COMMANDS dict maps known names to callables."""

    def test_commands_dict_has_expected_keys(self):
        from cli import COMMANDS

        expected = {
            "server",
            "search",
            "rebuild",
            "backfill",
            "consolidate",
            "integrity",
            "tier",
            "compact",
            "bootstrap",
            "worker",
            "sync",
            "init",
            "doctor",
            "status",
            "dashboard",
        }
        self.assertEqual(set(COMMANDS), expected)

    def test_commands_callable(self):
        from cli import COMMANDS

        for name, fn in COMMANDS.items():
            with self.subTest(name=name):
                self.assertTrue(callable(fn), f"{name} not callable")

    def test_main_shows_help_for_unknown(self):
        from cli import main

        with patch.object(sys, "argv", ["cli.py", "nonexistent"]):
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, 1)

    def test_main_no_args_runs_server(self):
        from cli import main

        with patch.object(sys, "argv", ["cli.py"]):
            with patch(
                "cli.server_main",
                return_value=None,
            ) as mock_server:
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 0)
                mock_server.assert_called_once()


class TestRunHelper(unittest.TestCase):
    """_run dispatches to subprocess.run."""

    @patch("cli.subprocess.run")
    def test_run_uses_correct_python(self, mock_run):
        from cli import _run, PYTHON, SCRIPTS

        _run("test_script.py")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], PYTHON)
        self.assertTrue(str(SCRIPTS) in args[1] or "cron" in args[1])

    @patch("cli.subprocess.run")
    def test_run_with_args(self, mock_run):
        from cli import _run

        _run("test_script.py", ["--flag", "value"])
        args = mock_run.call_args[0][0]
        self.assertIn("--flag", args)
        self.assertIn("value", args)

    @patch("cli.os.path.exists")
    @patch("cli.subprocess.run")
    def test_run_falls_back_to_cron(self, mock_run, mock_exists):
        from cli import _run

        mock_exists.side_effect = lambda p: "cron" in str(p)
        _run("test_script.py")
        args = mock_run.call_args[0][0]
        self.assertIn("cron", str(args[1]))


class TestSpecificCommands(unittest.TestCase):
    """Individual command functions dispatch correctly."""

    @patch("cli._run")
    def test_search_no_query_exits(self, mock_run):
        from cli import search_main

        with patch.object(sys, "argv", ["cli.py", "search"]):
            with self.assertRaises(SystemExit) as ctx:
                search_main()
            self.assertEqual(ctx.exception.code, 1)
        mock_run.assert_not_called()

    @patch("cli._run")
    def test_search_with_query(self, mock_run):
        from cli import search_main

        with patch.object(sys, "argv", ["cli.py", "search", "hello"]):
            with self.assertRaises(SystemExit) as ctx:
                search_main()
            self.assertEqual(ctx.exception.code, 0)
        mock_run.assert_called_once()

    @patch("cli._run")
    def test_backfill(self, mock_run):
        from cli import backfill_main

        backfill_main()
        mock_run.assert_called_once_with("backfill_all.py", ["--full"])

    @patch("cli._run")
    def test_consolidate(self, mock_run):
        from cli import consolidate_main

        consolidate_main()
        mock_run.assert_called_once_with("cron_consolidate.py")

    @patch("cli._run")
    def test_integrity(self, mock_run):
        from cli import integrity_main

        integrity_main()
        mock_run.assert_called_once_with("memory_integrity.py")

    @patch("cli._run")
    def test_tier(self, mock_run):
        from cli import tier_main

        tier_main()
        mock_run.assert_called_once_with("cron_tier_migration.py", ["--once"])

    @patch("cli._run")
    def test_compact(self, mock_run):
        from cli import compact_main

        compact_main()
        mock_run.assert_called_once_with("cron_compact.py")

    @patch("cli.subprocess.run")
    @patch("cli.os.path.exists", return_value=True)
    def test_rebuild(self, mock_exists, mock_run):
        import cli

        cli._run = mock_run
        from cli import rebuild_main

        with patch("infrastructure.resolve_active_memory_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/memory")
            rebuild_main()
            mock_run.assert_called_once()


class TestBootstrapMain(unittest.TestCase):
    @patch("cli._run")
    def test_bootstrap_unix(self, mock_run):
        from cli import bootstrap_main

        with patch("sys.platform", "linux"):
            bootstrap_main()
        mock_run.assert_called_once_with("setup_memory.sh")

    @patch("cli._run")
    def test_bootstrap_non_unix(self, mock_run):
        from cli import bootstrap_main

        with patch("sys.platform", "win32"):
            bootstrap_main()
        mock_run.assert_not_called()


class TestWorkerMain(unittest.TestCase):
    @patch("cli._run")
    def test_worker_drain(self, mock_run):
        from cli import worker_main

        with patch.object(sys, "argv", ["cli.py", "worker", "--drain"]):
            worker_main()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][1]
        self.assertIn("--drain", args)

    @patch("cli._run")
    def test_worker_once(self, mock_run):
        from cli import worker_main

        with patch.object(sys, "argv", ["cli.py", "worker", "--once"]):
            worker_main()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][1]
        self.assertIn("--once", args)

    @patch("cli._run")
    def test_worker_default_drain(self, mock_run):
        from cli import worker_main

        with patch.object(sys, "argv", ["cli.py", "worker"]):
            worker_main()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][1]
        self.assertIn("--drain", args)


class _FakeSyncClient:
    """Module-like object with sync_once attribute."""

    def __init__(self, return_value):
        self.sync_once = lambda **kwargs: return_value


class TestSyncMain(unittest.TestCase):
    def test_sync_success(self):
        from cli import sync_main

        fake = _FakeSyncClient({"success": True, "push": 5, "pull": 3})
        with patch.object(
            sys, "argv", ["cli.py", "sync", "--peer", "http://test:9877"]
        ):
            with patch.dict(sys.modules, {"sync_client": fake}):
                result = sync_main()
        self.assertEqual(result, 0)

    def test_sync_error(self):
        from cli import sync_main

        fake = _FakeSyncClient({"error": "Connection refused", "success": False})
        with patch.object(
            sys, "argv", ["cli.py", "sync", "--peer", "http://test:9877"]
        ):
            with patch.dict(sys.modules, {"sync_client": fake}):
                result = sync_main()
        self.assertEqual(result, 1)

    def test_sync_failure_no_error(self):
        from cli import sync_main

        fake = _FakeSyncClient({"success": False, "push": 0, "pull": 0})
        with patch.object(
            sys, "argv", ["cli.py", "sync", "--peer", "http://test:9877"]
        ):
            with patch.dict(sys.modules, {"sync_client": fake}):
                result = sync_main()
        self.assertEqual(result, 2)
