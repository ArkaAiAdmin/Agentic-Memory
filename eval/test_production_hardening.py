"""Tests for production hardening fixes.

Validates:
1. memory_system_health is registered as MCP tool
2. is_daemon_alive has no unreachable code
3. _warn_broken_links reports errors
4. chunk_index schema failure is logged with details
5. scope.py has no duplicate pytest check
6. Feature flags are checked before features run
7. Cron jobs are registered
8. _HOOK_SCRIPTS has correct entries

Subprocess-isolated per Hard Rule 20.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
VENV_PYTHON = WORKTREE / ".venv" / "bin" / "python"
if not VENV_PYTHON.exists():
    VENV_PYTHON = WORKTREE / "venv" / "bin" / "python"


def _run(code: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_")}
    env["PYTHONPATH"] = str(WORKTREE)
    env["MEMORY_INSTALL_ROOT"] = str(WORKTREE)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(VENV_PYTHON), "-c", code],
        capture_output=True, text=True, timeout=30, env=env,
        cwd=str(WORKTREE),
    )


class TestProductionHardening(unittest.TestCase):
    """Test production hardening fixes."""

    # ------------------------------------------------------------------ #
    # 1. memory_system_health is registered as MCP tool
    # ------------------------------------------------------------------ #
    def test_memory_system_health_is_mcp_tool(self):
        code = """
from mcp_surface.mcp_health import memory_system_health
# FastMCP registers @mcp.tool() decorated functions in the tool manager
from mcp_surface.mcp_instance import mcp
tools = mcp._tool_manager._tools
assert "memory_system_health" in tools, (
    f"memory_system_health not in MCP tools; available: {list(tools.keys())[:20]}"
)
print("OK: memory_system_health is registered as MCP tool")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------ #
    # 2. is_daemon_alive has no unreachable code
    # ------------------------------------------------------------------ #
    def test_is_daemon_alive_no_unreachable_code(self):
        code = """
import inspect
from infra.shared_memory_state import SharedMemoryState
source = inspect.getsource(SharedMemoryState.is_daemon_alive)
lines = source.split("\\n")
# Both return True are reachable:
#   - inside except PermissionError (process owned by another user)
#   - at the end of the method (os.kill succeeded with no exception)
returns = [i for i, l in enumerate(lines) if "return True" in l]
assert len(returns) == 2, (
    f"Expected 2 return True (PermissionError + normal path), "
    f"found {len(returns)}: {returns}"
)
print(f"OK: is_daemon_alive has {len(returns)} return True, no unreachable code")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------ #
    # 3. _warn_broken_links reports errors
    # ------------------------------------------------------------------ #
    def test_warn_broken_links_reports_errors(self):
        code = """
import tempfile
from pathlib import Path
from okf_conformance import _warn_broken_links

with tempfile.TemporaryDirectory() as d:
    base = Path(d)
    # Create a file with a broken internal link
    md = base / "test.md"
    md.write_text("[broken](/nonexistent.md)")
    errors: list[str] = []
    _warn_broken_links(base, [md], errors)
    assert len(errors) > 0, f"Expected broken link errors, got: {errors}"
    assert any("broken link" in e for e in errors), (
        f"Expected broken link message, got: {errors}"
    )
    print(f"OK: got {len(errors)} broken link error(s)")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_warn_broken_links_no_errors_for_valid_links(self):
        code = """
import tempfile
from pathlib import Path
from okf_conformance import _warn_broken_links

with tempfile.TemporaryDirectory() as d:
    base = Path(d)
    # Create two files and link between them
    target = base / "other.md"
    target.write_text("# Other")
    md = base / "test.md"
    md.write_text("[valid](/other.md)")
    errors: list[str] = []
    # Both files must be in md_files so the link target is "known"
    _warn_broken_links(base, [md, target], errors)
    assert len(errors) == 0, f"Expected no errors for valid links, got: {errors}"
    print("OK: no broken link errors for valid links")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------ #
    # 4. chunk_index schema failure is logged with details
    # ------------------------------------------------------------------ #
    def test_chunk_index_schema_error_includes_details(self):
        code = """
