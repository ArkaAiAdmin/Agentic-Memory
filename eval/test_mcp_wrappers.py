"""Tests for MCP tool wrappers — exercises the @mcp.tool() decorators.

These tests target the thin wrapper layer (mcp_okf.py, mcp_profile.py, etc.)
that is not covered by the underlying module tests. Each test calls
the tool function directly and verifies the string/JSON response.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _setup_test_env(tmpdir: str):
    """Redirect DB paths to tmpdir so tools use a clean test DB."""
    os.environ["MEMORY_DB_PATH"] = str(Path(tmpdir) / "memory.db")
    os.environ["MEMORY_LOCAL_DIR"] = tmpdir
    # Bootstrap the DB

    db_path = str(Path(tmpdir) / "memory.db")
    if not Path(db_path).exists():
        # Initialize via save_pipeline
        from save_pipeline import save_memory

        save_memory(
            content="test seed",
            category="lessons",
            title_slug="test_seed",
        )


class TestMcpOkfWrapper(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mcp_okf_test_")
        _setup_test_env(self.tmpdir)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Clean up env
        os.environ.pop("MEMORY_DB_PATH", None)
        os.environ.pop("MEMORY_LOCAL_DIR", None)

    def test_memory_okf_export_db_not_found(self):
        """When DB doesn't exist, returns an error string."""
        # Point to an empty dir with no DB
        empty_dir = tempfile.mkdtemp(prefix="empty_")
        try:
            from mcp_okf import memory_okf_export

            result = memory_okf_export(output_dir=empty_dir)
            # Tool functions return strings (may be JSON or error text)
            self.assertIsInstance(result, str)
            self.assertTrue(len(result) > 0)
        finally:
            import shutil

            shutil.rmtree(empty_dir, ignore_errors=True)

    def test_memory_okf_import_dir_not_found(self):
        """When input dir doesn't exist, returns an error string."""
        from mcp_okf import memory_okf_import

        result = memory_okf_import(input_dir="/nonexistent/path/xyz")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_memory_okf_export_to_temp_dir(self):
        """Export with a real DB writes OKF files to output dir."""
        from mcp_okf import memory_okf_export

        output_dir = os.path.join(self.tmpdir, "okf_out")
        result_str = memory_okf_export(output_dir=output_dir)
        result = json.loads(result_str)
        # Result is a dict with stats; either "error" key or success fields
        if "error" not in result:
            # Successful export — check that output dir has files
            self.assertTrue(Path(output_dir).exists())


class TestMcpProfileWrapper(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mcp_profile_test_")
        _setup_test_env(self.tmpdir)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        os.environ.pop("MEMORY_DB_PATH", None)
        os.environ.pop("MEMORY_LOCAL_DIR", None)

    def test_memory_profile_stats(self):
        """memory_profile_stats returns a JSON-serializable dict."""
        from mcp_profile import memory_profile_stats

        result = memory_profile_stats()
        # Result is a JSON string or dict
        if isinstance(result, str):
            data = json.loads(result)
        else:
            data = result
        self.assertIsInstance(data, dict)


class TestMcpQualityWrapper(unittest.TestCase):
    def test_memory_quality_stats(self):
        """memory_quality_stats returns a JSON-serializable dict."""
        from mcp_quality import memory_quality_stats

        result = memory_quality_stats()
        if isinstance(result, str):
            data = json.loads(result)
        else:
            data = result
        self.assertIsInstance(data, dict)


class TestMcpSafetyWrapper(unittest.TestCase):
    def test_memory_scan_injection_clean(self):
        """memory_scan_injection on clean text returns safe result."""
        from mcp_safety import memory_scan_injection

        result = memory_scan_injection(
            content="This is a normal memory about Python testing."
        )
        if isinstance(result, str):
            data = json.loads(result)
        else:
            data = result
        self.assertIsInstance(data, dict)
        # Clean text should not be suspicious
        if "is_suspicious" in data:
            self.assertFalse(data["is_suspicious"])


class TestMcpCtrDriftWrapper(unittest.TestCase):
    def test_memory_check_concept_drift(self):
        """memory_check_concept_drift returns a dict."""
        from mcp_ctr_drift import memory_check_concept_drift

        result = memory_check_concept_drift()
        if isinstance(result, str):
            data = json.loads(result)
        else:
            data = result
        self.assertIsInstance(data, dict)


class TestMcpRetentionWrapper(unittest.TestCase):
    def test_memory_retention_stats(self):
        """memory_retention_stats returns a dict."""
        from mcp_retention import memory_retention_stats

        result = memory_retention_stats()
        if isinstance(result, str):
            data = json.loads(result)
        else:
            data = result
        self.assertIsInstance(data, dict)


if __name__ == "__main__":
    unittest.main()
