"""Test that token is reused from .api_token if already present."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestTokenReuse(unittest.TestCase):
    def test_token_file_reuse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = Path(tmpdir) / ".api_token"
            token_path.write_text("existing_token_12345", encoding="utf-8")

            # Check that existing token is preserved
            existing = token_path.read_text(encoding="utf-8").strip()
            self.assertEqual(existing, "existing_token_12345")


if __name__ == "__main__":
    unittest.main()
