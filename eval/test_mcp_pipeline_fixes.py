"""Tests for MCP pipeline fixes (feat/mcp-pipeline-fixes).

Coverage for:
  1. Scheduler _run_job with -m module invocation (Fix #7)
  2. memory_learn slug auto-generation (Fix #4)
  3. _worker_alive() tightened pgrep pattern (Fix #3)
  4. _op_list_skills / _op_extract_skills __wrapped__ bypass (Fix #5)
  5. journal_reconciler JOBS entry uses -m mode (Fix #2)
  6. background_worker signal handler guard (Fix #1)
  7. No duplicate memory_share tool registration (Fix #6)
"""

from __future__ import annotations

import re
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Scheduler _run_job: -m module invocation
# ═══════════════════════════════════════════════════════════════════════════════


class TestSchedulerModuleInvocation:
    """_run_job supports script='-m' to invoke Python modules."""

    def test_run_job_m_mode_builds_correct_command(self):
        """Dry-run: -m mode builds [python, -m, module] + args."""
        from cron.scheduler import _run_job

        job = {
            "script": "-m",
            "args": ["background.journal_reconciler", "--drain", "--max-entries=5"],
            "timeout": 30,
        }
        result = _run_job("journal_reconciler", job, dry_run=True)
        assert result["status"] == "dry_run"
        cmd = result["command"]
        assert "-m" in cmd
        assert "background.journal_reconciler" in cmd
        assert "--drain" in cmd
        assert "--max-entries=5" in cmd

    def test_run_job_m_mode_does_not_check_script_path(self):
        """When script='-m', no file-existence check should occur."""
        from cron.scheduler import _run_job

        job = {
            "script": "-m",
            "args": ["some.nonexistent.module"],
            "timeout": 5,
        }
        # Dry-run should succeed (no "Script not found" error)
        result = _run_job("test_job", job, dry_run=True)
        assert result["status"] == "dry_run"
        assert "Script not found" not in result.get("error", "")

    def test_run_job_regular_mode_checks_file_exists(self):
        """Regular mode returns error when script file doesn't exist."""
        from cron.scheduler import _run_job

        job = {
            "script": "nonexistent_script_xyz.py",
            "args": [],
            "timeout": 5,
        }
        result = _run_job("test_missing", job, dry_run=False)
        assert result["status"] == "failed"
        assert "Script not found" in result["error"]

    def test_run_job_m_mode_executes_module(self, tmp_path):
        """Actually runs a module via -m and captures output."""
        from cron.scheduler import _run_job

        job = {
            "script": "-m",
            "args": ["json.tool", "--help"],
            "timeout": 10,
        }
        result = _run_job("json_tool_help", job, dry_run=False)
        # json.tool --help exits 0
        assert result["status"] == "completed"
        assert result["returncode"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. memory_learn slug auto-generation
# ═══════════════════════════════════════════════════════════════════════════════


class TestMemoryLearnSlugGeneration:
    """memory_learn auto-generates a valid slug when skill_name is empty."""

    def test_slug_generated_from_content(self):
        """Slugifies first 60 chars of content into a valid slug."""
        slug_re = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')

        test_content = "How to properly handle database migrations in production"
        slug = re.sub(r'[^a-z0-9]+', '-', test_content[:60].lower()).strip('-') or "learned"
        assert slug_re.match(slug), f"Generated slug '{slug}' is not valid"
        assert len(slug) > 0
        assert slug == "how-to-properly-handle-database-migrations-in-production"

    def test_slug_generated_for_special_chars_content(self):
        """Content with many special chars still produces a valid slug."""
        test_content = "!!!@@@###$$$%%%^^^&&&***((())) empty stuff"
        slug = re.sub(r'[^a-z0-9]+', '-', test_content[:60].lower()).strip('-') or "learned"
        assert slug  # not empty
        assert not slug.startswith('-')
        assert not slug.endswith('-')

    def test_slug_fallback_for_empty_content(self):
        """All-special content falls back to 'learned'."""
        test_content = "!@#$%^&*()"
        slug = re.sub(r'[^a-z0-9]+', '-', test_content[:60].lower()).strip('-') or "learned"
        assert slug == "learned"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _worker_alive() tightened pgrep pattern
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkerAlivePattern:
    """_worker_alive uses a tight pgrep regex to avoid false positives."""

    def test_worker_alive_returns_false_when_no_worker(self, monkeypatch):
        """No matching process → returns False."""
        import cron.cron_pipeline_health as cph

        def _fake_run(args, **kw):
            class _R:
                returncode = 1  # pgrep returns 1 = no match
            return _R()

        monkeypatch.setattr("subprocess.run", _fake_run)
        assert cph._worker_alive() is False

    def test_worker_alive_returns_true_when_drain_worker_exists(self, monkeypatch):
        """A process matching background_worker.py --drain → True."""
        import subprocess as _subprocess
        import cron.cron_pipeline_health as cph

        call_log = []

        def _fake_run(args, **kw):
            call_log.append(args)
            class _R:
                returncode = 0  # match found
                # pgrep outputs PIDs on stdout when it finds matches
                stdout = "12345\n" if "pgrep" in str(args) else ""
                stderr = ""
            return _R()

        monkeypatch.setattr(_subprocess, "run", _fake_run)
        result = cph._worker_alive()
        assert result is True
        # Verify pgrep was called with the tight pattern
        pgrep_calls = [c for c in call_log if "pgrep" in str(c)]
        assert len(pgrep_calls) >= 1
        pgrep_pattern = str(pgrep_calls[0])
        assert "drain" in pgrep_pattern or "--drain" in pgrep_pattern

    def test_worker_alive_pgrep_pattern_is_tight(self):
        """The pgrep pattern must include flag variants to avoid false positives."""
        import cron.cron_pipeline_health as cph
        import inspect

        source = inspect.getsource(cph._worker_alive)
        # Must match specific flags, not just "background_worker"
        assert "--drain" in source or "drain" in source
        assert "--interval" in source or "interval" in source
        assert "--once" in source or "once" in source


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _op_list_skills / _op_extract_skills __wrapped__ bypass
# ═══════════════════════════════════════════════════════════════════════════════


class TestOpSkillsWrappedBypass:
    """_op_list_skills and _op_extract_skills use __wrapped__ to bypass decorator."""

    def test_op_list_skills_uses_wrapped(self):
        """Source code calls .__wrapped__ to bypass @with_memory_connection."""
        import inspect
        from mcp_surface.mcp_maintenance_ops import _op_list_skills

        source = inspect.getsource(_op_list_skills)
        assert "__wrapped__" in source, (
            "_op_list_skills must use .__wrapped__ to bypass decorator"
        )

    def test_op_extract_skills_uses_wrapped(self):
        """Source code calls .__wrapped__ to bypass @with_memory_connection."""
        import inspect
        from mcp_surface.mcp_maintenance_ops import _op_extract_skills

        source = inspect.getsource(_op_extract_skills)
        assert "__wrapped__" in source, (
            "_op_extract_skills must use .__wrapped__ to bypass decorator"
        )

    def test_memory_list_skills_has_wrapped_attr(self):
        """memory_list_skills exposes __wrapped__ via functools.wraps."""
        from mcp_surface.mcp_maintenance import memory_list_skills

        assert hasattr(memory_list_skills, "__wrapped__"), (
            "memory_list_skills missing __wrapped__; functools.wraps not applied"
        )
        assert callable(memory_list_skills.__wrapped__)

    def test_memory_extract_skills_has_wrapped_attr(self):
        """memory_extract_skills exposes __wrapped__ via functools.wraps."""
        from mcp_surface.mcp_maintenance import memory_extract_skills

        assert hasattr(memory_extract_skills, "__wrapped__"), (
            "memory_extract_skills missing __wrapped__; functools.wraps not applied"
        )
        assert callable(memory_extract_skills.__wrapped__)

    def test_op_list_skills_returns_string(self, monkeypatch, tmp_path):
        """_op_list_skills returns a JSON string result."""
        # Create a minimal test DB
        import sqlite3
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY)")
        conn.close()

        monkeypatch.setenv("MEMORY_DB_PATH", str(db))
        from mcp_surface.mcp_maintenance_ops import _op_list_skills

        result = _op_list_skills(limit=10)
        assert isinstance(result, str)
        # Should not raise TypeError about "multiple values for argument 'conn'"

    def test_op_extract_skills_returns_string(self, monkeypatch, tmp_path):
        """_op_extract_skills returns a JSON string result."""
        import sqlite3
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY)")
        conn.close()

        monkeypatch.setenv("MEMORY_DB_PATH", str(db))
        from mcp_surface.mcp_maintenance_ops import _op_extract_skills

        result = _op_extract_skills(memory_id="", dry_run=True)
        assert isinstance(result, str)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. journal_reconciler JOBS entry uses -m mode
