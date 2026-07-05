"""CLI entry points for agentic-memory.

Usage:
    python cli.py server        — Start MCP server (default)
    python cli.py search <q>    — Search memories
    python cli.py rebuild       — Rebuild FTS5 index
    python cli.py backfill      — Full backfill (FTS5+chunks+KG+vec)
    python cli.py consolidate   — Dedup + contradiction detect
    python cli.py integrity     — DB health check
    python cli.py tier          — Tier migration
    python cli.py compact       — Full consolidation pipeline
    python cli.py bootstrap     — Initialize project
    python cli.py worker        — Process background tasks
    python cli.py sync          — One-shot peer sync (P2 #2 wire-up)
    python cli.py init          — One-command project bootstrap
    python cli.py doctor        — Comprehensive health check + report
    python cli.py status        — One-line health snapshot
    python cli.py version       — Print installed version, check PyPI
    python cli.py install-mcp   — Register MCP server in harness configs
    python cli.py dashboard     — Launch / stop / check Streamlit dashboard
"""

from __future__ import annotations

import shutil
import signal
import sys
import os
import subprocess
from pathlib import Path
from typing import Callable, NoReturn

SCRIPTS: str = os.path.dirname(os.path.abspath(__file__))
PYTHON: str = sys.executable


def _run(script: str, args: list[str] | None = None) -> None:
    script_path: str = os.path.join(SCRIPTS, script)
    if not os.path.exists(script_path):
        script_path = os.path.join(SCRIPTS, "cron", script)
    cmd: list[str] = [PYTHON, script_path] + (args or [])
    subprocess.run(cmd, timeout=300, check=True)


def server_main() -> None:
    """Run the MCP server. Auto-bootstraps the config dir on first run."""
    from pathlib import Path
    from config import GLOBAL_SCRIPTS_DIR

    global_dir: Path = GLOBAL_SCRIPTS_DIR
    memory_dir: Path = global_dir / "memory"
    db_path: Path = memory_dir / "memory.db"

    if not db_path.exists():
        memory_dir.mkdir(parents=True, exist_ok=True)
        for sub in [
            "lessons",
            "decisions",
            "preferences",
            "sessions",
            "projects",
            "backups",
        ]:
            (memory_dir / sub).mkdir(exist_ok=True)
        (memory_dir / "MEMORY.md").write_text(
            "---\ncreated: 2026-01-01T00:00:00\nupdated: 2026-01-01T00:00:00\n"
            "observed_at: 2026-01-01T00:00:00\ntags: [index, memory-system]\n---\n\n"
            "# Agentic Memory\n\nSystem populated on first use.\n"
        )
        rb: Path = Path(__file__).resolve().parent / "rebuild_index.py"
        if rb.exists():
            subprocess.run(
                [sys.executable, str(rb), str(memory_dir), str(db_path)],
                capture_output=True,
                timeout=60,
            )

    os.environ.setdefault("MEMORY_DB_PATH", str(db_path))
    import mcp_instance
    import mcp_tools  # noqa: F401
    import memory_mcp  # noqa: F401

    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        mcp_instance.mcp.run()
    except (BrokenPipeError, OSError, EOFError):
        pass  # parent closed stdio — expected during restart


def search_main() -> NoReturn:
    query: str = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
    if not query:
        print("Usage: cli.py search <query>")
        sys.exit(1)
    _run("search_memory.py", [query])
    sys.exit(0)


def rebuild_main() -> None:
    from infra.infrastructure import resolve_active_memory_dir

    d = resolve_active_memory_dir()
    _run("rebuild_index.py", [str(d), str(d / "memory.db")])


def backfill_main() -> None:
    _run("backfill_all.py", ["--full"])


def consolidate_main() -> None:
    _run("cron_consolidate.py")


def integrity_main() -> None:
    _run("memory_integrity.py")


def tier_main() -> None:
    _run("cron_tier_migration.py", ["--once"])


def compact_main() -> None:
    _run("cron_compact.py")


def bootstrap_main() -> None:
    if sys.platform != "win32":
        _run("setup_memory.sh")
    else:
        print("Run setup_memory.sh manually")


