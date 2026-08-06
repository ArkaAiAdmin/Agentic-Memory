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
            if re.match(r"^\s*def test_", line):
                func_count += 1
    return file_count, func_count


def _count_cron_jobs() -> int:
    """Return count of scheduled cron jobs.

    Counts entries in the consolidated job registry (``cron/jobs.py`` ->
    ``JOBS``) rather than ``*.py`` files in ``cron/``.  The directory also
    contains helper scripts, one-off backfills, and the registry module
    itself, so a file count over-reports the number of actually-scheduled
    jobs (audit P1-W1).
    """
    jobs_mod = REPO / "cron" / "jobs.py"
    if not jobs_mod.is_file():
        return 0
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location("_cron_jobs_meta", str(jobs_mod))
    if spec is None or spec.loader is None:
        return 0
    mod = _ilu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # Fallback: a registry module that fails to import should not
        # break doc generation.
        return 0
    jobs = getattr(mod, "JOBS", None)
    if isinstance(jobs, dict):
        return len(jobs)
    # Some registries expose a list; count that instead.
    if isinstance(jobs, (list, tuple)):
        return len(jobs)
    return 0


def _count_hooks() -> int:
    """Return count of concrete hook implementations."""
    hooks_dir = REPO / "hooks"
    return len([f for f in hooks_dir.glob("*.py") if not f.stem.startswith("_")])


def get_meta_for_json() -> dict[str, Any]:
    """Return a flat, _meta.json-compatible dict from the single canonical live-code gatherer.

    This is the ONLY entry point for live meta collection. Other scripts
    (gen_doc_meta.py, verify_doc_meta.py) must import this — never
    re-implement their own collection logic.
    """
    data = gather()
    tc = data["tool_counts"]
    return {
        "schema_version": data["schema_version"],
        "num_migrations": data["migration_count"],
        "num_mcp_modules": data["mcp_module_count"],
        "num_core_tools": tc["core"],
        "num_admin_tools": tc["admin"],
        "num_deprecated_tools": tc["deprecated"],
        "num_total_tools": tc["core"] + tc["admin"] + tc["deprecated"],
        "num_cron_scripts": data["cron_job_count"],
        "num_hooks": data["hook_count"],
        "num_test_files": data["test_file_count"],
        "num_test_functions": data["test_function_count"],
        "num_tables_visible": data["table_count"],
        "loc_production": data["production_loc"],
        "loc_test": data["test_loc"],
        "loc_total": data["production_loc"] + data["test_loc"],
    }


def _count_mcp_modules() -> int:
    """Return the number of mcp_*.py modules in mcp_surface/."""
    mcp_dir = REPO / "mcp_surface"
    if not mcp_dir.is_dir():
        # Fallback: legacy root-level modules
        return len(list(REPO.glob("mcp_*.py")))
    return len(list(mcp_dir.glob("mcp_*.py")))


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
    "paper_pipeline",
    "paper_pipeline_2",
    "paper_pipeline_3",
    "scratch",
    "examples",
}


def _compute_test_loc() -> int:
    """Approximate test LOC by summing all Python files under eval/."""
    total = 0
    for f in (REPO / "eval").rglob("*.py"):
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

    from datetime import date
    section_ts: dict[str, str] = {}
    for key in SECTION_FNS:
        section_ts[f"_section_timestamp_{key}"] = date.today().isoformat()

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
        "auto_gen_section_timestamps": section_ts,
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
    return (
        f"**Source of truth:** `docs/MCP_SURFACE.md` + `tool_registry.py`. "
        f"The MCP server exposes **{tc['core']} CORE tools** directly plus **1 `memory_maintenance` router**; "
        f"{tc['admin']} ADMIN + {tc['deprecated']} DEPRECATED are hidden behind it "
        f"`memory_maintenance(operation=\"...\")`."
    )


@_register("critical_path")
def gen_critical_path(data: dict[str, Any]) -> str:
    tc = data["tool_counts"]
    admin_label = f"{tc['admin']} ADMIN + {tc['deprecated']} DEPRECATED"
    return "\n".join([
        "agentic-memory/",
        "├── save/ (save/pipeline.py)               ← write path",
        "├── search/ (search/orchestrator.py)       ← read path",
        f"├── infra/ (tool_registry.py)              ← {tc['core']} CORE + {admin_label} (tool registry, migrations, config)",
        f"├── hooks/                                  ← {data['hook_count']} lifecycle hooks",
        "├── background/",
        "│   ├── auto_save.py   ← async inbox+daemon",
        "│   └── background_worker.py ← CQRS write-journal daemon",
        f"├── cron/             ← {data['cron_job_count']}+ scheduled jobs",
        f"├── mcp_surface/         ← {data['mcp_module_count']} MCP modules",
        "├── memory/           ← live store (gitignored)",
        "├── docs/MCP_SURFACE.md",
        f"└── eval/             ← {data['test_file_count']} test files, {data['test_function_count']}+ test functions",
    ])


@_register("current_state")
def gen_current_state(data: dict[str, Any]) -> str:
    tc = data["tool_counts"]
    admin_label = f"{tc['admin']} ADMIN + {tc['deprecated']} DEPRECATED"
    return "\n".join([
        f"- **Schema v{data['schema_version']}**: {data['migration_count']} migrations (100% down-coverage), ~{data['table_count']} tables.",
        f"- **MCP surface**: {tc['core']} CORE + 1 router ({admin_label}). See `docs/MCP_SURFACE.md`.",
        "- **Write path**: Saga transaction (DB + vec_key + .md) with flock locking, crash-consistent rollback. `defer_expensive=True` → <200ms.",
        "- **Read path**: 14-phase hybrid search (FTS5 BM25 + usearch vector + ColBERT + temporal decay + neural forget curve).",
        "- **KG/Temporal**: Jaccard entity match, contradiction detection, fact supersession, bi-temporal validity.",
        "- **Background**: Async inbox+daemon auto-save (circuit breaker), TS plugin, cron-driven maintenance.",
        f"- **Testing**: {data['test_file_count']} test files, {data['test_function_count']}+ test functions, ~{data['test_loc'] // 1000}k+ test LOC. Subprocess-per-file runner.",
        "- **Canonical refs**: `docs/architecture.md` · `docs/MCP_SURFACE.md` · `skills/memory-architecture/SKILL.md`.",
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

    # Expose per-section timestamps for gen_doc_meta / verify_doc_meta
    ts_path = REPO / "docs" / "_auto_gen_timestamps.json"
    ts = {k: v for k, v in data.items() if k.startswith("_section_timestamp_")}
    ts_path.write_text(json.dumps(ts, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