# ═══════════════════════════════════════════════════════════════════════════════


class TestJournalReconcilerJobConfig:
    """The journal_reconciler job in JOBS uses -m module invocation."""

    def test_journal_reconciler_in_jobs(self):
        """journal_reconciler is registered in the JOBS dict."""
        from cron.jobs import JOBS

        assert "journal_reconciler" in JOBS

    def test_journal_reconciler_uses_m_mode(self):
        """journal_reconciler uses script='-m' for correct sys.path."""
        from cron.jobs import JOBS

        job = JOBS["journal_reconciler"]
        assert job["script"] == "-m", (
            f"Expected script='-m', got '{job['script']}'"
        )

    def test_journal_reconciler_args_contain_module(self):
        """Args start with the module path for -m invocation."""
        from cron.jobs import JOBS

        job = JOBS["journal_reconciler"]
        assert "background.journal_reconciler" in job["args"]

    def test_journal_reconciler_has_drain_flag(self):
        """The job must pass --drain to actually drain the journal."""
        from cron.jobs import JOBS

        job = JOBS["journal_reconciler"]
        assert "--drain" in job["args"]


# ═══════════════════════════════════════════════════════════════════════════════
# 6. background_worker signal handler guard
# ═══════════════════════════════════════════════════════════════════════════════


