#!/usr/bin/env python3
"""Unit tests for opt-in transformer support in embedding search."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path.resolve(Path(__file__).parents[2])
sys.path.insert(0, str(INSTALL_DIR))

from infra.embedding_search import EmbeddingSearch

try:
    import torch  # noqa: F401
    import transformers  # noqa: F401
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False


@unittest.skipUnless(HAS_DEPS, "Requires optional torch and transformers libraries")
class TestTransformerOptIn(unittest.TestCase):
    def test_transformer_opt_in_loading(self):
        # Ensure sentence_transformers is available as a mock module
        # so the fallback path can be tested regardless of whether
        # sentence_transformers is actually installed.
        st_mock_module = mock.MagicMock()
        transformers_mock_module = mock.MagicMock()

        with mock.patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING_MODEL_ID": "sentence-transformers/all-MiniLM-L6-v2",
                "MEMORY_EMBEDDING_BACKEND": "auto",
            },
        ):
            with mock.patch.dict(
                "sys.modules",
                {
                    "sentence_transformers": st_mock_module,
                    "transformers": transformers_mock_module,
                },
            ):
                with mock.patch(
                    "sentence_transformers.SentenceTransformer",
                ) as mock_sentence_transformer:
                    # Force fallback to pure transformers path
                    mock_sentence_transformer.side_effect = ImportError(
                        "forcing fallback"
                    )

                    with mock.patch("transformers.AutoModel") as mock_auto_model:
                        with mock.patch(
                            "transformers.AutoTokenizer"
                        ) as mock_auto_tokenizer:
                            # Configure mock model configuration
                            mock_model_instance = mock.MagicMock()
                            mock_model_instance.config.hidden_size = 384
                            mock_auto_model.from_pretrained.return_value = (
                                mock_model_instance
                            )

                            # Instantiate EmbeddingSearch (falls back to pure transformers)
                            es = EmbeddingSearch()

                            # Wait for background thread to load the model
                            import time
                            deadline = time.monotonic() + 10.0
                            while time.monotonic() < deadline:
                                if es._model_loaded or es._model_load_failed:
                                    break
                                time.sleep(0.05)

                            # Verify it loaded the transformer model via fallback AutoModel
                            mock_auto_model.from_pretrained.assert_called_once()
                            mock_auto_tokenizer.from_pretrained.assert_called_once()
                            self.assertTrue(es.is_transformer)
                            self.assertEqual(es.model.dim, 384)

                            # Mock the forward pass output of the model
                            # outputs[0] is the token embeddings
                            import torch

                            fake_embeddings = torch.randn(1, 5, 384)
                            mock_model_instance.return_value = (fake_embeddings,)

                            # Mock tokenizer return value
                            mock_auto_tokenizer.from_pretrained.return_value.return_value = {
                                "input_ids": torch.tensor([[1, 2, 3, 4, 5]]),
                                "attention_mask": torch.tensor([[1, 1, 1, 1, 1]]),
                            }

                            # Verify encode method delegates correctly
                            texts = ["hello world"]
                            vecs = es.encode(texts)
                            self.assertEqual(vecs.shape, (1, 384))


if __name__ == "__main__":
    unittest.main()
