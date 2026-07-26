#!/usr/bin/env python3
"""Generate docs/reference/mcp-tools.md from tool_registry.py + MCP source files.

Usage:
    python scripts/gen_mcp_tools_doc.py          # write in-place
    python scripts/gen_mcp_tools_doc.py --check   # exit 1 on mismatch
    python scripts/gen_mcp_tools_doc.py --stdout  # print to stdout
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _docgen_markers import assemble, extract_manual  # noqa: E402

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
MCP_PATTERNS = ["mcp_*.py", "mcp_surface/mcp_*.py", "agentic_memory/*.py", "memory_mcp.py"]

# ---------------------------------------------------------------------------
# Tool extraction from source
# ---------------------------------------------------------------------------


def _find_tool_file(tool_name: str) -> tuple[Optional[Path], Optional[str], Optional[str]]:
    """Search MCP files for a tool function definition.

    Returns (file_path, docstring, full_source) or (None, None, None).
    Handles direct defs, aliases (foo = bar), and lambda router handlers.
    """
    # 1. Direct def / async def
    for pattern in MCP_PATTERNS:
        for fpath in sorted(ROOT.glob(pattern)):
            try:
                text = fpath.read_text(encoding="utf-8")
            except Exception:
                continue
            # Look for def <tool_name>( ... ) or async def <tool_name>(
            idx = text.find(f"def {tool_name}(")
            if idx == -1:
                idx = text.find(f"async def {tool_name}(")
            if idx == -1:
                continue
            doc = _extract_docstring_at(text, idx)
            if doc is not None:
                return fpath, doc, text
            # Found def but no docstring
            return fpath, "", text

    # 2. Alias: <tool_name> = <other_name> — follow the assignment
    for pattern in MCP_PATTERNS:
        for fpath in sorted(ROOT.glob(pattern)):
            try:
                text = fpath.read_text(encoding="utf-8")
            except Exception:
                continue
            alias_pat = rf"^{tool_name}\s*=\s*(\w+)"
            for m in re.finditer(alias_pat, text, re.MULTILINE):
                target = m.group(1)
                # Strip common wrapper suffix like _context
                for candidate in (target, target.replace("_context", ""), target.replace("_stats", "")):
                    res = _find_tool_file(candidate)
                    if res[1]:
                        return fpath, res[1], text
                # If target itself had no docstring, return empty
                if res[0]:
                    return fpath, "", text

    # 3. Lambda router handler: MaintenanceOp.NAME_UPPER: lambda ... -> <handler_func>(
    upper = tool_name.upper().replace("MEMORY_", "", 1) if tool_name.startswith("memory_") else tool_name.upper()
    # Handle recall_stats -> RECALL_STATS (not RECALL_STATS_STATS)
    if upper.endswith("_STATS") and not upper.endswith("_STATS_STATS"):
        upper = upper[:-5]
    for pattern in MCP_PATTERNS:
        for fpath in sorted(ROOT.glob(pattern)):
            try:
                text = fpath.read_text(encoding="utf-8")
            except Exception:
                continue
            # Match: MaintenanceOp.NAME: lambda ... -> _handler_func(...)
            # or: MaintenanceOp.NAME: lambda ...: _handler_func(...)
            m = re.search(
                rf"MaintenanceOp\.{re.escape(upper)}\s*:\s*lambda.*?(?:->\s*)?(\w+)\(",
                text,
                re.DOTALL,
            )
            if m:
                handler_name = m.group(1)
                for candidate in (handler_name,):
                    res = _find_tool_file(candidate)
                    if res[1]:
                        return fpath, res[1], text
                if res[0]:
                    return fpath, "", text

    return None, None, None


def _extract_docstring_at(text: str, sig_idx: int) -> Optional[str]:
    """Extract the first paragraph of a docstring immediately after a function sig."""
    # Find closing paren by tracking nesting
    rest = text[sig_idx:]
    depth = 0
    sig_end = sig_idx
    for ci, c in enumerate(rest):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                sig_end = sig_idx + ci + 1
                break
    if depth != 0:
        return None
    after_sig = text[sig_end:]
    doc_match = re.search(
        r'(?::\s*(?:->\s*.+?)?\s*)?(?:"""|\'\'\')(.+?)(?:"""|\'\'\')',
        after_sig,
        re.DOTALL,
    )
    if doc_match:
        doc = doc_match.group(1).strip()
        return doc.split("\n\n")[0].strip()
    return None


