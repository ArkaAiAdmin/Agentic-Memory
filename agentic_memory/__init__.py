from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
"""Agentic Memory — Local-first persistent memory for AI agents.

This package is the canonical, pip-installable surface of the agentic-memory
system.

Quick start::

    from agentic_memory import MemoryClient

    mc = MemoryClient()
    mc.save("User prefers dark mode")
    results = mc.search("What does the user prefer?")

    # With agent scoping:
    from agentic_memory import AgentMemory

    am = AgentMemory(agent_id="coder-1")
    am.save("Frontend uses React with TypeScript")
    results = am.search("frontend")

CLI usage (after ``pip install -e .``)::

    agentic-memory search "user preferences"
    agentic-memory add "User prefers dark mode"
    agentic-memory kg stats
    agentic-memory temporal contradictions
    agentic-memory maintenance check
    agentic-memory agent list
    agentic-memory sync status

Module usage::

    python -m agentic_memory search "user preferences"

See Also:
    - ``agentic_memory.client`` — :class:`MemoryClient` (core SDK class).
    - ``agentic_memory.models`` — typed dataclasses.
    - ``agentic_memory.exceptions`` — typed exception hierarchy.
    - ``examples/`` — runnable example scripts.
"""

__version__ = "1.1.0"

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

_REPO_ROOT = _PKG_DIR.parent
if (_REPO_ROOT / "infra").exists() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# New SDK (typed, feature-complete)
from agentic_memory.client import MemoryClient  # noqa: E402,F401
from agentic_memory.models import (  # noqa: E402,F401
    MemoryResult,
    SearchResults,
    Entity,
    Relation,
    Fact,
    Stats,
    IntegrityReport,
    AgentInfo,
    MaintenanceResult,
)
from agentic_memory.exceptions import (  # noqa: E402,F401
    AgenticMemoryError,
    AgenticConnectionError,
    AgenticIntegrityError,
    AgenticPermissionError,
    NotFoundError,
    ValidationError,
    MaintenanceError,
    SyncError,
    CircuitBreakerOpen,
    ConfigError,
    ConnectionError,
    IntegrityError,
    PermissionError,
)
from agentic_memory.kg import KnowledgeGraph  # noqa: E402,F401
from agentic_memory.temporal import TemporalKG  # noqa: E402,F401
from agentic_memory.maintenance import Maintenance  # noqa: E402,F401
from agentic_memory.agent import AgentMemory  # noqa: E402,F401
from agentic_memory.sync import SyncManager  # noqa: E402,F401
from agentic_memory.admin import Admin  # noqa: E402,F401

# Legacy backward-compat aliases
from sdk import Memory  # noqa: E402,F401

__all__ = [
    # New SDK
    "MemoryClient",
    "MemoryResult",
    "SearchResults",
    "Entity",
    "Relation",
    "Fact",
    "Stats",
    "IntegrityReport",
    "AgentInfo",
    "MaintenanceResult",
    # Exceptions
    "AgenticMemoryError",
    "AgenticConnectionError",
    "AgenticIntegrityError",
    "AgenticPermissionError",
    "NotFoundError",
    "ValidationError",
    "MaintenanceError",
    "SyncError",
    "CircuitBreakerOpen",
    "ConfigError",
    # Backward-compat aliases (shadow builtins — prefer new names)
    "ConnectionError",
    "IntegrityError",
    "PermissionError",
    # P2 — Knowledge Graph
    "KnowledgeGraph",
    # P3 — Temporal KG
    "TemporalKG",
    # P4 — Maintenance
    "Maintenance",
    # P5 — Agent & Sync
    "AgentMemory",
    "SyncManager",
    # P4b — Admin
    "Admin",
    # Legacy backward-compat
    "Memory",
    "main",
]


def _json(obj: object) -> str:
    """Compact JSON serialization with dataclass support."""
    import dataclasses
    import json

    class _Encoder(json.JSONEncoder):
        def default(self, o: object) -> object:
            if dataclasses.is_dataclass(o) and not isinstance(o, type):
                fields = getattr(o, "__dataclass_fields__", {})
                return {n: getattr(o, n) for n in fields}
            try:
                return super().default(o)
            except TypeError:
                return str(o)

    return json.dumps(obj, indent=2, cls=_Encoder)


