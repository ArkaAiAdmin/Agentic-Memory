#!/usr/bin/env python3
"""Unit tests for LLM abstractive summarization."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from summarization import is_llm_summarization_available, summarize_text_via_llm, summarize_text


class TestLLMSummarization(unittest.TestCase):
    @patch.dict(os.environ, {"MEMORY_LLM_SUMMARIZATION": "0", "MEMORY_LLM_EXTRACTION": "0"})
    @patch("config.get_config")
    def test_llm_summarization_disabled_by_default(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.llm_summarization = False
        mock_get_config.return_value = mock_cfg
        
        self.assertFalse(is_llm_summarization_available())

    @patch.dict(os.environ, {"MEMORY_LLM_SUMMARIZATION": "1"})
    @patch("fact.llm_providers.get_provider")
    def test_llm_summarization_enabled_via_env(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_get_provider.return_value = mock_provider
        
        self.assertTrue(is_llm_summarization_available())

    @patch.dict(os.environ, {"MEMORY_LLM_SUMMARIZATION": "1"})
    @patch("fact.llm_providers.get_provider")
    def test_summarize_text_via_llm_success(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.generate.return_value = "This is a great abstractive summary."
        mock_get_provider.return_value = mock_provider

        summary = summarize_text_via_llm("Some very long text to summarize " * 30)
        self.assertEqual(summary, "This is a great abstractive summary.")

    @patch.dict(os.environ, {"MEMORY_LLM_SUMMARIZATION": "1"})
    @patch("fact.llm_providers.get_provider")
    def test_summarize_text_falls_back_to_tfidf_on_failure(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.generate.return_value = None  # LLM failure
        mock_get_provider.return_value = mock_provider

        long_text = (
            "First sentence of the text is here. "
            "Second sentence of the text is also here. "
            "Third sentence is right here. "
            "Fourth sentence is here. "
            "Fifth sentence is here. "
            "Sixth sentence is here. "
        ) * 5
        # Ensure it has enough length to trigger summarization
        self.assertGreater(len(long_text), 500)

        # Should fall back to extractive summary
        summary = summarize_text(long_text)
        self.assertIsNotNone(summary)
        self.assertNotEqual(summary, "")
        # Since it fell back to TF-IDF, it should contain some original sentences
        self.assertIn("sentence", summary.lower())


if __name__ == "__main__":
    unittest.main()