def _extract_params(tool_name: str, source: str) -> list[dict]:
    """Extract function parameters from source using AST."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == tool_name:
            params = []
            for arg in node.args.args:
                if arg.arg == "self":
                    continue
                param = {"name": arg.arg, "type": "", "default": ""}
                if arg.annotation:
                    try:
                        param["type"] = ast.dump(arg.annotation)
                        # Simplify type annotation
                        if isinstance(arg.annotation, ast.Name):
                            param["type"] = arg.annotation.id
                        elif isinstance(arg.annotation, ast.Subscript):
                            param["type"] = ast.unparse(arg.annotation)
                        elif isinstance(arg.annotation, ast.Attribute):
                            param["type"] = ast.unparse(arg.annotation)
                        elif isinstance(arg.annotation, ast.BinOp):
                            param["type"] = ast.unparse(arg.annotation)
                        else:
                            param["type"] = ast.unparse(arg.annotation)
                    except Exception:
                        param["type"] = "unknown"
                params.append(param)
            # Map defaults to params (right-aligned)
            defaults = node.args.defaults
            for i, default in enumerate(defaults):
                idx = len(params) - len(defaults) + i
                if 0 <= idx < len(params):
                    try:
                        params[idx]["default"] = ast.unparse(default)
                    except Exception:
                        params[idx]["default"] = "..."
            return params
    return []


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------


def _generate_core_entry(tool_name: str, doc: str, file_path: Path) -> str:
    parts = [f"### `{tool_name}`", "", doc + "\n"]
    source_ref = f"*(Source: `{file_path.relative_to(ROOT)}`)*"
    parts.append(source_ref)
    parts.append("")
    parts.append("---")
    parts.append("")
    return "\n".join(parts)


def _generate_admin_table(
    tools: list[tuple[str, str, Optional[Path]]],
) -> str:
    rows = ["| Tool | Purpose |", "|------|---------|"]
    for tool_name, doc, fpath in sorted(tools, key=lambda x: x[0]):
        row_doc = doc.split("\n")[0] if doc else "*(no description)*"
        rows.append(f"| `{tool_name}` | {row_doc} |")
    return "\n".join(rows)


def generate_doc() -> str:
    from tool_registry import CORE_TOOLS, ADMIN_TOOLS, DEPRECATED

    # Build a set of all tool names
    deprecated_set = set(DEPRECATED)

    core_entries: list[str] = []
    admin_tools: list[tuple[str, str, Optional[Path]]] = []

    # Process CORE tools - full entries
    for tool_name in CORE_TOOLS:
        fpath, doc, source = _find_tool_file(tool_name)
        if not doc:
            doc = "*(no description found in source)*"
        core_entries.append(_generate_core_entry(tool_name, doc, fpath) if fpath else "")

    # Process ADMIN tools - compact table
    for tool_name in ADMIN_TOOLS:
        fpath, doc, _ = _find_tool_file(tool_name)
        if not doc:
            doc = ""
        admin_tools.append((tool_name, doc, fpath))

    # Counts
    n_core = len(CORE_TOOLS)
    n_admin = len(ADMIN_TOOLS)
    n_dep = len(deprecated_set)
    n_total = n_core + n_admin + n_dep

    header = (
        f"# MCP Tools Reference\n\n"
        f"Agentic Memory exposes **{n_core} CORE + {n_admin} ADMIN + {n_dep} DEPRECATED = "
        f"{n_total} total registered names**. "
        f"The single source of truth for the tool surface is `tool_registry.py` "
        f"(`CORE_TOOLS`, `ADMIN_TOOLS`, and `DEPRECATED` lists).\n\n"
        f"*This file is generated by `scripts/gen_mcp_tools_doc.py`. "
        f"Run it to sync from live code.*\n"
    )

    core_section = (
        f"## Core Tools ({n_core})\n\n"
        f"The {n_core} tools most agents use day-to-day. Each is a first-class MCP "
        f"function; no grouping required.\n\n" + "\n".join(core_entries)
    )

    admin_section = (
        f"## Admin Tools ({n_admin})\n\n"
        f"All admin operations go through the `memory_maintenance` grouped "
        f"tool, dispatched by `operation=`. The full list (single source of "
        f"truth: `tool_registry.ADMIN_TOOLS`):\n\n" + _generate_admin_table(admin_tools)
    )

    footer = "\n---\n*This file is generated by `scripts/gen_mcp_tools_doc.py`. "
    footer += "Do not edit directly; run the script and review the diff.*\n"

    return f"{header}\n\n{core_section}\n\n{admin_section}\n{footer}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    core = generate_doc()
    target = ROOT / "docs/reference/mcp-tools.md"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    manual = extract_manual(existing)
    doc = assemble(core, manual)

    if "--stdout" in sys.argv:
        print(doc)
        return 0

    if "--check" in sys.argv:
        if existing.strip() == doc.strip():
            print("✅ docs/reference/mcp-tools.md is in sync with live code.")
            return 0
        print("❌ docs/reference/mcp-tools.md has drifted from live code.")
        print("   Run: python scripts/gen_mcp_tools_doc.py")
        # Print diff
        existing_lines = existing.splitlines()
        new_lines = doc.splitlines()
        for i, (a, b) in enumerate(zip(existing_lines, new_lines)):
            if a != b:
                print(f"  Line {i+1}:")
                print(f"    - {a}")
                print(f"    + {b}")
                if i > 20:
                    print("  ... (truncated)")
                    break
        if len(existing_lines) != len(new_lines):
            print(f"  Line count: existing={len(existing_lines)} new={len(new_lines)}")
        return 1

    target.write_text(doc, encoding="utf-8")
    print(f"Written: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