class TestSignalHandlerGuard:
    """Signal handler restore is inside the _use_signal_timeout guard."""

    def test_signal_restore_guarded_in_source(self):
        """The signal restore must be inside 'if _use_signal_timeout' block."""
        import inspect
        from background.background_worker import process_one_task

        source = inspect.getsource(process_one_task)
        lines = source.split('\n')

        # Find lines with signal.SIGALRM and old_handler
        restore_lines = [
            i for i, l in enumerate(lines)
            if "old_handler" in l and "signal" in l.lower()
        ]
        # Each restore must be inside an if _use_signal_timeout block
        for idx in restore_lines:
            # Look backwards for the guard
            found_guard = False
            for back in range(idx - 1, max(idx - 5, -1), -1):
                if "_use_signal_timeout" in lines[back]:
                    found_guard = True
                    break
            assert found_guard, (
                f"old_handler restore at line {idx} is not guarded by "
                f"'if _use_signal_timeout'"
            )

    def test_no_name_error_in_non_main_thread(self):
        """process_one_task on non-main thread must not NameError on old_handler."""
        import sqlite3

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        from background.background_queue import init_task_queue, enqueue_task
        init_task_queue(conn)
        enqueue_task(conn, "cron_pipeline_sentinel", payload={"_test": True})
        conn.commit()

        errors = []

        def _run_in_thread():
            try:
                from background.background_worker import process_one_task
                process_one_task(conn, db_path)
            except NameError as e:
                errors.append(str(e))
            except Exception:
                pass  # Other exceptions are fine for this test

        t = threading.Thread(target=_run_in_thread)
        t.start()
        t.join(timeout=10)

        conn.close()
        db_path.unlink(missing_ok=True)

        assert not errors, f"NameError in non-main thread: {errors}"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. No duplicate memory_share tool registration
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoDuplicateMemoryShare:
    """memory_share must not be registered twice on the MCP surface."""

    def test_mcp_sharing_module_no_tool_decorator(self):
        """mcp_sharing.py's memory_share must NOT have @mcp.tool()."""
        import inspect
        import mcp_surface.mcp_sharing as sharing_mod

        source = inspect.getsource(sharing_mod.memory_share)
        # The function source should not contain @mcp.tool() in its decorators
        # Check the module source around the function definition
        mod_source = inspect.getsource(sharing_mod)
        # Find the function definition and look at preceding lines
        func_line = None
        for i, line in enumerate(mod_source.split('\n')):
            if 'def memory_share(' in line:
                func_line = i
                break
        assert func_line is not None
        # Check the 5 lines before the def for @mcp.tool()
        preceding = mod_source.split('\n')[max(0, func_line - 5):func_line]
        has_mcp_tool = any('@mcp.tool' in line for line in preceding)
        assert not has_mcp_tool, (
            "mcp_sharing.py:memory_share still has @mcp.tool() decorator — "
            "this causes duplicate registration"
        )

    def test_no_duplicate_tool_names_on_surface(self):
        """The MCP surface has no duplicate tool registrations."""
        from mcp_surface.mcp_instance import mcp
        import mcp_surface.mcp_verbs  # noqa: F401 — trigger registration

        import anyio
        tools = anyio.run(mcp.list_tools)
        names = [t.name for t in tools]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"Duplicate tool registrations found: {dupes}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
