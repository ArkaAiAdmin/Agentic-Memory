#!/usr/bin/env python3
"""Generate docs/architecture.md from live codebase inspection.

Sources:
  - Phase count      : search/orchestrator.py Phase comments
  - Tool counts      : tool_registry.py CORE_TOOLS + ADMIN_TOOLS
  - Schema version   : migration_runner.py SCHEMA_VERSION
  - Cron count       : cron/ directory
  - Hook count       : hooks/ directory (excluding _log_error.py)

Usage:
    python scripts/generate_architecture_md.py          # write in-place
    python scripts/generate_architecture_md.py --check  # CI: fail on mismatch
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# scripts/ runs from repo root for imports to work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

def count_search_phases() -> tuple[int, list[str], list[str]]:
    """Return (unique_phase_count, phase_numbers, phase_names).

    Source: orchestrator.py docstring (the canonical 12-phase pipeline).
    """
    path = Path("search/orchestrator.py")
    content = path.read_text()

    # Parse docstring header: """N-phase hybrid search orchestrator...
    header = re.search(r'"""(\d+)-phase\s', content)
    if not header:
        return 12, [], []

    expected_count = int(header.group(1))

    # Parse docstring phase entries: "Phase N — description"
    phases: list[tuple[str, str]] = []
    for line in content.splitlines()[:40]:
        m = re.match(r"\s*Phase\s+(\d+)\s*[—–-]\s+(.+)", line)
        if m:
            num = m.group(1)
            name = m.group(2).strip().rstrip(".")
            phases.append((num, name))

    if phases:
        phase_nums = [n for n, _ in phases]
        phase_names = [name for _, name in phases]
        return len(phases), phase_nums, phase_names

    return expected_count, [], []


def count_tools() -> dict[str, int]:
    from tool_registry import CORE_TOOLS, ADMIN_TOOLS
    return {
        "total": len(CORE_TOOLS) + len(ADMIN_TOOLS),
        "core": len(CORE_TOOLS),
        "admin": len(ADMIN_TOOLS),
    }


def count_schema_version() -> int:
    from infra.migration_runner import SCHEMA_VERSION
    return SCHEMA_VERSION


def count_migrations() -> int:
    """Count numbered migration pairs (each must have a .down.sql)."""
    return len(list(Path("migrations").glob("*.down.sql")))


def count_cron_scripts() -> int:
    return len(list(Path("cron").glob("cron_*.py")))


def count_hooks() -> int:
    return len([
        f for f in Path("hooks").glob("memory-*.py")
        if f.name != "_log_error.py"
    ])


def count_core_save_steps() -> int:
    content = Path("docs/architecture.md").read_text()
    m = re.search(r"The canonical write path.*?(\d+) steps in order:", content, re.DOTALL)
    return int(m.group(1)) if m else 13


# ---------------------------------------------------------------------------
# Phase names list builder (keeps names, drops phase numbers)
# ---------------------------------------------------------------------------

PHASE_TRANSITIONS: list[tuple[str, str]] = [
    ("Parse query", "Parse query"),
    ("Skill-first lookup", "Skill-first"),
    ("Cache check", "Cache"),
    ("DB setup", "DB setup"),
    ("FTS search", "FTS5"),
    ("KG fact search", "KG facts"),
    ("Fallback to embeddings", "Embedding"),
    ("Hybrid fusion", "Hybrid fusion"),
    ("Temporal filtering", "Temporal filter"),
    ("Chunk enhancement", "Chunk enhance"),
    ("Reranking", "Rerank"),
    ("Build output", "Output build"),
    ("Safety demoting", "Safety demoting"),
    ("Quality gates", "Quality gates"),
    ("User profiling", "User profile boost"),
    ("Record access", "Record access"),
]

# ---------------------------------------------------------------------------
# Document generator
# ---------------------------------------------------------------------------

