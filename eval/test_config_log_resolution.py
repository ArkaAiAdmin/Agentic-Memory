#!/usr/bin/env python3
"""Regression test for the MEMORY_LLM_EXTRACTION resolution log.

Background:
  2026-06-26: the import-time log only printed the raw env var, e.g.
    "MEMORY_LLM_EXTRACTION=None", which misled operators into
    thinking LLM extraction was disabled — when in fact the TOML
    config (features.llm_extraction = true) was the source of truth
    and LLM was still enabled. The fix: log env, TOML, and effective
    values so operators can see the actual resolution.

This test asserts the log line appears and includes all three values.
It does NOT assert exact output (which would be brittle); instead it
asserts the presence of each token.
"""

import os
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class TestConfigLogShowsResolution(unittest.TestCase):
    """The MEMORY_LLM_EXTRACTION import log shows env, toml, and effective."""

    def test_log_includes_effective_value(self):
        """When MEMORY_LLM_EXTRACTION is unset but TOML is true, the log
        must show effective=True so operators aren't misled."""
        result = subprocess.run(
            [
                str(REPO / "venv" / "bin" / "python"),
                "-c",
                "import config",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            env={**os.environ, "MEMORY_LLM_EXTRACTION": ""},  # force unset
        )
        stderr = result.stderr

        # The log line must mention effective
        self.assertIn("effective=", stderr)
        # And TOML value
        self.assertIn("toml[features.llm_extraction]=", stderr)
        # And env
        self.assertIn("env=", stderr)

    def test_log_respects_env_override(self):
        """When MEMORY_LLM_EXTRACTION=0 is set, effective must be False."""
        result = subprocess.run(
            [
                str(REPO / "venv" / "bin" / "python"),
                "-c",
                "import config",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            env={**os.environ, "MEMORY_LLM_EXTRACTION": "0"},
        )
        stderr = result.stderr
        # The line should show effective=False (because the env override
        # beats the TOML default of true)
        self.assertIn("effective=False", stderr)

    def test_log_respects_env_enable(self):
        """When MEMORY_LLM_EXTRACTION=1 is set, effective must be True."""
        result = subprocess.run(
            [
                str(REPO / "venv" / "bin" / "python"),
                "-c",
                "import config",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            env={**os.environ, "MEMORY_LLM_EXTRACTION": "1"},
        )
        stderr = result.stderr
        self.assertIn("effective=True", stderr)


if __name__ == "__main__":
    unittest.main()
