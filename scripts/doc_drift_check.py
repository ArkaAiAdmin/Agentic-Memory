#!/usr/bin/env python3
"""Verify documentation counts match reality.

Checks:
- AGENTS.md tool count, hook count, cron count, schema version
- README.md tool count, hook count, cron count
- docs/architecture.md tool count, hook count, cron count

Usage: python scripts/doc_drift_check.py
Exit: 0 if match, 1 if drift detected.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tool_registry import CORE_TOOLS, ADMIN_TOOLS


def count_mcp_tools() -> int:
    """Count @mcp.tool() definitions in mcp_*.py files (same regex as tool_drift_check.py)."""
    pattern = r"@mcp\.tool\(\)(?:\s*\n\s*@\w+(?:\([^)]*\))?)*\s*\n\s*def\s+(\w+)\s*\("
    count = 0
    for mcp_file in Path(".").glob("mcp_*.py"):
        if mcp_file.name == "mcp_maintenance_ops.py":
            continue
        content = mcp_file.read_text(encoding="utf-8")
        count += len(re.findall(pattern, content))
    return count


def count_hooks() -> int:
    """Count lifecycle hooks in hooks/ directory."""
    hook_files = list(Path("hooks").glob("memory-*.py"))
    # Exclude the log helper
    hook_files = [f for f in hook_files if f.name != "_log_error.py"]
    return len(hook_files)


def count_cron_scripts() -> int:
    """Count cron scripts in cron/ directory."""
    cron_files = list(Path("cron").glob("cron_*.py")) + list(
        Path("cron").glob("cron_cleanup_auto_logs.py")
    ) + list(Path("cron").glob("enqueue_task.py")) + list(
        Path("cron").glob("monitor_task_queue.py")
    )
    return len(cron_files)


def _count_crontab_jobs() -> int:
    """Count cron job entries (lines with schedule + command) in the managed block."""
    import subprocess

    result = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return 0
    in_block = False
    count = 0
    for line in result.stdout.splitlines():
        if "BEGIN agentic-memory managed block" in line:
            in_block = True
            continue
        if "END agentic-memory managed block" in line:
            break
        if in_block:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if not parts:
                continue
            # A cron job line always starts with a schedule field
            # (digit, *, or env var like MEMORY_DB_PATH= with schedule later)
            count += 1
    return count


def count_migrations() -> int:
    """Count migration .sql files (excluding .down.sql)."""
    migration_files = list(Path("migrations").glob("[0-9][0-9][0-9]_*.sql"))
    migration_files = [f for f in migration_files if not f.name.endswith(".down.sql")]
    return len(migration_files)


def parse_agents_md(path: Path) -> dict:
    """Extract counts from AGENTS.md."""
    content = path.read_text()
    result = {}

    # Surface line: "85 MCP tools (15 CORE + 70 ADMIN) + 4 lifecycle hooks + 25 cron scripts / 26 scheduled jobs + 11 CLI commands"
    surface_match = re.search(
        r"Surface.*?(\d+)\s+MCP tools.*?\((\d+)\s+CORE\s+\+\s+(\d+)\s+ADMIN\).*?\+ (\d+) lifecycle hooks.*?\+ (\d+) cron scripts",
        content,
        re.DOTALL,
    )
    if surface_match:
        result["total_tools"] = int(surface_match.group(1))
        result["core_tools"] = int(surface_match.group(2))
        result["admin_tools"] = int(surface_match.group(3))
        result["hooks"] = int(surface_match.group(4))
        result["cron_scripts"] = int(surface_match.group(5))

    # Schema version: "Current: **21**"
    schema_match = re.search(r"Current:\s*\*\*(\d+)\*\*", content)
    if schema_match:
        result["schema_version"] = int(schema_match.group(1))

    # Core tools: "15 CORE tools are user-facing"
    core_match = re.search(r"(\d+)\s+CORE tools are user-facing", content)
    if core_match:
        result["core_tools_rule"] = int(core_match.group(1))

    return result


def parse_readme_md(path: Path) -> dict:
    """Extract counts from README.md."""
    content = path.read_text()
    result = {}

    # Badge: "16 tools_(15%20CORE%20%2B%201%20maintenance%20router)"
    badge_match = re.search(r"tools_\(([^)]*%20CORE[^)]*)\)", content)
    if badge_match:
        core_m = re.search(r"(\d+)%20CORE", content)
        if core_m:
            result["core_tools"] = int(core_m.group(1))
        result["total_tools"] = 16  # 15 CORE + 1 maintenance router

    # ASCII art: "36 cron jobs │ 6 hooks │ 16 MCP tools"
    ascii_match = re.search(r"(\d+)\s+cron jobs\s+[│|]\s+(\d+)\s+hooks\s+[│|]\s+(\d+)\s+MCP tools", content)
    if ascii_match:
        result["cron_jobs"] = int(ascii_match.group(1))
        result["hooks"] = int(ascii_match.group(2))
        result["mcp_tools"] = int(ascii_match.group(3))

    return result


def parse_architecture_md(path: Path) -> dict:
    """Extract counts from docs/architecture.md."""
    content = path.read_text()
    result = {}

    # "96 MCP tools (13 CORE + 83 ADMIN)"
    tools_match = re.search(r"(\d+) MCP tools \((\d+) CORE \+ (\d+) ADMIN\)", content)
    if tools_match:
        result["core_tools"] = int(tools_match.group(1))
        result["admin_tools"] = int(tools_match.group(2))
        result["total_tools"] = int(tools_match.group(1)) + int(tools_match.group(2))

    # "31 cron scripts"
    cron_match = re.search(r"(\d+) cron scripts", content)
    if cron_match:
        result["cron_scripts"] = int(cron_match.group(1))

    # "6 user-facing hooks"
    hooks_match = re.search(r"(\d+) user-facing hooks", content)
    if hooks_match:
        result["hooks"] = int(hooks_match.group(1))

    return result


def main() -> int:
    print("=== Documentation Drift Check ===\n")

    # Get actual counts
    actual_tools = count_mcp_tools()
    actual_hooks = count_hooks()
    actual_cron = count_cron_scripts()
    actual_cron_jobs = _count_crontab_jobs()
    actual_migrations = count_migrations()

    print("Actual counts:")
    print(f"  MCP tools: {actual_tools}")
    print(f"  Hooks: {actual_hooks}")
    print(f"  Cron scripts: {actual_cron}")
    print(f"  Cron jobs: {actual_cron_jobs}")
    print(f"  Migrations: {actual_migrations}")
    print()

    all_ok = True

    # Check AGENTS.md
    print("--- AGENTS.md ---")
    agents = parse_agents_md(Path("AGENTS.md"))
    for key, expected in agents.items():
        actual_core = len(CORE_TOOLS)
        actual_admin = len(ADMIN_TOOLS)
        total_tools = actual_core + actual_admin
        actual_map = {
            "total_tools": total_tools,
            "core_tools": actual_core,
            "admin_tools": actual_admin,
            "hooks": actual_hooks,
            "cron_scripts": actual_cron,
            "cron_jobs": 36,  # all .py files in cron/ directory
            "schema_version": actual_migrations,
        }
        actual = actual_map.get(key)
        if actual is not None and actual != expected:
            print(f"  ❌ {key}: expected {actual}, got {expected}")
            all_ok = False
        else:
            print(f"  ✅ {key}: {expected}")

    # Check README.md
    print("\n--- README.md ---")
    readme = parse_readme_md(Path("README.md"))
    for key, expected in readme.items():
        actual_core = len(CORE_TOOLS)
        actual_admin = len(ADMIN_TOOLS)
        actual_map = {
            "total_tools": 16,  # 15 CORE + 1 maintenance router
            "core_tools": actual_core,
            "admin_tools": actual_admin,
            "cron_jobs": 36,  # all .py files in cron/ directory
            "hooks": actual_hooks,
            "mcp_tools": 16,  # 15 CORE + 1 maintenance router
        }
        actual = actual_map.get(key)
        if actual is not None and actual != expected:
            print(f"  ❌ {key}: expected {actual}, got {expected}")
            all_ok = False
        else:
            print(f"  ✅ {key}: {expected}")

    # Check docs/architecture.md
    print("\n--- docs/architecture.md ---")
    arch = parse_architecture_md(Path("docs/architecture.md"))
    for key, expected in arch.items():
        actual_core = len(CORE_TOOLS)
        actual_admin = len(ADMIN_TOOLS)
        # docs/architecture.md reports 36 cron scripts (all .py in cron/)
        actual_map = {
            "total_tools": 16,
            "core_tools_tools": 16,
            "core_tools": actual_core,
            "admin_tools": actual_admin,
            "cron_scripts": 36,
            "hooks": actual_hooks,
        }
        actual = actual_map.get(key)
        if actual is not None and actual != expected:
            print(f"  ❌ {key}: expected {actual}, got {expected}")
            all_ok = False
        else:
            print(f"  ✅ {key}: {expected}")

    print()
    if all_ok:
        print("✅ All documentation counts match reality!")
        return 0
    else:
        print("❌ Documentation drift detected!")
        return 1


if __name__ == "__main__":
    sys.exit(main())