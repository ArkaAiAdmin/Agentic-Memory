# Agent Contract — agentic-memory

You are an agent using the agentic-memory system. Follow these 5 rules. Additional maintainer-level rules, hard constraints, wiring diagrams, and recovery procedures are in `AGENTS.md` at the repo root — read that file for the full contract.

---

## 1. Start Every Session

```
agentic-memory_memory_session_start(query="<project/task/goal>")
```

Loads prior context — pinned notes, recent activity, high-importance memories, relevant search results, KG facts, and any unresolved contradictions.

## 2. Search Before You Act

```
agentic-memory_memory_search(query="<topic>")
```

Search before designing features, making decisions, writing code, or debugging. The search pipeline uses FTS5 + vector + KG fusion — it finds what you've done before.

## 3. Save What You Learn

```
agentic-memory_memory_save(category="lessons"|"decisions"|"projects", importance=4)
```

Save after: bug fixes (`lessons`), architectural decisions (`decisions`), significant milestones (`projects`, importance=4). Include context — what was the problem, what were the options, why was this one chosen, tradeoffs. The auto-save hook also captures tool calls; use this for deliberate, curated saves.

## 4. End Every Session

```
agentic-memory_memory_save(category="sessions")
```

The reinforce step runs automatically at session end — recalled memories get outcome-based `success_score` updates. You don't need to call reinforce yourself.

## 5. Maintenance Is Automated

Indexing, compaction, dedup, contradiction detection, skill extraction, semantic clustering, and background workers all run on schedules. Do not call `memory_maintenance`, `memory_organize`, or individual admin tools unless cron is down or you need immediate results.

---

_For a quick setup, core tool cheatsheet, and disaster recovery procedures, see [AGENT_QUICKSTART.md](file:///Users/arka/.config/agentic-memory/docs/AGENT_QUICKSTART.md). For the full contract: hard rules, constitution, Critical Path, hook wiring, and test requirements — see `AGENTS.md` at the repo root. It is loaded automatically as workspace context and is authoritative for all maintainer-level decisions._
