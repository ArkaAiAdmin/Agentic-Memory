from __future__ import annotations
"""
MCP tool registry — auto-discovery + dynamic re-export.

Modules are auto-discovered via glob for @mcp.tool() registration side
effects.  All ``memory_*`` functions + known helpers are dynamically
re-exported so ``from mcp_tools import memory_search`` keeps working.
Adding or removing an ``mcp_*.py`` module requires no changes here.
"""

import importlib
import sys
from pathlib import Path as _Path

# Must be imported first for bootstrap side effects
from mcp_common import _bootstrap_path  # noqa: E402,F401

_tools_dir = _Path(__file__).parent
_skip = {"mcp_tools", "mcp_instance"}

# Names that don't follow the memory_* convention but must be re-exported
_extra_exports = frozenset({
    "_run_subprocess_output",
    "recompile_skills_catalog",
    "search_memories",
})

# Collect all mcp_ module names (sorted so import order is deterministic)
_all_mcp_names: list[str] = []
for _path in sorted(_tools_dir.glob("mcp_*.py")):
    _name = _path.stem
    if _name not in _skip:
        _all_mcp_names.append(_name)

# Module-level __getattr__: handles circular imports where an mcp_ module
# (e.g. mcp_async) tries ``from mcp_tools import memory_save`` before
# Phase 2 completes or before the provider module is imported.
# Falls back to scanning (and on-demand importing) already-imported modules.
def __getattr__(name: str):
    for _mod_name in _all_mcp_names:
        _mod = sys.modules.get(_mod_name)
        if _mod is None:
            try:
                _mod = importlib.import_module(_mod_name)
            except Exception:
                continue
        if hasattr(_mod, name):
            _val = getattr(_mod, name)
            globals()[name] = _val
            return _val
    raise AttributeError(f"module 'mcp_tools' has no attribute '{name}'")

def __dir__():
    return __all__

# Phase 1: import all modules for @mcp.tool() registration side effects
for _name in _all_mcp_names:
    importlib.import_module(_name)

# Phase 2: populate globals() from imported modules (faster than __getattr__
# for subsequent lookups, and required for ``from mcp_tools import *``).
for _name in _all_mcp_names:
    _mod = sys.modules.get(_name)
    if _mod is None:
        continue
    for _attr_name in dir(_mod):
        if _attr_name.startswith("memory_") or _attr_name in _extra_exports:
            globals()[_attr_name] = getattr(_mod, _attr_name)

__all__ = sorted(
    k for k in globals()
    if k.startswith("memory_") or k in _extra_exports
)
