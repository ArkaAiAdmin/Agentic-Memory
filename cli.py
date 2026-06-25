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
"""

from __future__ import annotations

import signal
import sys
import os
import subprocess
from typing import Callable, NoReturn

SCRIPTS: str = os.path.dirname(os.path.abspath(__file__))
PYTHON: str = sys.executable


def _run(script: str, args: list[str] | None = None) -> None:
    script_path: str = os.path.join(SCRIPTS, script)
    if not os.path.exists(script_path):
        script_path = os.path.join(SCRIPTS, "cron", script)
    cmd: list[str] = [PYTHON, script_path] + (args or [])
    subprocess.run(cmd, timeout=300)


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
    from infrastructure import resolve_active_memory_dir

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

    from sync_client import sync_once

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
