#!/usr/bin/env python3
"""Generate self-describing metadata for AGENTS.md from live codebase state.

Reads counts and identifiers from the codebase and injects them into
AGENTS.md sections delimited by AUTO-GEN markers.  Run after any change
to tool_registry, migration_runner, cron/, hooks/, or eval/.

Usage:
    python infra/agents_md_generator.py [--dry-run]

    --dry-run   print JSON metadata and diff without writing
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
AGENTS_MD = REPO / "AGENTS.md"

MARKER_START = "<!--AUTO-GEN:START"
MARKER_END = "<!--AUTO-GEN:END"

SECTION_FNS: dict[str, Any] = {}


def _register(name: str):
    def decorator(fn):
        SECTION_FNS[name] = fn
        return fn
    return decorator


def _count_test_files() -> tuple[int, int]:
    """Return (test_file_count, test_function_count)."""
    eval_dir = REPO / "eval"
    py_files = list(eval_dir.glob("test_*.py"))
    file_count = len(py_files)
    func_count = 0
    for f in py_files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if re.match(r"\s+def test_", line):
                func_count += 1
    return file_count, func_count


def _count_cron_jobs() -> int:
    """Return count of executable cron Python scripts."""
    cron_dir = REPO / "cron"
    return len([f for f in cron_dir.glob("*.py") if f.is_file()])


def _count_hooks() -> int:
    """Return count of concrete hook implementations."""
    hooks_dir = REPO / "hooks"
    return len([f for f in hooks_dir.glob("*.py") if not f.stem.startswith("_")])


def _count_mcp_modules() -> int:
    """Return the number of mcp_*.py modules at repo root."""
    return len(list(REPO.glob("mcp_*.py")))


def _parse_tool_registry() -> dict[str, Any]:
    """Parse tool_registry.py and return counts + CORE tool names."""
    registry_path = REPO / "tool_registry.py"
    text = registry_path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    core_names: list[str] = []
    admin_count = 0
    deprecated_count = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0] if node.targets else None
        if not isinstance(target, ast.Name):
            continue
        if not isinstance(node.value, ast.List):
            continue
        items = node.value.elts
        if target.id == "CORE_TOOLS":
            core_names = [_str_from_ast(el) for el in items]
        elif target.id == "ADMIN_TOOLS":
            admin_count = len(items)
        elif target.id == "DEPRECATED":
            deprecated_count = len(items)

    return {
        "core_count": len(core_names),
        "core_names": core_names,
        "admin_count": admin_count,
        "deprecated_count": deprecated_count,
        "core_visible": len(core_names) + 1,
    }


def _str_from_ast(node: ast.AST) -> str:
    """Extract string value from an ast.Constant node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _parse_migration_runner() -> dict[str, Any]:
    """Extract SCHEMA_VERSION and migration pair count."""
    runner_path = REPO / "infra" / "migration_runner.py"
    text = runner_path.read_text(encoding="utf-8")
    m = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", text)
    schema_version = int(m.group(1)) if m else 0

    migrations_dir = REPO / "migrations"
    sqls = sorted(migrations_dir.glob("*.sql"))
    all_names = [f.name for f in sqls if f.is_file()]
    paired = [n for n in all_names if n.replace(".sql", ".down.sql") in all_names]
    return {
        "schema_version": schema_version,
        "migration_pair_count": len(paired),
    }


def _count_tables_in_schema() -> int:
    """Count distinct CREATE TABLE targets across all migration files."""
    migrations_dir = REPO / "migrations"
    table_names: set[str] = set()
    for sql in migrations_dir.glob("*.sql"):
        try:
            text = sql.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", text, re.IGNORECASE):
            table_names.add(m.group(1))
    return len(table_names)


_EXCLUDE_DIRS = {
    "eval",
    "memory",
    "venv",
    ".venv",
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".devfleet-worktrees",
    "dist",
    "node_modules",
    ".opencode",
    ".github",
    "ts-sdk",
}


def _compute_test_loc() -> int:
    """Approximate test LOC by summing Python file lines."""
    total = 0
    for f in (REPO / "eval").glob("test_*.py"):
        try:
            total += f.read_text(encoding="utf-8", errors="replace").count("\n")
        except OSError:
            pass
    return total


def _compute_production_loc() -> int:
    """Approximate production LOC by summing Python file lines.

    Excludes tests (eval/), the live memory store (memory/), virtualenvs,
    caches, and tooling/SDK dirs so the number tracks real source code.
    """
    total = 0
    for f in REPO.rglob("*.py"):
        rel = f.relative_to(REPO)
        if any(part in _EXCLUDE_DIRS for part in rel.parts):
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            total += f.read_text(encoding="utf-8", errors="replace").count("\n")
        except OSError:
            pass
    return total


