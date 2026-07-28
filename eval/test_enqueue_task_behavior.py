"""Behavior tests for Phase B cron consolidation: enqueue_task.py + worker run_script handler.

These tests verify observable behaviour (DB state, stdout, exit codes)
rather than internal implementation details (mock.call_count, internal
cache state, etc.).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval._fixtures import bootstrap_temp_db_clean  # noqa: E402


def _make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _env_for(db_path: Path) -> dict:
    return {
        **os.environ,
        "MEMORY_DB_PATH": str(db_path),
        "MEMORY_ASYNC_AUTOSAVE": "0",
        "AUTO_SAVE_TOOL_ALLOWLIST": "*",
    }


def _reset_autosave_state():
    from background.circuit_breaker import _auto_save_reset_state
    from infra.db import connection_pool

    _auto_save_reset_state()
    connection_pool._pool.clear()
    connection_pool._pooled_ids.clear()
    connection_pool._migrated.clear()


class TestEnqueueTaskBehavior(TestCase):
    """enqueue_task.py: verify DB state and stdout, not internal mocks."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.mem_dir = self.tmpdir / "memory"
        self.mem_dir.mkdir()
        self.db_path = self.mem_dir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        _reset_autosave_state()
        self.venv_py = str(Path(sys.executable).resolve())
        self.repo_root = str(Path(__file__).resolve().parent.parent)

    def tearDown(self) -> None:
        _reset_autosave_state()
        for f in self.tmpdir.glob("*"):
            try:
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
            except Exception:
                pass
        try:
            self.tmpdir.rmdir()
        except OSError:
            pass

    def _run_enqueue(self, *args) -> subprocess.CompletedProcess:
        script = str(Path(self.repo_root) / "cron" / "enqueue_task.py")
        env = _env_for(self.db_path)
        cmd = [self.venv_py, script, *args]
        return subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=30
        )

    def test_enqueue_creates_pending_task(self) -> None:
        result = self._run_enqueue(
            "--task-type", "cron_cleanup_auto_logs",
            "--payload", '{"args": ["--max-age-days", "30"]}',
        )
        assert result.returncode == 0, result.stderr
        assert "enqueued task_id=" in result.stdout
        task_id = int(result.stdout.split("task_id=")[1].split()[0])
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT task_type, status FROM task_queue WHERE id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row is not None, "task not found in DB"
        assert row[0] == "cron_cleanup_auto_logs"
        assert row[1] == "pending"

    def test_enqueue_with_max_queue_size_zero_always_enqueues(self) -> None:
        # Even if queue is full, max_queue_size=0 disables the cap.
        result = self._run_enqueue(
            "--task-type", "cron_cleanup_auto_logs",
            "--max-queue-size", "0",
        )
        assert result.returncode == 0
        assert "enqueued task_id=" in result.stdout

    def test_payload_file_argument(self) -> None:
        payload_file = self.tmpdir / "payload.json"
        payload_file.write_text(json.dumps({"args": ["--dry-run"]}))
        result = self._run_enqueue(
            "--task-type", "cron_consolidate",
            "--payload", f"@{payload_file}",
        )
        assert result.returncode == 0
        assert "enqueued task_id=" in result.stdout

    def test_missing_db_exits_with_error(self) -> None:
        missing_db = self.tmpdir / "does_not_exist.db"
        env = _env_for(missing_db)
        script = str(Path(self.repo_root) / "cron" / "enqueue_task.py")
        result = subprocess.run(
            [self.venv_py, script, "--task-type", "cron_cleanup_auto_logs"],
            capture_output=True, text=True, env=env, timeout=10
        )
        assert result.returncode != 0
        assert "database not found" in result.stderr.lower()

    def test_invalid_json_payload_exits_with_error(self) -> None:
        result = self._run_enqueue(
            "--task-type", "cron_cleanup_auto_logs",
            "--payload", "not-json",
        )
        assert result.returncode != 0
        assert "invalid json" in result.stderr.lower()


