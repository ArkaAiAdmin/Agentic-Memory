#!/usr/bin/env python3
from __future__ import annotations

import logging
logger = logging.getLogger(__name__)
"""Proactive context hook — searches memory before tool execution.

Usage: Called as PreToolUse hook. Extracts query from tool input,
searches memory, prints relevant context to stdout for agent consumption.

Input: JSON from stdin with tool_name, tool_input
Output: Relevant memory context to stdout

Hooks that do the same thing: the on-demand and session-start hooks also call ``search_memories``. Querying is performed directly on every hook execution as SQLite query execution is fast.
"""

import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Any, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

# Make the sibling _log_error.py importable (same dir as this hook)
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _log_error import log_error
except Exception:

    def log_error(exc: BaseException, context: str = "") -> None:  # type: ignore[misc]
        import sys

        print(f"logger error: {exc} context={context}", file=sys.stderr)


# I10 fix (2026-06-22): import directly from search.orchestrator
# (the canonical source) instead of going through search_pipeline's
# re-export chain. The chain silently hid signature changes
# (see B1 in the contradiction report).
try:
    from search.orchestrator import search_memories  # noqa: E402
    from infra.memory_common import get_memory_paths  # noqa: E402
except Exception as import_err:
    logger.warning("operation failed: %s", import_err)
    log_error(import_err, context="memory-proactive-context.imports")

    # Define stubs so python can still compile and run main() without crashing
    def search_memories(*args: Any, **kwargs: Any) -> dict:  # type: ignore[misc]
        return {}

    def get_memory_paths(*args: Any, **kwargs: Any) -> tuple[Path, Path, Path]:  # type: ignore[misc]
        cwd = Path.cwd()
        return cwd, cwd / "memory", cwd / "memory"


# 2026-06-23: Shared per-process dedup cache (cross-hook, in-memory)
# and subprocess-level file-backed cache (cross-invocation, disk-backed).
# CLI hooks run as transient subprocesses, so process-local state does
# not survive across invocations. The file-backed cache short-circuits
# repeated queries within a 60 s window; the in-memory cache avoids
# duplicate work from sibling hooks in the same process.

_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SEARCH_CACHE_MAX = int(os.environ.get("MEMORY_HOOK_CACHE_SIZE", "5"))
_SEARCH_CACHE_TTL = float(os.environ.get("MEMORY_HOOK_CACHE_TTL", "300"))


def _cache_get(query: str) -> list[dict] | None:
    entry = _SEARCH_CACHE.get(query)
    if entry is None:
        return None
    ts, results = entry
    if (time.time() - ts) > _SEARCH_CACHE_TTL:
        _SEARCH_CACHE.pop(query, None)
        return None
    return results


def _cache_put(query: str, results: list[dict]) -> None:
    _SEARCH_CACHE[query] = (time.time(), results)
    while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
        oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
        _SEARCH_CACHE.pop(oldest, None)


_CACHE_TTL_SECONDS = 60
_CACHE_MAX_ENTRIES = int(os.environ.get("MEMORY_HOOK_FILE_CACHE_SIZE", "20"))


def _get_cache_file() -> Path:
    _, local_mem, _ = get_memory_paths()
    return local_mem / "hook_cache.json"


def _load_file_cache(cache_file: Path) -> dict:
    if not cache_file.exists():
        return {}
    try:
        raw = json.loads(cache_file.read_text())
        if isinstance(raw, dict):
            return raw
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_file_cache(cache_file: Path, cache: dict) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, separators=(",", ":")))
    except OSError:
        pass


def _file_cache_get(
    query_hash: str, cache_file: Path, db_mtime: float
) -> list[dict] | None:
    cache = _load_file_cache(cache_file)
    entry = cache.get(query_hash)
    if entry is None:
        return None
    ts, result_set, entry_mtime = entry
    if entry_mtime != db_mtime or (time.time() - ts) > _CACHE_TTL_SECONDS:
        return None
    return result_set if isinstance(result_set, list) else None


def _file_cache_put(
    query_hash: str, result_set: list, cache_file: Path, db_mtime: float
) -> None:
    cache = _load_file_cache(cache_file)
    cache[query_hash] = (time.time(), result_set, db_mtime)
    while len(cache) > _CACHE_MAX_ENTRIES:
        oldest_key = min(cache, key=lambda k: cache[k][0])
        cache.pop(oldest_key, None)
    _save_file_cache(cache_file, cache)