def _asdict(obj: object) -> dict:
    """Serialize a dataclass to a dict (avoids  LSP noise)."""
    if hasattr(obj, "__dataclass_fields__"):
        fields = getattr(obj, "__dataclass_fields__", {})
        return {n: getattr(obj, n) for n in fields}
    if isinstance(obj, dict):
        return obj
    return {"_value": obj}


def _init_mc() -> MemoryClient:
    """Initialise a default MemoryClient, handling startup errors."""
    try:
        return MemoryClient()
    except Exception as exc:
        print(
            f"agentic-memory: failed to initialise MemoryClient: {exc}", file=sys.stderr
        )
        raise SystemExit(2) from exc


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Provides a small CLI so the package is usable end-to-end without the
    MCP server. Dispatches to the typed SDK classes.

    Usage::

        agentic-memory add <text> [tags...]
        agentic-memory search <query> [--limit N]
        agentic-memory list [--limit N]
        agentic-memory stats
        agentic-memory clear
        agentic-memory demo [--query Q]

        agentic-memory kg search <query>
        agentic-memory temporal search <query>
        agentic-memory maintenance check
        agentic-memory agent list
        agentic-memory sync status
    """
    import argparse

    if argv is None:
        argv = sys.argv[1:]

    WELCOME = (
        "\033[1;36magentic-memory\033[0m — "
        "local-first persistent memory for AI agents\n"
        "  \033[90mdocs:\033[0m https://github.com/ArkaAiAdmin/Agentic-Memory\n"
        "  \033[90mrun:\033[0m   agentic-memory --help\n"
    )

    def _print_welcome() -> None:
        if sys.stdout.isatty():
            print(WELCOME, end="")

    if not argv or argv[0] in ("--help", "-h", "help"):
        _print_welcome()

    parser = argparse.ArgumentParser(
        prog="agentic-memory",
        description="Agentic Memory — local-first persistent memory for AI agents",
        epilog="Run 'agentic-memory <subcommand> --help' for more info.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── Core commands ──────────────────────────────────────────────────
    p_add = sub.add_parser("add", help="Save a new memory to the store")
    p_add.add_argument("text", help="Memory text to save")
    p_add.add_argument("tags", nargs="*", help="Optional tags")

    p_search = sub.add_parser("search", help="Full-text search across memories")
    p_search.add_argument("query", help="Query string")
    p_search.add_argument("--limit", type=int, default=5, help="Result limit")

    p_list = sub.add_parser("list", help="List recent memories")
    p_list.add_argument("--limit", type=int, default=10, help="How many to list")

    sub.add_parser("stats", help="Print database statistics")
    sub.add_parser("clear", help="Clear all SDK-created memories")

    p_demo = sub.add_parser("demo", help="Run a quick end-to-end demo")
    p_demo.add_argument(
        "--query",
        default="preferences",
        help="Query to search for at the end of the demo",
    )

    # ── Knowledge Graph ────────────────────────────────────────────────
    p_kg = sub.add_parser("kg", help="Knowledge graph: entities, relations, facts")
    kg_sub = p_kg.add_subparsers(dest="kg_cmd", required=True)

    kg_search = kg_sub.add_parser("search", help="Search KG entities by name or text")
    kg_search.add_argument("query", help="Entity search query")
    kg_search.add_argument("--limit", type=int, default=10)
    kg_search.add_argument("--max-hops", type=int, default=2)

    kg_facts = kg_sub.add_parser("facts", help="Search KG facts by text")
    kg_facts.add_argument("query", help="Fact search query")
    kg_facts.add_argument("--limit", type=int, default=10)

    kg_path = kg_sub.add_parser("path", help="Shortest path between two entities")
    kg_path.add_argument("source", help="Source entity name")
    kg_path.add_argument("target", help="Target entity name")
    kg_path.add_argument("--max-hops", type=int, default=5)

    kg_trav = kg_sub.add_parser(
        "traverse", help="Traverse KG edges from a start entity"
    )
    kg_trav.add_argument("start", help="Starting entity")
    kg_trav.add_argument("--max-hops", type=int, default=3)

    kg_sub.add_parser("stats", help="KG statistics: entity/edge/fact counts")

    kg_lf = kg_sub.add_parser("list-facts", help="List all facts (paginated)")
    kg_lf.add_argument("--limit", type=int, default=50)
    kg_lf.add_argument("--offset", type=int, default=0)

    # ── Temporal KG ────────────────────────────────────────────────────
    p_tmp = sub.add_parser(
        "temporal", help="Temporal KG: event-time facts and contradictions"
    )
    tmp_sub = p_tmp.add_subparsers(dest="temporal_cmd", required=True)

    tmp_search = tmp_sub.add_parser("search", help="Search temporal facts by text")
    tmp_search.add_argument("query", help="Fact text query")
    tmp_search.add_argument("--limit", type=int, default=10)

    tmp_contra = tmp_sub.add_parser("contradictions", help="List contradiction events")
    tmp_contra.add_argument("--since", type=float, default=None)
    tmp_contra.add_argument("--until", type=float, default=None)
    tmp_contra.add_argument("--reason", type=str, default=None)
    tmp_contra.add_argument("--limit", type=int, default=50)
    tmp_contra.add_argument("--offset", type=int, default=0)

    tmp_at = tmp_sub.add_parser(
        "at-time", help="Facts valid at a given epoch timestamp"
    )
    tmp_at.add_argument("timestamp", type=float, help="Epoch timestamp")
    tmp_at.add_argument("--query", type=str, default=None)
    tmp_at.add_argument("--limit", type=int, default=50)

    tmp_cs = tmp_sub.add_parser("changed-since", help="Facts changed since a timestamp")
    tmp_cs.add_argument("timestamp", type=float, help="Epoch timestamp")
    tmp_cs.add_argument("--limit", type=int, default=100)

    tmp_chain = tmp_sub.add_parser("chain", help="Walk supersession chain from a fact")
    tmp_chain.add_argument("fact_id", type=int, help="Fact ID to walk from")

    tmp_inv = tmp_sub.add_parser(
        "invalidate", help="Manually invalidate (supersede) a fact"
    )
    tmp_inv.add_argument("fact_id", type=int, help="Fact ID to invalidate")
    tmp_inv.add_argument("--reason", type=str, default="manual")

    # ── Maintenance ────────────────────────────────────────────────────
    p_maint = sub.add_parser(
        "maintenance", help="Maintenance: rebuild, compact, integrity checks"
    )
    maint_sub = p_maint.add_subparsers(dest="maint_cmd", required=True)

    m_rebuild = maint_sub.add_parser("rebuild", help="Rebuild FTS5 + vector index")
    m_rebuild.add_argument(
        "--scope", default="active", choices=("active", "local", "global")
    )

    m_compact = maint_sub.add_parser("compact", help="Run full compaction pipeline")
    m_compact.add_argument("--dry-run", action="store_true")

    m_check = maint_sub.add_parser("check", help="Check DB integrity (FTS5 + schema)")
    m_check.add_argument("--deep", action="store_true")

    maint_sub.add_parser("audit", help="Audit full memory system health")
    maint_sub.add_parser("heartbeat", help="Run self-directed heartbeat check")
    maint_sub.add_parser("tier-stats", help="Show tier distribution across memories")
    maint_sub.add_parser("tier-migrate", help="Run tier migration pass")
    maint_sub.add_parser("consolidate", help="Run fact consolidation and dedup")
    maint_sub.add_parser("rewrite-links", help="Rewrite broken markdown wiki-links")

    m_dc = maint_sub.add_parser(
        "detect-contradictions", help="Run contradiction detector across facts"
    )
    m_dc.add_argument(
        "--min-confidence", default="low", choices=("low", "medium", "high")
    )
    m_dc.add_argument("--mode", default="both", choices=("phrase", "semantic", "both"))
    m_dc.add_argument("--threshold", type=float, default=0.65)

    m_run = maint_sub.add_parser(
        "run", help="Run an arbitrary named maintenance operation"
    )
    m_run.add_argument("operation", help="Operation name (e.g. heartbeat, duplicates)")
    m_run.add_argument("args", nargs=argparse.REMAINDER, help="key=value arguments")

    # ── Admin ─────────────────────────────────────────────────────────
    p_adm = sub.add_parser(
        "admin", help="Admin operations: health, circuit breaker, sync"
    )
    adm_sub = p_adm.add_subparsers(dest="admin_cmd", required=True)

    adm_sub.add_parser("health", help="Per-table row counts and staleness report")
    adm_cb = adm_sub.add_parser("circuit-breaker", help="Circuit breaker event history")
    adm_cb.add_argument("--limit", type=int, default=20)
    adm_cb.add_argument("--since", type=float, default=None)

    # ── Agent ──────────────────────────────────────────────────────────
    p_agent = sub.add_parser("agent", help="Agent-scoped memory operations")
    agent_sub = p_agent.add_subparsers(dest="agent_cmd", required=True)

    agent_sub.add_parser("list", help="List registered agents")

    agent_info = agent_sub.add_parser("info", help="Show agent metadata")
    agent_info.add_argument("--agent-id", required=True, help="Agent identifier")

    agent_save = agent_sub.add_parser("save", help="Save a memory scoped to an agent")
    agent_save.add_argument("--agent-id", required=True, help="Agent identifier")
    agent_save.add_argument("text", help="Memory text to save")
    agent_save.add_argument("--tags", nargs="*", default=[])
    agent_save.add_argument("--category", default="agents")

    agent_search = agent_sub.add_parser("search", help="Search agent-scoped memories")
    agent_search.add_argument("--agent-id", required=True, help="Agent identifier")
    agent_search.add_argument("query", help="Search query")
    agent_search.add_argument("--limit", type=int, default=10)

    agent_list = agent_sub.add_parser(
        "list-memories", help="List agent-scoped memories"
    )
    agent_list.add_argument("--agent-id", required=True, help="Agent identifier")
    agent_list.add_argument("--limit", type=int, default=50)

    agent_clear = agent_sub.add_parser("clear", help="Clear all memories for an agent")
    agent_clear.add_argument("--agent-id", required=True, help="Agent identifier")

    # ── Sync ───────────────────────────────────────────────────────────
    p_sync = sub.add_parser("sync", help="Sync & sharing operations")
    sync_sub = p_sync.add_subparsers(dest="sync_cmd", required=True)

    sync_sub.add_parser("status", help="Show sync status for all peers")

    sync_share = sync_sub.add_parser("share", help="Share a memory with another agent")
    sync_share.add_argument("note_id", help="Note ID to share")
    sync_share.add_argument("agent_id", help="Target agent identifier")

    sync_ls = sync_sub.add_parser("list-shared", help="List shared memories")
    sync_ls.add_argument("--agent-id", default="")
    sync_ls.add_argument("--category", default="")
    sync_ls.add_argument("--limit", type=int, default=50)

    sync_import = sync_sub.add_parser("import", help="Import a memory shared with you")
    sync_import.add_argument("shared_id", help="Shared memory ID")
    sync_import.add_argument("target_agent_id", help="Agent to import into")

    sync_as = sync_sub.add_parser(
        "auto-share", help="Auto-share high-importance memories"
    )
    sync_as.add_argument("--agent-id", default="")
    sync_as.add_argument("--min-importance", type=int, default=0)
    sync_as.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)

    # ── Dispatch ───────────────────────────────────────────────────────

    # Core commands
    if args.cmd in ("add", "search", "list", "stats", "clear", "demo"):
        mc = _init_mc()
        if args.cmd == "add":
            note_id = mc.save(args.text, tags=args.tags)
            print(_json({"note_id": note_id}))
            return 0
        if args.cmd == "search":
            results = mc.search(args.query, limit=args.limit)
            print(
                _json(
                    {
                        "results": [r for r in results.results],
                        "total": results.total,
                        "synthesis": results.synthesis,
                        "query": results.query,
                    }
                )
            )
            return 0
        if args.cmd == "list":
            notes = mc.list(limit=args.limit)
            print(_json({"count": len(notes), "notes": [r for r in notes]}))
            return 0
        if args.cmd == "stats":
            s = mc.stats()
            print(_json(s))
            return 0
        if args.cmd == "clear":
            n = mc.clear()
            print(_json({"cleared": n}))
            return 0
        if args.cmd == "demo":
            return _run_demo(args.query)

    # KG
    if args.cmd == "kg":
        kg = KnowledgeGraph()
        try:
            if args.kg_cmd == "search":
                ents = kg.search(args.query, limit=args.limit, max_hops=args.max_hops)
                print(_json([e for e in ents]))
            elif args.kg_cmd == "facts":
                facts = kg.search_facts(args.query, limit=args.limit)
                print(_json([f for f in facts]))
            elif args.kg_cmd == "path":
                path = kg.shortest_path(
                    args.source, args.target, max_hops=args.max_hops
                )
                print(_json([r for r in path]))
            elif args.kg_cmd == "traverse":
                ents, rels = kg.traverse(args.start, max_hops=args.max_hops)
                print(
                    _json(
                        {
                            "entities": [e for e in ents],
                            "relations": [r for r in rels],
                        }
                    )
                )
            elif args.kg_cmd == "stats":
                print(_json(kg.stats()))
            elif args.kg_cmd == "list-facts":
                facts = kg.list_facts(limit=args.limit, offset=args.offset)
                print(_json([f for f in facts]))
            return 0
        except Exception as exc:
            print(f"kg {args.kg_cmd}: {exc}", file=sys.stderr)
            return 1

    # Temporal
    if args.cmd == "temporal":
        tk = TemporalKG()
        try:
            if args.temporal_cmd == "search":
                facts = tk.search(args.query, limit=args.limit)
                print(_json([f for f in facts]))
            elif args.temporal_cmd == "contradictions":
                events = tk.contradictions(
                    since_ts=args.since,
                    until_ts=args.until,
                    reason=args.reason,
                    limit=args.limit,
                    offset=args.offset,
                )
                print(_json(events))
            elif args.temporal_cmd == "at-time":
                facts = tk.query_facts_at_time(
                    args.timestamp,
                    query=args.query,
                    limit=args.limit,
                )
                print(_json([f for f in facts]))
            elif args.temporal_cmd == "changed-since":
                facts = tk.query_changed_since(args.timestamp, limit=args.limit)
                print(_json([f for f in facts]))
            elif args.temporal_cmd == "chain":
                facts = tk.query_supersession_chain(args.fact_id)
                print(_json([f for f in facts]))
            elif args.temporal_cmd == "invalidate":
                ok = tk.invalidate_fact(args.fact_id, reason=args.reason)
                print(_json({"ok": ok, "fact_id": args.fact_id, "reason": args.reason}))
            return 0
        except Exception as exc:
            print(f"temporal {args.temporal_cmd}: {exc}", file=sys.stderr)
            return 1

    # Maintenance
    if args.cmd == "maintenance":
        m = Maintenance()
        try:
            if args.maint_cmd == "rebuild":
                result: Any = m.rebuild(scope=args.scope)
                print(_json(result if hasattr(result, "_asdict") else result))
            elif args.maint_cmd == "compact":
                result = m.compact(dry_run=args.dry_run)
                print(_json(result if hasattr(result, "_asdict") else result))
            elif args.maint_cmd == "check":
                report = m.check_integrity(deep=args.deep)
                print(_json(report if hasattr(report, "_asdict") else report))
            elif args.maint_cmd == "audit":
                print(_json(m.audit()))
            elif args.maint_cmd == "heartbeat":
                print(_json(m.heartbeat()))
            elif args.maint_cmd == "tier-stats":
                print(_json(m.tier_stats()))
            elif args.maint_cmd == "tier-migrate":
                print(_json({"result": m.run_tier_migration()}))
            elif args.maint_cmd == "consolidate":
                result = m.consolidate()
                print(_json(result if hasattr(result, "_asdict") else result))
            elif args.maint_cmd == "rewrite-links":
                result = m.rewrite_links()
                print(_json(result if hasattr(result, "_asdict") else result))
            elif args.maint_cmd == "detect-contradictions":
                result = m.detect_contradictions(
                    min_confidence=args.min_confidence,
                    mode=args.mode,
                    semantic_threshold=args.threshold,
                )
                print(_json(result))
            elif args.maint_cmd == "run":
                kwargs = {}
                for a in args.args:
                    if "=" in a:
                        k, v = a.split("=", 1)
                        kwargs[k] = v
                result = m.run(args.operation, **kwargs)
                print(_json({"operation": args.operation, "result": result}))
            return 0
        except Exception as exc:
            print(f"maintenance {args.maint_cmd}: {exc}", file=sys.stderr)
            return 1

    # Admin
    if args.cmd == "admin":
        adm = Admin()
        try:
            if args.admin_cmd == "health":
                print(_json(adm.health()))
            elif args.admin_cmd == "circuit-breaker":
                print(
                    _json(
                        adm.circuit_breaker_status(
                            limit=args.limit, since_ts=args.since
                        )
                    )
                )
            return 0
        except Exception as exc:
            print(f"admin {args.admin_cmd}: {exc}", file=sys.stderr)
            return 1

    # Agent
    if args.cmd == "agent":
        try:
            if args.agent_cmd == "list":
                agents = AgentMemory.list_agents()
                print(_json([a for a in agents]))
            elif args.agent_cmd == "info":
                am = AgentMemory(agent_id=args.agent_id)
                print(_json(am.info))
            elif args.agent_cmd == "save":
                am = AgentMemory(agent_id=args.agent_id)
                note_id = am.save(args.text, category=args.category, tags=args.tags)
                print(_json({"note_id": note_id}))
            elif args.agent_cmd == "search":
                am = AgentMemory(agent_id=args.agent_id)
                results = am.search(args.query, limit=args.limit)
                print(
                    _json(
                        {
                            "results": [r for r in results.results],
                            "total": results.total,
                            "query": results.query,
                        }
                    )
                )
            elif args.agent_cmd == "list-memories":
                am = AgentMemory(agent_id=args.agent_id)
                notes = am.list(limit=args.limit)
                print(_json({"count": len(notes), "notes": [r for r in notes]}))
            elif args.agent_cmd == "clear":
                am = AgentMemory(agent_id=args.agent_id)
                n = am.clear()
                print(_json({"cleared": n}))
            return 0
        except Exception as exc:
            print(f"agent {args.agent_cmd}: {exc}", file=sys.stderr)
            return 1

    # Sync
    if args.cmd == "sync":
        sm = SyncManager()
        try:
            if args.sync_cmd == "status":
                print(_json(sm.status()))
            elif args.sync_cmd == "share":
                ok = sm.share(args.note_id, args.agent_id)
                print(_json({"ok": ok}))
            elif args.sync_cmd == "list-shared":
                items = sm.list_shared(
                    agent_id=args.agent_id,
                    category=args.category,
                    limit=args.limit,
                )
                print(_json(items))
            elif args.sync_cmd == "import":
                ok = sm.import_shared(args.shared_id, args.target_agent_id)
                print(_json({"ok": ok}))
            elif args.sync_cmd == "auto-share":
                result = sm.auto_share(
                    agent_id=args.agent_id,
                    min_importance=args.min_importance,
                    dry_run=args.dry_run,
                )
                print(_json(result))
            return 0
        except Exception as exc:
            print(f"sync {args.sync_cmd}: {exc}", file=sys.stderr)
            return 1

    parser.print_help()
    _print_welcome()
    return 1


def _run_demo(query: str) -> int:
    """Quick end-to-end demo: save a few notes, search, print results.

    This is the canonical "hello world" for the SDK. It is the
    implementation backing the `memory_sdk_demo` MCP tool.
    """
    mc = _init_mc()
    samples = [
        ("User prefers dark mode in all editors.", ["preferences", "ui"]),
        ("User is learning Rust and building a CLI tool.", ["learning", "rust"]),
        ("Project uses PostgreSQL with the pgvector extension.", ["database"]),
    ]
    saved = []
    for text, tags in samples:
        try:
            note_id = mc.save(text, tags=tags)
            saved.append({"text": text, "note_id": note_id})
        except Exception as exc:
            logger.warning("_run_demo failed: %s", exc)
            saved.append({"text": text, "error": str(exc)})

    try:
        results: Any = mc.search(query, limit=5)
    except Exception as exc:
        logger.warning("_run_demo failed: %s", exc)
        results = {"error": str(exc)}

    try:
        s = mc.stats()
        stats: Any = s if hasattr(s, "_asdict") else str(s)
    except Exception as exc:
        logger.warning("_run_demo failed: %s", exc)
        stats = {"error": str(exc)}

    out = {
        "saved": saved,
        "search_query": query,
        "results": results if hasattr(results, "_asdict") else results,
        "stats": stats,
    }
    import json as _json

    print(_json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