def gather() -> dict[str, Any]:
    """Collect all metadata from the codebase."""
    tools = _parse_tool_registry()
    migrations = _parse_migration_runner()
    test_files, test_functions = _count_test_files()
    cron_count = _count_cron_jobs()
    hook_count = _count_hooks()
    mcp_modules = _count_mcp_modules()

    return {
        "schema_version": migrations["schema_version"],
        "migration_count": migrations["migration_pair_count"],
        "table_count": _count_tables_in_schema(),
        "tool_counts": {
            "core": tools["core_count"],
            "admin": tools["admin_count"],
            "deprecated": tools["deprecated_count"],
            "core_visible": tools["core_visible"],
        },
        "core_tool_names": json.dumps(tools["core_names"][:5]) + " ...",
        "test_file_count": test_files,
        "test_function_count": test_functions,
        "test_loc": _compute_test_loc(),
        "production_loc": _compute_production_loc(),
        "cron_job_count": cron_count,
        "hook_count": hook_count,
        "mcp_module_count": mcp_modules,
    }


# ---------------------------------------------------------------------------
# Section content generators
# ---------------------------------------------------------------------------


@_register("what_this_system_is")
def gen_what_this_system_is(data: dict[str, Any]) -> str:
    tc = data["tool_counts"]
    admin_label = f"{tc['admin']} ADMIN + {tc['deprecated']} DEPRECATED"
    return "\n".join([
        f"- **Surface**: {tc['core']} CORE verbs + `memory_maintenance` router ({admin_label} behind router) + {data['hook_count']} lifecycle hooks + {data['cron_job_count']}+ cron jobs",
        f"- **Schema**: v{data['schema_version']}, ~{data['table_count']} tables",
        f"- **Code**: ~{data['production_loc'] // 1000}k LOC production, ~{data['test_loc'] // 1000}k+ test LOC; see `docs/architecture.md`",
        "- **MCP Help**: `docs/MCP_SURFACE.md` — quick-reference for agents using MCP tools. See also [AGENT_QUICKSTART.md](file:///Users/arka/.config/agentic-memory/docs/AGENT_QUICKSTART.md).",
    ])


@_register("hard_rule_4")
def gen_hard_rule_4(data: dict[str, Any]) -> str:
    return str(data["schema_version"])


@_register("hard_rule_6")
def gen_hard_rule_6(data: dict[str, Any]) -> str:
    tc = data["tool_counts"]
    return (
        f"**{tc['core']} CORE tools are user-facing**; "
        f"{tc['admin']} ADMIN + {tc['deprecated']} DEPRECATED are operations behind the single "
        f"`memory_maintenance` router. Don't add CORE tools without checking "
        f"`docs/MCP_SURFACE.md` first."
    )


@_register("mcp_surface_contract")
def gen_mcp_surface_contract(data: dict[str, Any]) -> str:
    tc = data["tool_counts"]
    return "\n".join([
        "**Source of truth for the MCP tool surface: `docs/MCP_SURFACE.md` + `tool_registry.py`**. The MCP",
        f"server exposes **{tc['core']} CORE tools** directly plus **1 `memory_maintenance` router**; "
        f"{tc['admin']} ADMIN + {tc['deprecated']} DEPRECATED are hidden behind it",
        "`memory_maintenance(operation=\"...\")`.",
        "",
        "| Tier | Count | Access |",
        "|------|-------|--------|",
        f"| CORE verbs | {tc['core']} | Direct MCP tool call |",
        f"| ADMIN (legacy) | {tc['admin']} | `memory_maintenance(operation=\"...\")` or `memory_advanced(operation=\"...\")` |",
        f"| DEPRECATED | {tc['deprecated']} | Same as ADMIN (also listed in ADMIN_TOOLS; tracked for audit) |",
    ])


@_register("critical_path")
def gen_critical_path(data: dict[str, Any]) -> str:
    tc = data["tool_counts"]
    admin_label = f"{tc['admin']} ADMIN + {tc['deprecated']} DEPRECATED"
    return "\n".join([
        "agentic-memory/",
        "├── save/ (save/pipeline.py)          ← write path (saga, FTS5, chunks, embeddings, KG, facts, audit, CRDT)",
        "├── search/ (search/orchestrator.py)  ← read path (FTS5 BM25 + usearch vector + ColBERT + temporal decay + neural forget curve)",
        f"├── infra/ (tool_registry.py)         ← {tc['core']} CORE + {admin_label} (single source of truth; tool_registry.py + memory_mcp.py + mcp_maintenance.py)",
        f"├── hooks/                            ← {data['hook_count']} lifecycle hook implementations + 1 log helper",
        "├── background/",
        "│   ├── auto_save.py                  ← async inbox+daemon entry point",
        "│   ├── inbox.py                      ← inbox management + daemon lifecycle",
        "│   ├── daemon.py                     ← long-lived inbox drainer",
        "│   ├── background_worker.py           ← CQRS write-journal reconciler daemon",
        "│   ├── tool_complete.py              ← hook → save_memory pipeline",
        "│   └── circuit_breaker.py            ← auto-save failure gating",
        f"├── cron/                             ← {data['cron_job_count']}+ scheduled jobs + install_crontab.sh",
        f"├── mcp_*.py ({data['mcp_module_count']} modules)             ← domain-split MCP tools",
        "├── memory/                           ← live store (gitignored)",
        "├── docs/MCP_SURFACE.md               ← MCP tool reference for agents",
        f"└── eval/                             ← {data['test_file_count']} test files, {data['test_function_count']}+ test functions",
    ])