def _temporal_kg_alert() -> None:
    """Phase 4 heuristic: surface temporal-KG misbehavior warning.

    If ``MEMORY_TEMPORAL_KG`` is enabled and the DB has a large number
    of invalidated/superseded ``kg_facts`` (relative to total facts),
    the agent should consider setting ``MEMORY_TEMPORAL_KG=0`` as the
    escape hatch (see AGENTS.md Rule #12).

    No-op if the DB doesn't exist, lacks ``kg_facts``, or the
    invalidated ratio looks sane.
    """
    try:
        if os.environ.get("MEMORY_TEMPORAL_KG", "1") == "0":
            return
        _, local_mem, _ = get_memory_paths()
        db_path = local_mem / "memory.db"
        if not db_path.exists():
            return

        import sqlite3

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
            invalid = conn.execute(
                "SELECT COUNT(*) FROM kg_facts WHERE valid_to IS NOT NULL "
                "OR superseded_by IS NOT NULL"
            ).fetchone()[0]

        if total >= 50 and invalid / max(total, 1) > 0.5:
            print("=== TEMPORAL-KG WARNING ===")
            print(
                f"  {invalid}/{total} kg_facts ({invalid * 100 // max(total, 1)}%)"
                " are invalidated or superseded. This may indicate"
                " overly-aggressive contradiction detection or edit"
                " invalidation. Consider setting MEMORY_TEMPORAL_KG=0."
            )
            print("=== END TEMPORAL-KG WARNING ===")
    except Exception as e:
        logger.warning("_temporal_kg_alert failed: %s", e)


def _health_alerts() -> None:
    """Surface health alerts from cron_health_check.py (Rules #9-11).

    Reads ``.health_status.json`` written by the cron job and prints
    any active alerts to STDOUT so the agent sees them as context.
    No-op if the file doesn't exist or is unreadable.
    """
    try:
        from infra.memory_config import GLOBAL_MEM_DIR

        health_file = GLOBAL_MEM_DIR / ".health_status.json"
        if not health_file.exists():
            return
        try:
            data = json.loads(health_file.read_text())
        except (json.JSONDecodeError, OSError):
            return

        alerts = data.get("alerts", [])
        if not alerts:
            return

        ts = data.get("timestamp", "unknown")
        print(f"=== HEALTH ALERTS (as of {ts}) ===")
        for a in alerts[:5]:
            print(f"  ! {a}")
        if len(alerts) > 5:
            print(f"  ... and {len(alerts) - 5} more (see .health_status.json)")
        print("=== END HEALTH ALERTS ===")
    except Exception as e:
        # Never block the hook — health alerts are best-effort
        logger.warning("_health_alerts failed: %s", e)

    # Phase 4 temporal-KG heuristic (outside the health-file try block
    # so one failure doesn't suppress the other).
    _temporal_kg_alert()


def extract_query_from_tool_input(tool_name: str, tool_input: Any) -> str:
    """Extract searchable query from various tool inputs.

    For read/write tools: uses filePath or pattern as context signal.
    For bash: uses the command string.
    For grep/glob: uses the pattern.
    """
    if not isinstance(tool_input, dict):
        if isinstance(tool_input, str) and len(tool_input) > 3:
            return tool_input[:300]
        return ""

    # Common fields across all tools
    for field in [
        "query",
        "prompt",
        "task",
        "description",
        "message",
        "content",
        "search",
        "question",
        "goal",
        "objective",
        "brief",
    ]:
        val = tool_input.get(field)
        if isinstance(val, str) and len(val) > 3:
            return val[:300]

    # Support for parameters used by Antigravity and Gemini-based agents
    for path_field in ["TargetFile", "AbsolutePath", "SearchPath", "filePath", "path"]:
        fp = tool_input.get(path_field)
        if isinstance(fp, str) and len(fp) > 3:
            return f"memory about {fp}"[:300]

    # Bash / Command line
    cmd = tool_input.get("CommandLine") or tool_input.get("command") or ""
    if isinstance(cmd, str) and len(cmd) > 3:
        return cmd[:300]

    # Grep/Glob: use pattern
    pat = tool_input.get("pattern") or ""
    if isinstance(pat, str) and len(pat) > 3:
        return pat[:300]

    return ""