def generate_doc() -> str:
    tools = count_tools()
    version = count_schema_version()
    n_migrations = count_migrations()
    n_cron = count_cron_scripts()
    n_hooks = count_hooks()
    n_phases, phase_nums, phase_names = count_search_phases()

    save_steps = count_core_save_steps()

    # Build phase arrow from actual names if available
    if phase_names:
        phase_arrow = " → ".join(phase_names)
    else:
        phase_arrow = " → ".join(
            name for _, name in PHASE_TRANSITIONS[:n_phases]
        )

    doc = f"""# Architecture

Agentic Memory is a local-first, markdown-primary persistent memory system for AI agents.

## Core Principles

1. **Markdown is source of truth for user content** — the body and
   frontmatter of each `.md` note is the authoritative copy.  The SQLite
   database is *mostly* derivable from markdown (FTS5, embeddings,
   chunks) but some relational metadata exists ONLY in SQLite:
   CRDT version vectors, KG edges, access logs, concept drift metrics,
   ARC state, the task queue, the sync log, and the shared memory pool.
   A full rebuild (`backfill_all.py --full`) logs a warning listing
   these unrecoverable tables.
2. **One-directional data flow** — markdown → index, never reversed
3. **No LLM in the write path** — deterministic extraction only
4. **Graceful degradation** — works without any process running
5. **Local-first** — all data stays on your machine

## Data Flow

```
User/Agent
    │
    ▼
┌─────────────┐
│ Save Pipeline │
│  (save_*)    │
└──────┬──────┘
       │
       ├──▶ Markdown files (source of truth)
       ├──▶ SQLite FTS5 index (full-text search)
       ├──▶ Knowledge graph (entities + relations)
       ├──▶ Vector embeddings (optional)
       └──▶ Background tasks (async processing)
```

## Search Pipeline

The search orchestrator (`search_memories` in `search/orchestrator.py`)
runs the following **{n_phases} phases** in order:

> **Pipeline flow:** {phase_arrow}

{_numbered_list(phase_names if phase_names else [name for _, name in PHASE_TRANSITIONS[:n_phases]])}

## Save Pipeline

The canonical write path (`save_memory` → `_upsert_memory_row` +
`_run_post_save_hooks`) runs the following **{save_steps} steps** in order:

1. Lock acquire
2. Compute tier + PRAGMA setup
3. Upsert memory row (DB + tier inline)
4. CRDT version bump (legacy; gated by `legacy_note_crdt` flag)
5. Index backlinks (wiki-style)
6. Index chunks
7. Index embedding
8. Index KG (entities + edges)
9. Index facts (SPO triples)
10. Auto semantic backlinks (FTS overlap)
11. Auto FTS backlinks
12. Adaptive retention index
13. Enrich context + commit + post-hooks (fitness recalc + background tasks)

## Module Map

### Package Structure

```
agentic-memory/                    # Repo root
├── agentic_memory/                # Python package (pip installable; 2 files)
│   ├── __init__.py                 # Re-exports Memory, AgentMemory, main
│   └── __main__.py                 # python -m agentic_memory
├── cli.py                          # 11 CLI entry points
├── memory_mcp.py                   # MCP server (thin orchestrator)
├── save_pipeline.py                # Write path shim → save/
├── save/                           # Write path subpackage
│   ├── __init__.py                 # Public API
│   ├── crdt_helpers.py             # CRDT snapshot extraction
│   ├── indexers.py                 # FTS/embedding/chunk index writes
│   ├── backlinks.py                # Auto-backlink computation
│   └── post_save_hooks.py          # Fitness recalc, tier, audit
├── search_pipeline.py              # Read path shim → search/
├── search/                         # Read path subpackage
│   ├── __init__.py                 # Public API
│   ├── query_parser.py             # Query type detection, expansion, FTS
│   ├── rerankers.py                # Cross-encoder, late interaction
│   ├── scoring.py                  # RRF fusion, temporal decay, CTR
│   ├── synthesis.py                # BB1/BB2 synthesis
│   ├── chunk_index.py              # Chunk search, Graph-RAG expansion
│   ├── instrumentation.py          # Timing/log/observability
│   └── orchestrator.py             # search_memories + {len(phase_names)}-phase search
├── backfill_all.py                 # Audit pipeline shim → backfill/
├── backfill/                       # Audit pipeline subpackage
│   ├── __init__.py                 # Public API
│   ├── index_backfills.py          # FTS, embedding, chunk, vec backfills
│   └── kg_backfills.py             # KG facts, entity filter
├── auto_save.py                    # Shim + CLI entry; impl in background/auto_save.py
├── background/                     # Auto-save + worker
│   ├── auto_save.py                # Core auto-save logic
│   ├── background_worker.py        # Task queue worker (flock-protected)
│   └── ...
├── knowledge_graph.py              # Entity extraction
├── fact_extraction.py              # SPO triple extraction
├── kg_dedup.py                     # Entity deduplication
├── contradiction_detector.py       # Conflict detection
├── background_queue.py             # SQLite-backed task queue
├── embedding_search.py             # Semantic search via model2vec
├── memory_common.py                # Shared utilities (connection pool, flock)
├── db.py                           # Connection pool with tenant routing
├── migration_runner.py             # Schema migrations (current v{version})
└── ... ({_module_count()} modules total)
```

| Module | Layer | Purpose |
|--------|-------|---------|
| `save_pipeline.py` + `save/` | Write | Orchestrates markdown → index writes |
| `search_pipeline.py` + `search/` | Read | BM25 + vector + KG hybrid search |
| `backfill_all.py` + `backfill/` | Maintenance | Audit pipeline for index rebuilds |
| `auto_save.py` + `background/` | Hook | Async inbox + daemon auto-save |
| `knowledge_graph.py` | Write | Pattern-based NER, entity storage |
| `fact_extraction.py` | Write | Regex-based SPO triple extraction |
| `kg_dedup.py` | Maintenance | Exact + semantic entity dedup |
| `contradiction_detector.py` | Quality | Conflicting fact detection |
| `background_queue.py` | Infra | SQLite-backed async task queue |
| `background_worker.py` | Infra | Task queue worker (flock-protected) |
| `embedding_search.py` | Search | model2vec semantic search |
| `memory_injection.py` | Safety | Prompt injection detection |
| `migration_runner.py` | Infra | Schema migrations (v{version}, {n_migrations} migrations) |

## Surface: MCP tools, cron jobs, hooks

- **{tools["total"]} MCP tools** ({tools["core"]} CORE + {tools["admin"]} ADMIN).
  Single source of truth: `tool_registry.py`.
- **{n_cron} cron scripts** in `cron/` — task queue, FTS rebuild, tier migration,
  kg backfill, integrity check, heartbeat, consolidation, etc.
  Cadence: `*/15 min`. Each cron acquires a `flock` before running.
- **{n_hooks} lifecycle hooks** in `hooks/` — session start/end,
  precompact snapshot, proactive context, recall,
  search-on-demand. See `~/.claude/settings.json` and `opencode.jsonc` for wiring.
  `_log_error.py` is a log helper, not a lifecycle hook.

## Concurrency Model

- **Single-writer**: SQLite handles write serialization via `BEGIN IMMEDIATE`
- **Multiple readers**: WAL mode allows concurrent reads during writes
- **Background tasks**: `BEGIN IMMEDIATE` prevents double-dequeue
- **No external dependencies**: No Redis, no message queues, no daemons required
  (the optional background daemon is graceful-degradable)

## Feature Flags

See `memory.toml [features]` for all flags. Key defaults:

| Flag | Default | Purpose |
|------|---------|---------|
| `crdt_enabled` | `true` | Version vector tracking + conflict resolution |
| `legacy_note_crdt` | `false` | Legacy note-level VV bump (deprecated; per-field CRDT is source of truth) |
| `temporal_tiers` | `true` | Hot/warm/cold tier management |
| `adaptive_retention` | `true` | Psi formula + spaced repetition |
| `feature_temporal_kg` | `true` | Fact-level temporal KG (event_time, supersession, invalidation) |
| `saga_enabled` | `true` | Transactional save (DB + vec + file) |
| `vec_rebuild_adaptive` | `true` | Dynamic vec-index rebuild threshold based on write velocity |
| `summarization` | `true` | Auto-summarize long notes |
| `user_profile` | `true` | Personalize recall ranking from access history |
| `consolidation` | `true` | SHA-256 + n-gram Jaccard dedup |
| `quality_gates` | `true` | Filter results below relevance threshold |

## Safety & Integrity

- **Lock order**: file flock first, then DB conn. Both `save_memory` and
  the incremental indexer follow this order.
- **Saga rollback**: `save.saga.undo_upsert` calls
  `save.cleanup.cleanup_memory_relations()` — removes kg_facts,
  orphan kg_edges, and backlinks on rollback.
- **Atomic markdown writes**: `safe_atomic_write` preserves conflicting
  on-disk versions as `<path>.conflict-<pid>-<ts>`.
- **Circuit breaker**: `auto_save` uses a circuit breaker for repeated
  failures; state is persisted to `memory_audit_log`.
- **CRDT markdown sync**: Every successful merge writes the merged
  content to the `.md` file. Markdown is source of truth; stale `.md`
  after a merge is silent drift.
- **Connection pool**: per-DB-path pool with re-entrancy guard;
  per-thread keys; `PoolExhaustedError` on full depth.

---
*This file is generated by `scripts/generate_architecture_md.py`.
Do not edit directly; run the script and review the diff.*
"""
    return doc


def _numbered_list(names: list[str]) -> str:
    return "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))


def _module_count() -> int:
    py_files = list(Path(".").glob("*.py"))
    return len([f for f in py_files if f.name not in {
        # exclude obvious build/test artifacts
        # (keep it simple: count all .py at repo root)
    }])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    doc = generate_doc()
    target = Path("docs/architecture.md")

    if "--check" in sys.argv:
        existing = target.read_text() if target.exists() else ""
        if existing.strip() == doc.strip():
            print("✅ docs/architecture.md is in sync with live code.")
            return 0
        print("❌ docs/architecture.md has drifted from live code.")
        print("   Run: python scripts/generate_architecture_md.py")
        return 1

    target.write_text(doc, encoding="utf-8")
    print(f"Written: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
