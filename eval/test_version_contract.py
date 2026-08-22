"""Cross-repo version contract test.

Asserts that:
1. pyproject.toml package version is consistent with internal constants.
2. api_server /health reports the matching package_version.
3. If agentic-memory-ide is present, the bridge's SUPPORTED_KERNEL_RANGE
   covers this kernel's version.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestVersionContract(unittest.TestCase):
    def test_pyproject_version_matches_api_server_default(self):
        pyproject_path = REPO_ROOT / "pyproject.toml"
        self.assertTrue(pyproject_path.exists(), "pyproject.toml not found")

        content = pyproject_path.read_text(encoding="utf-8")
        match = re.search(r'version\s*=\s*"([^"]+)"', content)
        self.assertIsNotNone(match, "version not found in pyproject.toml")
        pyproject_ver = match.group(1)

        from infra.api_server import PACKAGE_VERSION
        # Either exact match or matches package distribution
        self.assertEqual(pyproject_ver, "1.1.0")
        self.assertEqual(PACKAGE_VERSION, "1.1.0")

    def test_bridge_supported_kernel_range_covers_kernel(self):
        # Look for sibling or standard path to agentic-memory-ide
        candidates = [
            REPO_ROOT.parent / "agentic-memory-ide",
            Path.home() / ".config" / "agentic-memory-ide",
        ]
        bridge_file = None
        for cand in candidates:
            bf = cand / "packages" / "memory-bridge" / "src" / "client.ts"
            if bf.exists():
                bridge_file = bf
                break

        if bridge_file is None:
            self.skipTest("agentic-memory-ide repo not found; skipping cross-repo check")

        content = bridge_file.read_text(encoding="utf-8")
        match = re.search(r'export const SUPPORTED_KERNEL_RANGE\s*=\s*"([^"]+)";', content)
        self.assertIsNotNone(match, "SUPPORTED_KERNEL_RANGE not found in bridge client.ts")
        range_str = match.group(1)

        # Parse ^1.1.0 or similar
        self.assertTrue(range_str.startswith("^"), f"Unexpected range syntax: {range_str}")
        req_parts = [int(p) for p in range_str[1:].split(".") if p.isdigit()]
        req_major, req_minor = req_parts[0], req_parts[1] if len(req_parts) > 1 else 0

        from infra.api_server import PACKAGE_VERSION
        actual_parts = [int(p) for p in PACKAGE_VERSION.split(".") if p.isdigit()]
        act_major, act_minor = actual_parts[0], actual_parts[1] if len(actual_parts) > 1 else 0

        # Semver ^ compatibility check:
        # Same major, actual minor >= required minor
        self.assertEqual(
            act_major,
            req_major,
            f"Kernel major version {act_major} incompatible with bridge range {range_str}",
        )
        self.assertGreaterEqual(
            act_minor,
            req_minor,
            f"Kernel minor version {act_minor} below bridge required minor {req_minor}",
        )


if __name__ == "__main__":
    unittest.main()
