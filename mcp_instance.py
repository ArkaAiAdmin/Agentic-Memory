"""Shared FastMCP instance for the agentic-memory MCP server.

Extracted to break circular import between memory_mcp ↔ mcp_tools.
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("AgenticMemory")
