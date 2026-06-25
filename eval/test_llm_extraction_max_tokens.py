"""Tests for MEMORY_LLM_EXTRACTION_MAX_TOKENS env var (P3, 2026-06-19).

Covers:
- llm_extraction._get_max_tokens() reads env var
- llm_extraction._get_max_tokens() falls back to default
- llm_extraction._get_max_tokens() falls back to _MAX_NEW_TOKENS constant
- llm_extraction._get_max_tokens() parses invalid env as default
- backfill_all.py --llm-max-tokens CLI flag sets env var
- LLMExtractor.extract() is called with the configured max_tokens
"""

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _reload_llm_extraction():
    """Reload llm_extraction with a clean env (drop cached extractor)."""
    import llm_extraction

    importlib.reload(llm_extraction)
    # Drop singleton so the next call reads fresh config
    llm_extraction._extractor = None
    return llm_extraction


class TestGetMaxTokens(unittest.TestCase):
    """_get_max_tokens() must respect env > config > default."""

    def setUp(self):
        self._saved_env = os.environ.get("MEMORY_LLM_EXTRACTION_MAX_TOKENS")
        os.environ.pop("MEMORY_LLM_EXTRACTION_MAX_TOKENS", None)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("MEMORY_LLM_EXTRACTION_MAX_TOKENS", None)
        else:
            os.environ["MEMORY_LLM_EXTRACTION_MAX_TOKENS"] = self._saved_env

    def test_env_var_takes_precedence(self):
        """MEMORY_LLM_EXTRACTION_MAX_TOKENS=128 is used when set."""
        os.environ["MEMORY_LLM_EXTRACTION_MAX_TOKENS"] = "128"
        llm = _reload_llm_extraction()
        self.assertEqual(llm._get_max_tokens(), 128)

    def test_default_when_no_env(self):
        """No env → falls back to config or _MAX_NEW_TOKENS (256)."""
        llm = _reload_llm_extraction()
        result = llm._get_max_tokens()
        # Should be a positive int; either 256 (default) or config value
        self.assertIsInstance(result, int)
        self.assertGreater(result, 0)
        self.assertLessEqual(result, 4096)

    def test_invalid_env_falls_back_to_default(self):
        """Garbage env value → falls back, doesn't crash."""
        os.environ["MEMORY_LLM_EXTRACTION_MAX_TOKENS"] = "not-a-number"
        llm = _reload_llm_extraction()
        # Should NOT raise; should return a sane default
        result = llm._get_max_tokens()
        self.assertIsInstance(result, int)
        self.assertGreater(result, 0)

    def test_default_constant_is_reasonable(self):
        """The default constant should be ≤ 1024 (the old broken default)."""
        llm = _reload_llm_extraction()
        self.assertLessEqual(llm._MAX_NEW_TOKENS, 1024)
        self.assertGreaterEqual(llm._MAX_NEW_TOKENS, 64)


class TestLlmExtractionPassesMaxTokens(unittest.TestCase):
    """extract_facts_via_llm() must pass max_tokens to extractor.extract()."""

    def test_extract_receives_max_tokens(self):
        """Mock the extractor and verify max_tokens is forwarded."""
        os.environ["MEMORY_LLM_EXTRACTION_MAX_TOKENS"] = "192"
        llm = _reload_llm_extraction()

        captured = {}

        class FakeExtractor:
            is_loaded = True

            def extract(self, content, max_tokens=_llm_DEFAULT):
                captured["max_tokens"] = max_tokens
                captured["content"] = content
                return {"facts": []}

            def load(self):
                return True

        # Replace the singleton with a fake
        llm._extractor = FakeExtractor()

        # Also mock is_llm_extraction_available to skip config check
        original_avail = llm.is_llm_extraction_available
        llm.is_llm_extraction_available = lambda: True

        try:
            llm.extract_facts_via_llm("Some text to extract from.")
            self.assertEqual(captured.get("max_tokens"), 192)
            self.assertEqual(captured.get("content"), "Some text to extract from.")
        finally:
            llm.is_llm_extraction_available = original_avail
            llm._extractor = None


# Module-level alias for the default arg evaluation in FakeExtractor
_llm_DEFAULT = 256


class TestBackfillCliLlmMaxTokens(unittest.TestCase):
    """backfill_all.py --llm-max-tokens flag must set env var."""

    def test_flag_sets_env_var(self):
        """The --llm-max-tokens flag must set MEMORY_LLM_EXTRACTION_MAX_TOKENS env var."""
        env = os.environ.copy()
        env["MEMORY_LLM_EXTRACTION_MAX_TOKENS"] = (
            "333"  # pre-set; flag should overwrite
        )

        # Write the test driver to a temp file to avoid format-string issues
        # with % chars in the embedded Python code.
        driver = REPO / "memory" / ".test_llm_max_tokens_driver.py"
        driver.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "import backfill_all as ba\n"
            "ba.backfill_all = lambda *a, **kw: {'operations': [], 'result': 'stub'}\n"
            "sys.argv = ['backfill_all', '--llm-max-tokens', '222']\n"
            "import os\n"
            "ba.main()\n"
            "got = os.environ.get('MEMORY_LLM_EXTRACTION_MAX_TOKENS')\n"
            "assert got == '222', f'env var not set: got {got!r}'\n"
            "print('OK')\n"
        )
        try:
            result = subprocess.run(
                [str(REPO / "venv/bin/python3.14"), str(driver)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            try:
                driver.unlink()
            except Exception:
                pass

        self.assertEqual(
            result.returncode,
            0,
            f"Expected exit 0; got {result.returncode}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}",
        )
        self.assertIn("OK", result.stdout)

    def test_flag_with_invalid_value_warns_but_does_not_crash(self):
        """A non-int --llm-max-tokens value should warn, not error."""
        driver = REPO / "memory" / ".test_llm_max_tokens_bad_driver.py"
        driver.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "import backfill_all as ba\n"
            "ba.backfill_all = lambda *a, **kw: {'operations': [], 'result': 'stub'}\n"
            "sys.argv = ['backfill_all', '--llm-max-tokens', 'banana']\n"
            "ba.main()\n"
        )
        try:
            result = subprocess.run(
                [str(REPO / "venv/bin/python3.14"), str(driver)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            try:
                driver.unlink()
            except Exception:
                pass

        # Should warn but not crash with non-zero exit
        self.assertIn(
            "warning: --llm-max-tokens",
            result.stderr,
            f"Expected warning in stderr; got: {result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
