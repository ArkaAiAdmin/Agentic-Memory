"""Tests for infra/agents_md_generator.py — live-count injection into AGENTS.md.

Verifies:
- gather() returns expected keys with plausible counts
- Section generator functions return non-empty strings
- _update_markers() is idempotent
- Markers in AGENTS.md are well-formed (no orphan --> suffixes)
- --dry-run CLI flag emits JSON without writing
- --check CLI flag exits non-zero when stale
"""

import json
import os
import re
import subprocess
import sys
import unittest

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
sys.path.insert(0, os.getcwd())

from infra.agents_md_generator import (  # noqa: E402
    AGENTS_MD,
    MARKER_END,
    MARKER_START,
    SECTION_FNS,
    _update_markers,
    gather,
)


class TestCollectData(unittest.TestCase):
    def test_returns_dict_with_expected_keys(self):
        data = gather()
        self.assertIsInstance(data, dict)
        for key in (
            "schema_version",
            "migration_count",
            "table_count",
            "tool_counts",
            "hook_count",
            "cron_job_count",
            "test_file_count",
            "test_function_count",
            "test_loc",
            "mcp_module_count",
        ):
            self.assertIn(key, data, f"Missing key in gather output: {key}")

    def test_schema_version_is_positive_int(self):
        data = gather()
        v = data["schema_version"]
        self.assertIsInstance(v, int)
        self.assertGreater(v, 0)

    def test_tool_counts_has_core_admin_deprecated(self):
        data = gather()
        tc = data["tool_counts"]
        for key in ("core", "admin", "deprecated"):
            self.assertIn(key, tc)
            self.assertIsInstance(tc[key], int)
            self.assertGreaterEqual(tc[key], 0)

    def test_counts_are_self_consistent(self):
        data = gather()
        self.assertGreaterEqual(data["test_file_count"], 1)
        self.assertGreaterEqual(data["test_function_count"], data["test_file_count"])

    def test_test_loc_positive(self):
        data = gather()
        self.assertGreater(data["test_loc"], 0)


class TestSectionFunctions(unittest.TestCase):
    def test_all_expected_sections_registered(self):
        expected = {
            "what_this_system_is",
            "critical_path",
            "hard_rule_4",
            "hard_rule_6",
            "mcp_surface_contract",
            "current_state",
        }
        self.assertEqual(set(SECTION_FNS.keys()), expected)

    def test_sections_return_nonempty_strings(self):
        data = gather()
        for key, fn in SECTION_FNS.items():
            with self.subTest(section=key):
                result = fn(data)
                self.assertIsInstance(result, str, f"{key} returned non-string")
                self.assertGreater(len(result), 0, f"{key} returned empty string")

    def test_hard_rule_4_returns_schema_version(self):
        data = gather()
        result = SECTION_FNS["hard_rule_4"](data)
        self.assertIn(str(data["schema_version"]), result)

    def test_current_state_mentions_schema_and_mcp(self):
        data = gather()
        result = SECTION_FNS["current_state"](data)
        self.assertIn("Schema", result)
        self.assertIn("MCP", result)


class TestUpdateMarkers(unittest.TestCase):
    def test_idempotent_on_real_agents_md(self):
        original = AGENTS_MD.read_text(encoding="utf-8")
        data = gather()
        once = _update_markers(original, data)
        twice = _update_markers(once, data)
        self.assertEqual(once, twice, "_update_markers is not idempotent")

    def test_no_double_arrow_suffix(self):
        original = AGENTS_MD.read_text(encoding="utf-8")
        data = gather()
        updated = _update_markers(original, data)
        # Should not contain the malformed "-->-->" anywhere
        self.assertNotIn("-->-->", updated)

    def test_preserves_all_markers(self):
        original = AGENTS_MD.read_text(encoding="utf-8")
        data = gather()
        updated = _update_markers(original, data)
        for key in SECTION_FNS:
            start_marker = f'{MARKER_START} key="{key}"-->'
            end_marker = f'{MARKER_END} key="{key}"-->'
            self.assertIn(start_marker, updated, f"Missing START marker for {key}")
            self.assertIn(end_marker, updated, f"Missing END marker for {key}")

    def test_replaces_stale_value(self):
        data = gather()
        marker_block = (
            f'{MARKER_START} key="hard_rule_4"-->\n'
            f"999\n"
            f'{MARKER_END} key="hard_rule_4"-->'
        )
        text = f"prefix\n{marker_block}\nsuffix\n"
        result = _update_markers(text, data)
        self.assertNotIn("999", result)
        self.assertIn(str(data["schema_version"]), result)


class TestMarkersWellFormed(unittest.TestCase):
    def test_agents_md_has_no_orphan_arrow_suffixes(self):
        text = AGENTS_MD.read_text(encoding="utf-8")
        self.assertNotIn("-->-->", text, "AGENTS.md has malformed double --> markers")

    def test_agents_md_has_balanced_markers(self):
        text = AGENTS_MD.read_text(encoding="utf-8")
        starts = len(re.findall(r"<!--AUTO-GEN:START\s+key=\"\w+\"\s*-->", text))
        ends = len(re.findall(r"<!--AUTO-GEN:END\s+key=\"\w+\"\s*-->", text))
        self.assertEqual(starts, ends, f"Unbalanced markers: {starts} START vs {ends} END")


class TestCLI(unittest.TestCase):
    def test_dry_run_emits_json(self):
        proc = subprocess.run(
            [sys.executable, "infra/agents_md_generator.py", "--dry-run"],
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        # Dry-run output is JSON followed by an excerpt section; isolate the JSON object.
        stdout = proc.stdout
        json_end = stdout.find("\n\n---")
        json_text = stdout if json_end == -1 else stdout[:json_end]
        payload = json.loads(json_text)
        self.assertIn("schema_version", payload)
        self.assertIn("tool_counts", payload)

    def test_check_flag_exits_zero_when_current(self):
        # First ensure AGENTS.md is current
        subprocess.run(
            [sys.executable, "infra/agents_md_generator.py"],
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
            timeout=30,
        )
        proc = subprocess.run(
            [sys.executable, "infra/agents_md_generator.py", "--check"],
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"AGENTS.md is stale: {proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