def main(db_path: Path | None = None):
    try:
        if db_path is None:
            _, local_mem, _ = get_memory_paths()
            db_path = local_mem / "memory.db"

        # Read hook input from stdin, or fall back to CLI args
        hook_data = {}
        # I9 fix (2026-06-22): narrow the except clause so
        # SystemExit / KeyboardInterrupt aren't swallowed.
        try:
            stdin_data = sys.stdin.read()
            if stdin_data.strip():
                hook_data = json.loads(stdin_data)
        except (json.JSONDecodeError, ValueError, OSError):
            hook_data = {}
        if not hook_data:
            hook_data = {
                "tool_name": os.environ.get("MEMORY_TOOL_NAME", ""),
                "tool_input": {},
            }
        tool_name = hook_data.get("tool_name", "")
        tool_input = hook_data.get("tool_input", {})

        # I3 fix (2026-06-22): populate skip_tools from env so
        # operators can suppress the hook for noisy tools without
        # editing the source. Default is the empty set — AGENTS.md
        # says the hook fires for ALL tools intentionally. Use
        # MEMORY_HOOK_SKIP_TOOLS="mcp_ListMcpResourcesTool,TodoWrite"
        # to opt out of specific tools (comma-separated).
        skip_tools: Set[str] = set(
            t.strip()
            for t in os.environ.get("MEMORY_HOOK_SKIP_TOOLS", "").split(",")
            if t.strip()
        )
        if tool_name in skip_tools:
            return

        # Extract query
        query = extract_query_from_tool_input(tool_name, tool_input)
        if not query or len(query) < 3:
            return

        # Search memory
        if not db_path.exists():
            return

        # I7 fix (2026-06-22): read result limit from
        # MEMORY_HOOK_RESULT_LIMIT env var (default 3, matching
        # the previous hardcoded value).
        try:
            limit = int(os.environ.get("MEMORY_HOOK_RESULT_LIMIT", "3"))
        except ValueError:
            limit = 3

        cache_file = _get_cache_file()
        db_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
        query_hash = hashlib.md5(query.encode()).hexdigest()
        cached_items = _file_cache_get(query_hash, cache_file, db_mtime)
        if cached_items is not None:
            items = cached_items
        else:
            in_mem = _cache_get(query)
            if in_mem is not None:
                items = in_mem
            else:
                results = search_memories(
                    db_path=db_path,
                    query=query,
                    limit=limit,
                    include_global=True,
                    light=True,
                )
                items = results.get("results", [])
                if not isinstance(items, list):
                    items = []
                _cache_put(query, items)
                _file_cache_put(query_hash, items, cache_file, db_mtime)

        # Output relevant context
        if items:
            # Write to STDOUT so the agent's tool-execution context
            # receives the proactive context. Writing to stderr was
            # a bug — stderr is for diagnostics, the agent reads stdout.
            print("=== PROACTIVE MEMORY CONTEXT ===")
            for i, r in enumerate(items, 1):
                content = r.get("content", "")[:300]
                score = r.get("final_score", 0)
                print(f"[{i}] {r.get('id')} (score: {score:.2f})")
                print(f"    {content}...")
            print("=== END CONTEXT ===")

        # Phase 4 (2026-06-25): surface health alerts from cron_health_check
        # so the agent sees FTS drift / KG orphan / circuit breaker alerts
        # without having to investigate first (Rules #9-11).
        _health_alerts()

    except Exception as e:
        # Never block tool execution, but record the failure so ops
        # can see it (see lessons/hook-silent-failures-design-debt-2026-06-16)
        logger.warning("main failed: %s", e)
        log_error(e, context="memory-proactive-context.main()")


if __name__ == "__main__":
    # 2026-06-19 resilience: wrap the entire main() in a top-level
    # try/except so a failure in main() (including import errors that
    # slipped past the local sys.path fix) does NOT propagate as a
    # non-zero exit code. The hook is best-effort: a failure must
    # NEVER block tool execution, so we swallow and log.
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _hook_e:  # noqa: BLE001 — belt-and-suspenders
        try:
            from _log_error import log_error as _le

            _le(_hook_e, context="memory-proactive-context.top_level")
        except Exception:
            # No logger available; print to stderr and exit 0.
            import sys as _sys

            print(f"hook fatal: {_hook_e}", file=_sys.stderr)
        # Force exit 0 so the harness doesn't see this as a hook failure.
        import sys as _sys

        _sys.exit(0)
