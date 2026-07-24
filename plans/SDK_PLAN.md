# SDK Implementation Plan

## Objective
Rewrite the agentic-memory SDK from a ~240-line Mem0-compatible proof-of-concept into a full-featured, production-ready Python library covering the system's entire 79-tool surface.

## Architecture
```
agentic_memory/
├── client.py       ← MemoryClient (replaces Memory) — full core API
├── agent.py        ← AgentMemory rewrite — full API
├── kg.py           ← Knowledge Graph operations
├── temporal.py     ← Temporal KG operations
├── maintenance.py  ← rebuild, compact, check_integrity, etc.
├── admin.py        ← stats, status, health, circuit breaker
├── sync.py         ← CRDT sync & sharing
├── models.py       ← Typed dataclasses
├── exceptions.py   ← Typed exception hierarchy
├── utils.py        ← Connection/config helpers
└── __init__.py     ← Re-exports + backward-compat aliases
```

## Phases (24 tasks)

### P1 — Foundation (6 tasks)
- P1a: exceptions.py — typed exception hierarchy
- P1b: models.py — typed dataclasses (MemoryResult, Entity, Fact, Relation, SearchResults)
- P1c: utils.py — connection helpers, config resolution
- P1d: client.py — MemoryClient class with full core API
- P1e: __init__.py — re-exports + backward-compat aliases
- P1f: tests for P1

### P2 — Knowledge Graph (3 tasks)
- P2a: kg.py — KnowledgeGraph class
- P2b: Integrate into MemoryClient
- P2c: tests

### P3 — Temporal KG (3 tasks)
- P3a: temporal.py — TemporalKG class
- P3b: Integrate into MemoryClient
- P3c: tests

### P4 — Maintenance & Admin (3 tasks)
- P4a: maintenance.py
- P4b: admin.py
- P4c: tests

### P5 — Agent & Sync (3 tasks)
- P5a: agent.py rewrite
- P5b: sync.py
- P5c: tests

### P6 — CLI (1 task)
- P6a: full __main__.py

### P7 — Type Stubs (1 task)
- P7a: comprehensive __init__.pyi

### P8 — Full Test Suite (1 task)
- P8a: 50+ tests in eval/test_sdk.py

### P9 — Examples (1 task)
- P9a: refresh + new examples

### P10 — Verification (2 tasks)
- P10a: backward compat check
- P10b: full test suite run

## Key Design Decisions
- MemoryClient replaces Memory with context manager + batch ops
- Memory kept as backward-compat alias
- Typed dataclasses for all return types
- user_id properly propagated to writes
- All parameters (importance, category, is_global, pinned) exposed
- Each feature domain gets its own module, attached as property

Created: 2026-06-25
Importance: 5 (high)
Pinned: yes