@_register("current_state")
def gen_current_state(data: dict[str, Any]) -> str:
    tc = data["tool_counts"]
    admin_label = f"{tc['admin']} ADMIN + {tc['deprecated']} DEPRECATED"
    return "\n".join([
        f"- **Schema v{data['schema_version']}**: {data['migration_count']} migrations (100% down-migration coverage), ~{data['table_count']} tables.",
        f"- **MCP surface**: {tc['core']} CORE verbs + 1 `memory_maintenance` router ({admin_label}). Agents see {tc['core_visible']} tools. See `docs/MCP_SURFACE.md` for verb reference.",
        "- **Write path**: Saga transaction (DB + vec_key + .md file) with flock-based cross-process locking, crash-consistent rollback, and dependent-row cleanup. `defer_expensive=True` by default — returns <200ms.",
        "- **Read path**: 12-phase hybrid search (FTS5 BM25 + usearch vector + ColBERT + cross-encoder + temporal decay + neural forget curve + concept/centrality boost). Phase-level error counters.",
        "- **KG/Temporal**: Entity extraction with Jaccard fuzzy match, temporal KG with contradiction detection and fact supersession, bi-temporal validity.",
        "- **Background**: Async inbox+daemon auto-save with circuit breaker, TS plugin coordination, cron-driven maintenance.",
        f"- **Testing**: {data['test_file_count']} test files, {data['test_function_count']}+ test functions, ~{data['test_loc'] // 1000}k+ test LOC. Subprocess-per-file runner for torch-safe parallelism.",
        "- **Canonical references**: `docs/architecture.md` (architecture), `docs/MCP_SURFACE.md` (MCP workflow), `docs/reference/mcp-tools.md` (tool catalog), `skills/memory-architecture/SKILL.md` (agent walkthrough).",
        "",
        "> Note: For authoritative counts, query `tool_registry.py` and `infra/migration_runner.py` directly.",
        "",
        "> Note: Current Status is a point-in-time snapshot. It will drift. For authoritative counts, query the codebase directly.",
    ])


# ---------------------------------------------------------------------------
# Marker operations
# ---------------------------------------------------------------------------


def _update_markers(text: str, data: dict[str, Any]) -> str:
    """Replace content between AUTO-GEN:START and AUTO-GEN:END marker pairs."""
    pattern = re.compile(
        rf"{re.escape(MARKER_START)}\s+key=\"(\w+)\"\s*-->.*?{re.escape(MARKER_END)}\s+key=\"\1\"\s*-->",
        re.DOTALL,
    )
    result: list[str] = []
    last_end = 0
    for m in pattern.finditer(text):
        key = m.group(1)
        fn = SECTION_FNS.get(key)
        if fn is not None:
            new_content = fn(data)
            result.append(text[last_end : m.start()])
            result.append(
                f"{MARKER_START} key=\"{key}\"-->\n{new_content}\n{MARKER_END} key=\"{key}\"-->"
            )
            last_end = m.end()
        else:
            result.append(text[last_end : m.end()])
            last_end = m.end()
    result.append(text[last_end:])
    return "".join(result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    data = gather()
    text = AGENTS_MD.read_text(encoding="utf-8")

    if "<!--AUTO-GEN:START" not in text:
        print("No AUTO-GEN markers found in AGENTS.md. Add markers before running.")
        sys.exit(1)

    updated = _update_markers(text, data)

    if dry_run:
        print(json.dumps(data, indent=2))
        print("\n--- Updated AGENTS.md excerpt ---\n")
        for i, (o, n) in enumerate(zip(text.splitlines(), updated.splitlines()), 1):
            if o != n:
                print(f"  line {i}: {repr(o)}")
                print(f"     -> {repr(n)}")
        return

    if updated != text:
        AGENTS_MD.write_text(updated, encoding="utf-8")
        print(f"Updated {AGENTS_MD}")
    else:
        print("No changes needed")


if __name__ == "__main__":
    main()
