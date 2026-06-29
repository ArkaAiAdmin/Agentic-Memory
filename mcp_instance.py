"""Shared FastMCP instance for the agentic-memory MCP server.

Extracted to break circular import between memory_mcp ↔ mcp_tools.
"""
from mcp_common import _bootstrap_path  # noqa: E402
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("AgenticMemory")