def worker_main() -> None:
    """Process pending background tasks. Honors --drain / --once / --type / --max-tasks flags.

    Forwarded flag semantics:
        --drain      process all pending tasks (or until --max-tasks hit), then exit
        --once       process one task and exit (cron-friendly)
        --type=X     only process tasks of type X
        --max-tasks=N safety cap on --drain mode

    Without any flag, defaults to --drain. This is the common operator
    use case (flush a backlog after downtime or first install).

    Examples:
        agentic-memory-worker                       # drain the queue
        agentic-memory-worker --once                # one task
        agentic-memory-worker --drain --type=entity_resolution
        agentic-memory-worker --once --max-tasks=10
    """
    args: list[str] = sys.argv[2:]
    if "--drain" in args:
        passthrough: list[str] = [a for a in args if a != "--drain"]
        _run("background_worker.py", ["--drain"] + passthrough)
    elif "--once" in args:
        passthrough = [a for a in args if a != "--once"]
        _run("background_worker.py", ["--once"] + passthrough)
    else:
        _run("background_worker.py", ["--drain"] + args)


def sync_main() -> int:
    """One-shot peer sync (P2 #2 wire-up).

    Usage:
        python cli.py sync --peer <url> [--db <path>] [--name <n>]
                           [--peer-agent <id>] [--limit N]

    The peer URL can also be provided via MEMORY_SYNC_PEER env var.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description="One-shot peer sync (P2 #2)")
    parser.add_argument("--peer", help="Peer sync server URL (or MEMORY_SYNC_PEER)")
    parser.add_argument("--db", help="Local memory.db path (or MEMORY_DB_PATH)")
    parser.add_argument("--name", help="Peer name (for sync_log)")
    parser.add_argument(
        "--peer-agent", help="Peer agent id (or MEMORY_SYNC_PEER_AGENT_ID)"
    )
    parser.add_argument("--limit", type=int, default=200)
    parsed = parser.parse_args(sys.argv[2:])

    package_root: str = os.path.dirname(os.path.abspath(__file__))
    if package_root not in sys.path:
        sys.path.insert(0, package_root)

    from infra.sync_client import sync_once

    result: dict = sync_once(
        peer_url=parsed.peer,
        db_path=parsed.db,
        peer_name=parsed.name,
        peer_agent_id=parsed.peer_agent,
        limit=parsed.limit,
    )

    if "error" in result and not result.get("push"):
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 2


# ═══════════════════════════════════════════════════════════════════════════
# NEW COMMANDS — P1 product-polish sprint
# ═══════════════════════════════════════════════════════════════════════════


def init_main() -> None:
    """One-command project bootstrap. Replaces manual setup_memory.sh invocation.

    Idempotent: safe to re-run. Skips steps whose outputs already exist.
    """
    import argparse
    import shutil

    parser = argparse.ArgumentParser(
        description="Bootstrap agentic-memory for the current project"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run all steps even if outputs already exist",
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Skip the pip install check (assumes CLI is already on PATH)",
    )
    parsed = parser.parse_args(sys.argv[2:])

    scripts_dir: str = os.path.dirname(os.path.abspath(__file__))

    # 1. Ensure the agentic-memory CLI is on PATH
    if not parsed.no_install and not shutil.which("agentic-memory-server"):
        print("agentic-memory CLI not found. Installing in editable mode...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", scripts_dir],
            check=False,
            timeout=300,
        )
        if not shutil.which("agentic-memory-server"):
            print(
                "WARNING: install finished but CLI still not on PATH. "
                "You may need to restart your shell or run: "
                f"  {sys.executable} -m pip install -e {scripts_dir}"
            )

    # 2. Run the bash setup script (idempotent)
    setup_script = os.path.join(scripts_dir, "setup_memory.sh")
    if not os.path.exists(setup_script):
        print(f"ERROR: setup_memory.sh not found at {setup_script}")
        sys.exit(1)

    env = os.environ.copy()
    env["AGENTIC_MEMORY_DIR"] = str(Path.home() / ".config" / "agentic-memory")
    result = subprocess.run(
        ["bash", setup_script],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(f"setup_memory.sh failed (exit {result.returncode}):")
        print(result.stderr or result.stdout)
        sys.exit(result.returncode)

    print(result.stdout)

    # 3. Quick post-init verification
    print("─" * 50)
    print("Post-init verification:")
    db_candidates = [
        Path.home() / ".config" / "agentic-memory" / "memory" / "memory.db",
        Path(".venv")
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "agentic_memory"
        / "memory"
        / "memory.db",
    ]
    db_found = next((p for p in db_candidates if p.exists()), None)
    if db_found:
        print(f"  DB found: {db_found}")
    else:
        print("  DB: not yet created (will be created on first server run)")

    # Check key directories
    am_dir = Path.home() / ".config" / "agentic-memory"
    for sub in ["memory", "memory/sessions", "memory/lessons", "memory/decisions"]:
        p = am_dir / sub
        status = "OK" if p.exists() else "MISSING"
        print(f"  {sub}/: {status}")

    print()
    print("Next steps:")
    print("  1. Run  : agentic-memory server")
    print("  2. Open : agentic-memory dashboard")
    print("  3. Check: agentic-memory doctor")


def doctor_main() -> None:
    """Comprehensive health check. Exits 0 if all checks pass, 1 on warnings, 2 on failures.

    Writes a JSON report to memory/doctor_report.json for the dashboard to consume.
    """
    import argparse
    import json
    from datetime import datetime, timezone

    parser = argparse.ArgumentParser(description="Run all agentic-memory health checks")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON only (no human-readable summary)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-apply safe repairs (recover orphan .md files, repair FTS drift)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to memory.db (default: from MEMORY_DB_PATH or active dir)",
    )
    parsed = parser.parse_args(sys.argv[2:])

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from infra.infrastructure import resolve_active_memory_dir

    mem_dir = resolve_active_memory_dir()
    db_path = Path(parsed.db) if parsed.db else mem_dir / "memory.db"

    checks: list[dict] = []
    worst: str = "ok"

    def add_check(name: str, severity: str, detail: str, fixable: bool = False) -> None:
        nonlocal worst
        rank = {"ok": 0, "info": 0, "warning": 1, "failure": 2}
        if rank.get(severity, 0) > rank.get(worst, 0):
            worst = severity
        checks.append(
            {
                "check": name,
                "severity": severity,
                "detail": detail,
                "fixable": fixable,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )

    # ── Check 1: DB file exists ────────────────────────────────────────────
    if not db_path.exists():
        add_check("db_exists", "failure", f"DB not found at {db_path}")
    else:
        add_check("db_exists", "ok", f"DB at {db_path}")

        # ── Check 2: Schema version ────────────────────────────────────────
        try:
            import sqlite3

            with sqlite3.connect(str(db_path), timeout=5) as conn:
                row = conn.execute(
                    "SELECT version FROM schema_version WHERE id=1"
                ).fetchone()
                db_version = row[0] if row else None
                from infra.migration_runner import SCHEMA_VERSION

                if db_version is None:
                    add_check(
                        "schema_version",
                        "warning",
                        f"schema_version table missing (fresh DB expected: v{SCHEMA_VERSION})",
                    )
                elif db_version == SCHEMA_VERSION:
                    add_check(
                        "schema_version",
                        "ok",
                        f"v{db_version} (current: v{SCHEMA_VERSION})",
                    )
                else:
                    add_check(
                        "schema_version",
                        "failure",
                        f"DB at v{db_version}, code expects v{SCHEMA_VERSION} — run migrations",
                    )
        except Exception as exc:
            add_check("schema_version", "failure", str(exc))

        # ── Check 3: Integrity (via existing module) ───────────────────────
        try:
            from memory_integrity import check_index_integrity

            report = check_index_integrity(db_path, deep=False)
            criticals = [
                f for f in report.get("findings", []) if f.get("severity") == "critical"
            ]
            warnings = [
                f for f in report.get("findings", []) if f.get("severity") == "warning"
            ]
            if criticals:
                add_check(
                    "db_integrity",
                    "failure",
                    f"{len(criticals)} critical, {len(warnings)} warnings",
                    fixable=True,
                )
            elif warnings:
                add_check("db_integrity", "warning", f"{len(warnings)} warnings")
            else:
                add_check("db_integrity", "ok", "No issues found")
        except Exception as exc:
            add_check("db_integrity", "failure", str(exc))

        # ── Check 4: FTS5 drift ────────────────────────────────────────────
        try:
            from memory_integrity import check_index_integrity

            report = check_index_integrity(db_path, deep=False)
            fts_findings = [
                f
                for f in report.get("findings", [])
                if "fts" in f.get("check", "").lower() and f.get("severity") != "ok"
            ]
            if fts_findings:
                add_check(
                    "fts5_drift",
                    "warning",
                    fts_findings[0].get("detail", "FTS drift detected"),
                    fixable=True,
                )
            else:
                add_check("fts5_drift", "ok", "FTS5 in sync")
        except Exception as exc:
            add_check("fts5_drift", "warning", str(exc))

        # ── Check 5: KG orphans ────────────────────────────────────────────
        try:
            import sqlite3

            with sqlite3.connect(str(db_path), timeout=5) as conn:
                from memory_integrity import find_kg_orphans

                kg = find_kg_orphans(conn)
                orphan_ents = len(kg.get("orphan_entities", []))
                orphan_edges = len(kg.get("orphan_edges", []))
                if orphan_ents or orphan_edges:
                    add_check(
                        "kg_orphans",
                        "warning",
                        f"{orphan_ents} orphan entities, {orphan_edges} orphan edges",
                        fixable=True,
                    )
                else:
                    add_check("kg_orphans", "ok", "No KG orphans")
        except Exception as exc:
            add_check("kg_orphans", "warning", str(exc))

    # ── Check 6: Hook errors / circuit breaker ─────────────────────────────
    hook_errors = mem_dir / "hook-errors.jsonl"
    if not hook_errors.exists():
        add_check("hook_errors", "ok", "No hook errors logged yet")
    else:
        try:
            import json

            lines = hook_errors.read_text(errors="replace").splitlines()
            recent = [json.loads(line) for line in lines[-50:] if line.strip()]
            open_circuits: dict[str, int] = {}
            for entry in recent:
                label = entry.get("label", "?")
                count = entry.get("failureCount", entry.get("failure_count", 0))
                if count >= 10:
                    open_circuits[label] = count
            if open_circuits:
                add_check(
                    "hook_errors",
                    "warning",
                    f"Circuit OPEN for: {', '.join(f'{k} ({v}fail)' for k, v in open_circuits.items())}",
                )
            else:
                add_check(
                    "hook_errors",
                    "ok",
                    f"OK ({len(lines)} total entries, no open circuits)",
                )
        except Exception as exc:
            add_check("hook_errors", "warning", str(exc))

    # ── Check 7: Context monitor state ────────────────────────────────────
    state_file = mem_dir / "sessions" / ".context_monitor_state.json"
    if not state_file.exists():
        add_check("context_monitor", "info", "No state file yet (no session started)")
    else:
        try:
            import json

            state = json.loads(state_file.read_text())
            tool_count = state.get("total_tool_calls", state.get("tool_call_count", 0))
            last_compact = state.get("last_compaction_time", 0)
            add_check(
                "context_monitor",
                "ok",
                f"tool_calls={tool_count}, last_compaction={datetime.fromtimestamp(last_compact).isoformat() if last_compact else 'never'}",
            )
        except Exception as exc:
            add_check("context_monitor", "warning", str(exc))

    # ── Check 8: Allowlist config ─────────────────────────────────────────
    try:
        from background.auto_save import _resolve_allowlist

        al = _resolve_allowlist()
        if al is None:
            add_check("allowlist", "ok", "Unrestricted (allowlist='*')")
        else:
            add_check("allowlist", "ok", f"{len(al)} tools in allowlist")
    except Exception as exc:
        add_check("allowlist", "warning", str(exc))

    # ── Check 9: Memory dir writable ──────────────────────────────────────
    try:
        test_file = mem_dir / ".perm_test"
        test_file.write_text("ok")
        test_file.unlink()
        add_check("dir_writable", "ok", f"{mem_dir} is writable")
    except Exception as exc:
        add_check("dir_writable", "failure", str(exc))

    # ── Check 10: Plugin wiring ───────────────────────────────────────────
    try:
        opencode_jsonc = Path.home() / ".config" / "opencode" / "opencode.jsonc"
        if opencode_jsonc.exists():
            text = opencode_jsonc.read_text()
            plugin_path = str(Path.home() / ".config" / "agentic-memory" / "plugin")
            if plugin_path in text:
                add_check(
                    "plugin_wiring",
                    "ok",
                    "agentic-memory/plugin registered in opencode.jsonc",
                )
            else:
                add_check(
                    "plugin_wiring",
                    "warning",
                    f"Plugin path {plugin_path} not found in opencode.jsonc",
                )
        else:
            add_check(
                "plugin_wiring",
                "info",
                "opencode.jsonc not found (not using OpenCode?)",
            )
    except Exception as exc:
        add_check("plugin_wiring", "warning", str(exc))

    # ── Output ────────────────────────────────────────────────────────────
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "mem_dir": str(mem_dir),
        "worst": worst,
        "checks": checks,
    }
    report_path = mem_dir / "doctor_report.json"
    try:
        report_path.write_text(json.dumps(report, indent=2))
        report["report_path"] = str(report_path)
    except Exception:
        pass

    if parsed.json:
        print(json.dumps(report, indent=2))
    else:
        _print_doctor_report(report)

    if parsed.fix and worst != "ok":
        print()
        print("─" * 50)
        print("Auto-repair (--fix):")
        fix_applied = False

        fixable_checks = [c for c in checks if c.get("fixable")]
        if not fixable_checks:
            print("  No auto-fixable issues found.")
        for c in fixable_checks:
            label = c["check"]
            print(f"  Repairing: {label} ...", end=" ", flush=True)
            try:
                if label == "kg_orphans":
                    from memory_integrity import repair_kg_orphans

                    repair_kg_orphans(Path(db_path))
                    print("done")
                    fix_applied = True
                elif label == "fts5_drift":
                    from infra.infrastructure import resolve_active_memory_dir

                    mem_dir = resolve_active_memory_dir()
                    _run("rebuild_index.py", [str(mem_dir), str(mem_dir / "memory.db")])
                    print("done (FTS5 rebuilt)")
                    fix_applied = True
                elif label == "db_integrity":
                    from infra.infrastructure import resolve_active_memory_dir

                    mem_dir = resolve_active_memory_dir()
                    _run(
                        "memory_integrity.py",
                        [str(mem_dir / "memory.db"), "--recover-orphan-files"],
                    )
                    print("done")
                    fix_applied = True
                else:
                    print("skipped (no handler)")
            except Exception as exc:
                print(f"FAILED: {exc}")

        if fix_applied:
            print("Repairs applied. Re-run 'agentic-memory doctor' to verify.")

    sys.exit({"ok": 0, "info": 0, "warning": 1, "failure": 2}.get(worst, 0))


def _print_doctor_report(report: dict) -> None:
    """Pretty-print doctor report for TTY consumption."""
    SEV_ICON = {"ok": "✅", "warning": "⚠️", "failure": "❌"}
    print(f"Agentic Memory Doctor — {report['ts']}")
    print(f"DB: {report['db_path']}")
    print()
    for c in report["checks"]:
        icon = SEV_ICON.get(c["severity"], "?")
        print(f"  {icon} [{c['severity'].upper():8s}] {c['check']}: {c['detail']}")
    print()
    if report["worst"] == "ok":
        print("All checks passed.")
    elif report["worst"] == "warning":
        print(
            "Warnings found — review above. Run with --fix to auto-repair safe items."
        )
    else:
        print(
            "FAILURES found — action required. Run with --fix for auto-repair where possible."
        )
    rp = report.get("report_path")
    if rp:
        print(f"\nReport: {rp}")


def status_main() -> None:
    """One-line health snapshot for tmux status bars and quick TTY checks."""
    import json

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from infra.infrastructure import resolve_active_memory_dir
    from infra.migration_runner import SCHEMA_VERSION

    mem_dir = resolve_active_memory_dir()
    db_path = mem_dir / "memory.db"

    ok_color = "\033[32m"
    warn_color = "\033[33m"
    fail_color = "\033[31m"
    reset = "\033[0m"
    use_color = sys.stdout.isatty()

    def _c(text: str, color: str) -> str:
        return f"{color}{text}{reset}" if use_color else text

    parts: list[str] = []

    # DB
    if db_path.exists():
        size_mb = db_path.stat().st_size / 1024 / 1024
        parts.append(f"db={size_mb:.1f}MB")
        try:
            import sqlite3

            with sqlite3.connect(str(db_path), timeout=3) as conn:
                n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                parts.append(f"memories={n}")
                row = conn.execute(
                    "SELECT version FROM schema_version WHERE id=1"
                ).fetchone()
                db_ver = row[0] if row else "?"
                if db_ver != SCHEMA_VERSION:
                    parts.append(
                        _c(f"SCHEMA=v{db_ver}!need=v{SCHEMA_VERSION}", fail_color)
                    )
                else:
                    parts.append(_c(f"schema=v{db_ver}", ok_color))
        except Exception as exc:
            parts.append(_c(f"db_err={exc}", fail_color))
    else:
        parts.append(_c("db=missing", fail_color))

    # Circuit breaker
    hook_errors = mem_dir / "hook-errors.jsonl"
    if hook_errors.exists():
        try:
            lines = hook_errors.read_text(errors="replace").splitlines()
            import json as _json

            recent = [_json.loads(line) for line in lines[-20:] if line.strip()]
            open_circuits = {
                e["label"]
                for e in recent
                if (e.get("failureCount") or e.get("failure_count") or 0) >= 10
            }
            if open_circuits:
                parts.append(
                    _c(f"circuits=OPEN:{','.join(sorted(open_circuits))}", fail_color)
                )
            else:
                parts.append(_c("circuits=ok", ok_color))
        except Exception:
            parts.append(_c("circuits=?", warn_color))
    else:
        parts.append(_c("circuits=ok", ok_color))

    # Auto-save state
    state_file = mem_dir / "sessions" / ".context_monitor_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            tc = state.get("tool_calls", state.get("total_tool_calls", 0))
            parts.append(f"tool_calls={tc}")
        except Exception:
            pass

    # Allowlist
    try:
        from background.auto_save import _resolve_allowlist

        al = _resolve_allowlist()
        parts.append(f"allowlist={'*' if al is None else len(al)}")
    except Exception:
        parts.append("allowlist=?")

    print(" | ".join(parts))


def version_main() -> None:
    """Print the installed version and check PyPI for updates."""
    try:
        from importlib.metadata import version as _pkg_version

        installed = _pkg_version("agentic-memory")
    except Exception:
        installed = "dev (unknown)"

    try:
        import urllib.request

        with urllib.request.urlopen(
            "https://pypi.org/pypi/agentic-memory/json", timeout=5
        ) as resp:
            data = __import__("json").loads(resp.read())
        latest = data["info"]["version"]

        if installed == latest:
            print(f"agentic-memory {installed} (latest)")
        else:
            try:
                from packaging.version import Version

                if Version(installed) < Version(latest):
                    print(
                        f"agentic-memory {installed} (update available: {latest})"
                    )
                else:
                    print(f"agentic-memory {installed} (newer than PyPI {latest})")
            except Exception:
                print(f"agentic-memory {installed} (PyPI: {latest})")
    except Exception:
        print(f"agentic-memory {installed}")


def install_mcp_main() -> None:
    """Register agentic-memory as an MCP server in harness config files.

    Updates:
      OpenCode   ~/.opencode/mcp-configs/mcp-servers.json
      Claude Desktop  ~/Library/Application Support/Claude/claude_desktop_config.json
      Claude Code     ~/.claude.json  or  ~/.claude/settings.json

    The entry uses ``agentic-memory-server`` when available, otherwise
    falls back to ``python -m memory_mcp``.
    """
    import json

    # Resolve launch command: prefer installed console script, fall back to
    # venv python + module, then system python.
    am_dir = Path.home() / ".config" / "agentic-memory"
    command: str
    args: list[str]
    cli_path = shutil.which("agentic-memory-server")
    if cli_path:
        command = cli_path
        args = []
    else:
        for venv_name in ["venv", ".venv"]:
            candidate = am_dir / venv_name / "bin" / "python"
            if candidate.exists():
                command = str(candidate)
                args = ["-m", "memory_mcp"]
                break
        else:
            command = sys.executable
            args = ["-m", "memory_mcp"]

    entry: dict[str, object] = {
        "command": command,
        "args": args,
        "description": "Agentic Memory: local-first hybrid agent memory system",
    }
    updated: list[str] = []

    def _update(path: Path) -> bool:
        data = json.loads(path.read_text())
        servers = data.setdefault("mcpServers", {})
        if "agentic-memory" in servers:
            return False
        servers["agentic-memory"] = entry
        path.write_text(json.dumps(data, indent=4) + "\n")
        return True

    # OpenCode
    oc_mcp = Path.home() / ".opencode" / "mcp-configs" / "mcp-servers.json"
    if oc_mcp.exists():
        try:
            if _update(oc_mcp):
                updated.append(str(oc_mcp))
        except Exception as exc:
            print(f"warning: {oc_mcp}: {exc}", file=sys.stderr)

    # Claude Desktop
    cd_cfg = (
        Path.home()
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json"
    )
    if cd_cfg.exists():
        try:
            if _update(cd_cfg):
                updated.append(str(cd_cfg))
        except Exception as exc:
            print(f"warning: {cd_cfg}: {exc}", file=sys.stderr)

    # Claude Code
    for cc_cfg in [Path.home() / ".claude.json", Path.home() / ".claude" / "settings.json"]:
        if cc_cfg.exists():
            try:
                if _update(cc_cfg):
                    updated.append(str(cc_cfg))
            except Exception as exc:
                print(f"warning: {cc_cfg}: {exc}", file=sys.stderr)

    if updated:
        print(f"Registered agentic-memory in {len(updated)} config(s):")
        for p in updated:
            print(f"  {p}")
    else:
        print("No harness config found. Add this to your MCP config:\n")
        print(json.dumps({"agentic-memory": entry}, indent=4))
    print("\nRestart your harness to pick up the new MCP server.")


def dashboard_main() -> None:
    """Launch, stop, or check the Streamlit dashboard.

    Usage:
        agentic-memory dashboard start [--port 8501]
        agentic-memory dashboard stop
        agentic-memory dashboard status
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Manage the agentic-memory Streamlit dashboard"
    )
    parser.add_argument(
        "action",
        choices=["start", "stop", "status"],
        help="Action: start, stop, or check status",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8501,
        help="HTTP port (default: 8501)",
    )
    parsed = parser.parse_args(sys.argv[2:])

    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    dashboard_script = Path(scripts_dir) / "dashboard.py"

    if not dashboard_script.exists():
        print(f"ERROR: dashboard.py not found at {dashboard_script}")
        sys.exit(1)

    # Find streamlit binary
    streamlit_bin = shutil.which("streamlit")
    if not streamlit_bin:
        bin_dir = Path(sys.executable).parent
        for candidate in [
            bin_dir / "streamlit",
            Path(sys.prefix) / "bin" / "streamlit",
        ]:
            if candidate.exists():
                streamlit_bin = str(candidate)
                break
    if not streamlit_bin:
        print(
            "ERROR: streamlit not found. Install with:\n"
            f"  {sys.executable} -m pip install streamlit pandas plotly"
        )
        sys.exit(1)

    _DASHBOARD_PID_FILE = (
        Path.home() / ".config" / "agentic-memory" / "memory" / ".dashboard.pid"
    )

    if parsed.action == "start":
        if _DASHBOARD_PID_FILE.exists():
            try:
                old_pid = int(_DASHBOARD_PID_FILE.read_text().strip())
                os.kill(old_pid, 0)
                print(
                    f"Dashboard already running (PID {old_pid}, http://localhost:{parsed.port})"
                )
                return
            except (ProcessLookupError, ValueError, OSError):
                _DASHBOARD_PID_FILE.unlink(missing_ok=True)

        proc = subprocess.Popen(
            [
                streamlit_bin,
                "run",
                str(dashboard_script),
                "--server.port",
                str(parsed.port),
                "--server.address",
                "0.0.0.0",
                "--server.headless",
                "true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _DASHBOARD_PID_FILE.write_text(str(proc.pid))
        print(f"Dashboard started (PID {proc.pid}): http://localhost:{parsed.port}")

    elif parsed.action == "stop":
        if not _DASHBOARD_PID_FILE.exists():
            print("Dashboard not running (no PID file)")
            return
        try:
            pid = int(_DASHBOARD_PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGINT)
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass
            _DASHBOARD_PID_FILE.unlink(missing_ok=True)
            print(f"Dashboard stopped (was PID {pid})")
        except (ProcessLookupError, ValueError, OSError) as exc:
            print(f"Dashboard stop failed: {exc}")
            _DASHBOARD_PID_FILE.unlink(missing_ok=True)

    elif parsed.action == "status":
        if _DASHBOARD_PID_FILE.exists():
            try:
                pid = int(_DASHBOARD_PID_FILE.read_text().strip())
                os.kill(pid, 0)
                print(f"Dashboard RUNNING (PID {pid}, http://localhost:{parsed.port})")
            except (ProcessLookupError, ValueError, OSError):
                print("Dashboard PID file exists but process is dead (stale PID file)")
                _DASHBOARD_PID_FILE.unlink(missing_ok=True)
        else:
            print("Dashboard not running")

def api_server_main() -> None:
    """Run the REST & WebSocket API server standalone."""
    import argparse
    import threading
    import sqlite3
    from pathlib import Path
    from config import get_config
    from infra.api_server import APIServer
    from save_pipeline import _crdt_agent_id

    parser = argparse.ArgumentParser(description="REST and WebSocket API server")
    parser.add_argument("--port", type=int, help="Port to run on (overrides config)")
    parser.add_argument("--host", help="Host to run on (overrides config)")
    parser.add_argument("--db", help="Path to memory.db (overrides config)")
    
    # Handle sys.argv correctly
    args_slice = sys.argv[2:] if len(sys.argv) > 1 and sys.argv[1] == "api" else sys.argv[1:]
    parsed = parser.parse_args(args_slice)

    db_path = parsed.db
    if not db_path:
        db_path = get_config().db_path

    db_path_obj = Path(db_path)
    if not db_path_obj.exists():
        db_path_obj.parent.mkdir(parents=True, exist_ok=True)
        from infra.db_migrations import run_schema_setup
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.close()

    cfg = get_config()
    host = parsed.host or getattr(cfg, "api_listen_host", "127.0.0.1")
    port = parsed.port or getattr(cfg, "api_listen_port", 9878)
    agent_id = _crdt_agent_id()

    server = APIServer(db_path=db_path, agent_id=agent_id, host=host, port=port)
    server.start()
    print(f"API server running on http://{host}:{port}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()


COMMANDS: dict[str, Callable[[], int | None]] = {
    "server": server_main,
    "search": search_main,
    "rebuild": rebuild_main,
    "backfill": backfill_main,
    "consolidate": consolidate_main,
    "integrity": integrity_main,
    "tier": tier_main,
    "compact": compact_main,
    "bootstrap": bootstrap_main,
    "worker": worker_main,
    "sync": sync_main,
    "init": init_main,
    "doctor": doctor_main,
    "status": status_main,
    "version": version_main,
    "install-mcp": install_mcp_main,
    "dashboard": dashboard_main,
    "api": api_server_main,
}


def main() -> NoReturn:
    if len(sys.argv) > 1:
        cmd = COMMANDS.get(sys.argv[1])
        if cmd:
            sys.exit(cmd() or 0)
        print(f"Unknown command: {sys.argv[1]}")
        print(f"Available: {', '.join(sorted(COMMANDS))}")
        sys.exit(1)
    server_main()
    sys.exit(0)


if __name__ == "__main__":
    main()