class TestDebounceBehavior(TestCase):
    """Debounce logic: skip enqueue if same task_type completed recently."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.mem_dir = self.tmpdir / "memory"
        self.mem_dir.mkdir()
        self.db_path = self.mem_dir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        _reset_autosave_state()
        self.venv_py = str(Path(sys.executable).resolve())
        self.repo_root = str(Path(__file__).resolve().parent.parent)

    def tearDown(self) -> None:
        _reset_autosave_state()
        for f in self.tmpdir.glob("*"):
            try:
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
            except Exception:
                pass
        try:
            self.tmpdir.rmdir()
        except OSError:
            pass

    def _run_enqueue(self, *args) -> subprocess.CompletedProcess:
        script = str(Path(self.repo_root) / "cron" / "enqueue_task.py")
        env = _env_for(self.db_path)
        cmd = [self.venv_py, script, *args]
        return subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=30
        )

    def _complete_task(self, task_type: str) -> int:
        """Enqueue and immediately process a task so it ends up completed."""
        result = self._run_enqueue("--task-type", task_type)
        assert result.returncode == 0, result.stderr
        task_id = int(result.stdout.split("task_id=")[1].split()[0])
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "UPDATE task_queue SET status='completed', completed_at=datetime('now') "
            "WHERE id=?",
            (task_id,),
        )
        conn.commit()
        conn.close()
        return task_id

    def test_debounce_skips_recent_completion(self) -> None:
        self._complete_task("cron_cleanup_auto_logs")
        result = self._run_enqueue(
            "--task-type", "cron_cleanup_auto_logs",
            "--debounce-seconds", "3600",
        )
        assert result.returncode == 0
        assert "skipped: debounce" in result.stdout

    def test_debounce_allows_after_window(self) -> None:
        task_id = self._complete_task("cron_cleanup_auto_logs")
        # Artificially age the completed_at timestamp by 2 hours
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "UPDATE task_queue SET completed_at=datetime('now', '-2 hours') "
            "WHERE id=?",
            (task_id,),
        )
        conn.commit()
        conn.close()
        result = self._run_enqueue(
            "--task-type", "cron_cleanup_auto_logs",
            "--debounce-seconds", "3600",
        )
        assert result.returncode == 0
        assert "enqueued task_id=" in result.stdout

    def test_no_debounce_when_flag_zero(self) -> None:
        self._complete_task("cron_cleanup_auto_logs")
        result = self._run_enqueue(
            "--task-type", "cron_cleanup_auto_logs",
            "--debounce-seconds", "0",
        )
        assert result.returncode == 0
        assert "enqueued task_id=" in result.stdout


class TestWorkerRunScriptHandler(TestCase):
    """Worker's run_script handler executes cron scripts via subprocess."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.mem_dir = self.tmpdir / "memory"
        self.mem_dir.mkdir()
        self.db_path = self.mem_dir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        _reset_autosave_state()
        self.venv_py = str(Path(sys.executable).resolve())
        self.repo_root = str(Path(__file__).resolve().parent.parent)

    def tearDown(self) -> None:
        _reset_autosave_state()
        for f in self.tmpdir.glob("*"):
            try:
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
            except Exception:
                pass
        try:
            self.tmpdir.rmdir()
        except OSError:
            pass

    def _enqueue_and_process(self, task_type: str, payload: dict):
        from background.background_worker import process_one_task
        from background.background_queue import init_task_queue, enqueue_task
        from infra.memory_common import connection_pool

        conn = connection_pool.get(str(self.db_path), timeout=10)
        init_task_queue(conn)
        task_id = enqueue_task(conn, task_type, payload=payload)
        result = process_one_task(conn, Path(self.db_path))
        conn.close()
        return task_id, result

    def test_run_script_executes_cron_script(self) -> None:
        """A cron-style task_type resolves via CRON_SCRIPT_MAP and runs."""
        task_id, processed = self._enqueue_and_process(
            "cron_cleanup_auto_logs",
            {},
        )
        assert processed is True
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT status, error FROM task_queue WHERE id=?",
            (task_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "completed", f"task failed: {row[1]}"

    def test_run_script_passes_args(self) -> None:
        """--once flag is passed through to the cron script."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ok"
        mock_result.stderr = ""

        with patch("background.background_worker.subprocess.run", return_value=mock_result) as mock_run:
            task_id, processed = self._enqueue_and_process(
                "cron_embedding_recompute",
                {"args": ["--once"]},
            )
            assert processed is True
            # Verify --once was passed through to the subprocess
            assert mock_run.called
            cmd_args = mock_run.call_args[0][0]
            assert "--once" in cmd_args

        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT status FROM task_queue WHERE id=?",
            (task_id,),
        ).fetchone()
        conn.close()
        assert row[0] == "completed"

    def test_run_script_fails_on_missing_script(self) -> None:
        """Explicit script path that doesn't exist fails the task."""
        from infra.memory_common import connection_pool
        from background.background_queue import init_task_queue, enqueue_task

        conn = connection_pool.get(str(self.db_path), timeout=10)
        init_task_queue(conn)
        unknown_script = "cron/does_not_exist.py"
        task_id = enqueue_task(
            conn, "run_script",
            payload={"script": unknown_script},
        )
        result = conn.execute(
            "SELECT status FROM task_queue WHERE id=?", (task_id,)
        ).fetchone()
        conn.close()
        # task should be marked failed after processing
        from background.background_worker import process_one_task
        from infra.memory_common import connection_pool as cp2

        conn2 = cp2.get(str(self.db_path), timeout=10)
        for _ in range(3):
            process_one_task(conn2, Path(self.db_path))
        row = conn2.execute(
            "SELECT status, error FROM task_queue WHERE id=?",
            (task_id,),
        ).fetchone()
        conn2.close()
        assert row[0] == "failed"
        assert "not found" in (row[1] or "")

    def test_cron_script_map_resolution(self) -> None:
        """task_type not in HANDLERS but in CRON_SCRIPT_MAP routes to run_script."""
        from background.background_worker import CRON_SCRIPT_MAP

        assert "cron_consolidate" in CRON_SCRIPT_MAP
        assert CRON_SCRIPT_MAP["cron_consolidate"] == "cron/cron_consolidate.py"

    def test_worker_completes_enqueued_cron_task(self) -> None:
        """End-to-end: enqueue and process via worker in-process."""
        from background.background_queue import init_task_queue, enqueue_task
        from infra.memory_common import connection_pool

        conn = connection_pool.get(str(self.db_path), timeout=10)
        init_task_queue(conn)
        task_id = enqueue_task(
            conn, "cron_cleanup_auto_logs", payload={"args": ["--max-age-days", "30"]}
        )
        conn.close()

        from background.background_worker import process_one_task

        conn2 = connection_pool.get(str(self.db_path), timeout=10)
        result = process_one_task(conn2, Path(self.db_path))
        conn2.close()
        assert result is True
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT status FROM task_queue WHERE id=?",
            (task_id,),
        ).fetchone()
        conn.close()
        assert row[0] == "completed", f"task status: {row}"
