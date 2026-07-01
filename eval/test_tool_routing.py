"""Tests for Tool routing layer — MCP dispatch, tool registry, skill routing.

Tests CORE_TOOLS vs ADMIN_TOOLS separation, tool registry completeness,
and the memory_maintenance routing gateway.
"""

import os
import sys
import unittest

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
sys.path.insert(0, os.getcwd())


class TestToolRegistry(unittest.TestCase):
    def test_core_tools_are_strings(self):
        from tool_registry import CORE_TOOLS

        self.assertIsInstance(CORE_TOOLS, list)
        self.assertGreater(len(CORE_TOOLS), 0)
        for tool in CORE_TOOLS:
            self.assertIsInstance(tool, str)

    def test_admin_tools_are_strings(self):
        from tool_registry import ADMIN_TOOLS

        self.assertIsInstance(ADMIN_TOOLS, list)
        self.assertGreater(len(ADMIN_TOOLS), 0)
        for tool in ADMIN_TOOLS:
            self.assertIsInstance(tool, str)

    def test_no_duplicates_between_core_and_admin(self):
        from tool_registry import CORE_TOOLS, ADMIN_TOOLS

        core_set = set(CORE_TOOLS)
        admin_set = set(ADMIN_TOOLS)
        overlap = core_set & admin_set
        self.assertEqual(len(overlap), 0, f"Tools in both CORE and ADMIN: {overlap}")

    def test_core_tools_have_key_operations(self):
        from tool_registry import CORE_TOOLS

        expected = [
            "memory_save",
            "memory_search",
            "memory_delete",
            "memory_session_start",
        ]
        for tool in expected:
            self.assertIn(tool, CORE_TOOLS, f"Missing core tool: {tool}")

    def test_admin_tools_include_maintenance(self):
        from tool_registry import ADMIN_TOOLS

        self.assertIn("memory_maintenance", ADMIN_TOOLS)


class TestMCPToolRegistration(unittest.TestCase):
    def test_all_core_tools_registered_as_mcp_functions(self):
        from tool_registry import CORE_TOOLS
        import mcp_tools

        for tool in CORE_TOOLS:
            self.assertTrue(
                hasattr(mcp_tools, tool), f"CORE tool {tool} not found in mcp_tools"
            )

    def test_skill_tools_registered(self):
        import mcp_tools

        self.assertTrue(
            hasattr(mcp_tools, "memory_search"), "memory_search must be registered"
        )
        self.assertTrue(
            hasattr(mcp_tools, "memory_save"), "memory_save must be registered"
        )
        self.assertTrue(
            hasattr(mcp_tools, "memory_rebuild"), "memory_rebuild must be registered"
        )


class TestSkillRouting(unittest.TestCase):
    def test_search_memories_accepts_skill_first(self):
        from search_pipeline import search_memories
        import inspect

        sig = inspect.signature(search_memories)
        params = list(sig.parameters.keys())
        self.assertIn("skill_first", params)

    def test_save_memory_accepts_all_needed_params(self):
        from save_pipeline import save_memory
        import inspect

        sig = inspect.signature(save_memory)
        params = list(sig.parameters.keys())
        self.assertIn("content", params)
        self.assertIn("category", params)
        self.assertIn("title_slug", params)
        self.assertIn("db_path", params)


if __name__ == "__main__":
    unittest.main()
