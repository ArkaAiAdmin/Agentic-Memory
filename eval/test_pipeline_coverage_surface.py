"""Tests for the pipeline_coverage MCP surface entry (Step 6 follow-up).

Verifies the memory_maintenance(operation="pipeline_coverage") path:
  - MaintenanceOp.PIPELINE_COVERAGE exists and is registered
  - handler runs cron/cron_pipeline_health.py and returns exit code + output
  - unknown kwargs are ignored (defensive)
"""

import sys
from pathlib import Path

_WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKTREE))

from mcp_surface.mcp_maintenance import MaintenanceOp
from mcp_surface.mcp_maintenance_ops import MAINTENANCE_HANDLERS, _run_pipeline_health


class TestPipelineCoverageSurface:
    def test_enum_exists(self):
        assert MaintenanceOp("pipeline_coverage") == MaintenanceOp.PIPELINE_COVERAGE

    def test_in_all_values(self):
        assert "pipeline_coverage" in MaintenanceOp.all_values()

    def test_handler_registered(self):
        assert MaintenanceOp.PIPELINE_COVERAGE in MAINTENANCE_HANDLERS

    def test_handler_is_callable(self):
        handler = MAINTENANCE_HANDLERS[MaintenanceOp.PIPELINE_COVERAGE]
        assert callable(handler)

    def test_handler_runs_and_returns_output(self, monkeypatch):
        import subprocess
        fake = subprocess.CompletedProcess(args=["dummy"], returncode=0, stdout="cron_pipeline_health ok\n", stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
        out = _run_pipeline_health(json_output=False)
        assert isinstance(out, str)
        assert "exit_code=0" in out

    def test_handler_ignores_unknown_kwargs(self, monkeypatch):
        import subprocess
        fake = subprocess.CompletedProcess(args=["dummy"], returncode=0, stdout="cron_pipeline_health ok\n", stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
        handler = MAINTENANCE_HANDLERS[MaintenanceOp.PIPELINE_COVERAGE]
        # unknown kwargs must not raise
        result = handler(not_a_real_kwarg="ignored")
        assert isinstance(result, str)

    def test_script_path_resolves(self):
        script = Path(__file__).resolve().parent.parent / "cron" / "cron_pipeline_health.py"
        assert script.exists(), f"script missing: {script}"