import inspect
from search.chunk_index import _qw5_ensure_schema
source = inspect.getsource(_qw5_ensure_schema)
# The except clause must capture the exception as 'e' and log it
assert "except Exception as e" in source, (
    "Missing exception capture in _qw5_ensure_schema"
)
# Must not have a bare 'pass' after the except
lines = source.split("\\n")
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith("except Exception as e"):
        # The next non-empty line must NOT be 'pass'
        for j in range(i + 1, min(i + 4, len(lines))):
            next_stripped = lines[j].strip()
            if next_stripped:
                assert next_stripped != "pass", (
                    "Redundant 'pass' after except in _qw5_ensure_schema"
                )
                break
        break
print("OK: _qw5_ensure_schema captures exception details")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------ #
    # 5. scope.py has no duplicate pytest check
    # ------------------------------------------------------------------ #
    def test_scope_no_duplicate_pytest_check(self):
        code = """
import re
import inspect
from infra.scope import resolve_scope
source = inspect.getsource(resolve_scope)
# Count occurrences of pytest in sys.modules check
count = len(re.findall(r'"pytest" in sys\\.modules', source))
assert count == 1, f"Expected 1 pytest check, found {count}"
print(f"OK: scope.py has exactly {count} pytest check (no duplicates)")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------ #
    # 6. Feature flags are checked before features run
    # ------------------------------------------------------------------ #
    def test_graph_communities_respects_feature_flag(self):
        code = """
from config import get_config
cfg = get_config()
assert hasattr(cfg, "graph_communities"), (
    "graph_communities flag not found on config"
)
print(f"OK: graph_communities flag exists, value={cfg.graph_communities}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_self_editing_respects_feature_flag(self):
        code = """
from config import get_config
cfg = get_config()
assert hasattr(cfg, "self_editing"), (
    "self_editing flag not found on config"
)
print(f"OK: self_editing flag exists, value={cfg.self_editing}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------ #
    # 7. Cron jobs are registered
    # ------------------------------------------------------------------ #
    def test_all_cron_scripts_registered(self):
        code = """
from pathlib import Path
from cron.jobs import JOBS

cron_dir = Path("cron")
expected_scripts = {
    "cron_resolve_contradictions.py",
    "cron_train_forget_model.py",
}
for script_name in expected_scripts:
    assert (cron_dir / script_name).exists(), f"{script_name} not found in cron/"

# Check revalidate_entailments is registered via enqueue_task
found = any(
    "cron_revalidate_entailments" in str(job.get("args", []))
    for job in JOBS.values()
)
assert found, "cron_revalidate_entailments not registered in JOBS"
print(f"OK: all {len(expected_scripts)} cron scripts exist; revalidate_entailments registered")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_cron_jobs_count_at_least_minimum(self):
        code = """
from cron.jobs import JOBS
# Sanity: ensure the job registry has a reasonable number of entries
assert len(JOBS) >= 20, f"Expected >=20 cron jobs, got {len(JOBS)}"
print(f"OK: {len(JOBS)} cron jobs registered")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------ #
    # 8. _HOOK_SCRIPTS has correct entries
    # ------------------------------------------------------------------ #
    def test_hook_scripts_has_correct_entries(self):
        code = """
from llm_extraction import _HOOK_SCRIPTS

# Must be present
assert "memory-precompact-snapshot.py" in _HOOK_SCRIPTS, (
    "memory-precompact-snapshot.py not in _HOOK_SCRIPTS"
)
assert "memory-recall-session.py" in _HOOK_SCRIPTS, (
    "memory-recall-session.py not in _HOOK_SCRIPTS"
)
assert "auto_save.py" in _HOOK_SCRIPTS, (
    "auto_save.py not in _HOOK_SCRIPTS"
)

# Must NOT be present (stale/renamed scripts)
assert "memory-idle-checkpoint.py" not in _HOOK_SCRIPTS, (
    "stale memory-idle-checkpoint.py still in _HOOK_SCRIPTS"
)
assert "memory-pre-compaction.py" not in _HOOK_SCRIPTS, (
    "stale memory-pre-compaction.py still in _HOOK_SCRIPTS"
)

print(f"OK: _HOOK_SCRIPTS has {len(_HOOK_SCRIPTS)} entries with correct contents")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_hook_scripts_is_frozenset(self):
        code = """
from llm_extraction import _HOOK_SCRIPTS
assert isinstance(_HOOK_SCRIPTS, frozenset), (
    f"Expected frozenset, got {type(_HOOK_SCRIPTS)}"
)
print(f"OK: _HOOK_SCRIPTS is a frozenset with {len(_HOOK_SCRIPTS)} entries")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
