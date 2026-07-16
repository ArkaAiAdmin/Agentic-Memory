# Agent Contract — agentic-memory

You are an agent using the agentic-memory system. Follow these 5 rules. Additional maintainer-level rules, hard constraints, wiring diagrams, and recovery procedures are in `AGENTS.md` at the repo root — read that file for the full contract.

---

## Rule 0: Truth-Source Ranking (MANDATORY)

Before reading AGENTS.md, docs/MCP_SURFACE.md, or docs/architecture.md
for ANY count (tool names, schema version, cron count, test count, LOC):

1. Read docs/_meta.json FIRST.
2. Check _meta.json["provenance"]["truth_rank_1"] — that is the canonical
   machine-enforced source for all counts.
3. Check _meta.json["provenance"]["last_meta_regenerated"] — if > 7 days
   old, run `make update-agents-md` before trusting any counts.
4. Only then fall back to AGENTS.md AUTO-GEN sections or MCP_SURFACE.md
   for behavioral/narrative context (not counts).

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

> **Self-editing happens automatically.** On every `memory_save` with procedural content, the system auto-extracts a reusable skill into `memory_skills` (verify with `memory_list_skills`). You do not need to invoke skill compilation yourself — but you can: `memory_learn(content=..., as_skill=True, skill_name=...)` compiles a skill in one call, and `memory_extract_skills` / `memory_compile_skill` are now CORE tools.
>
> **If your new lesson contradicts an existing one**, prefer `memory_note(note_id, action="supersede", rationale="...")` over a fresh `memory_save`. That records the revision in `memory_revision_log` and supersedes the stale note instead of leaving two conflicting memories. Use `memory_search` first to find the note you'd supersede.

## 4. End Every Session

```
agentic-memory_memory_save(category="sessions")
```

The reinforce step runs automatically at session end — recalled memories get outcome-based `success_score` updates. You don't need to call reinforce yourself.

## 5. Maintenance Is Automated

Indexing, compaction, dedup, contradiction detection, skill extraction, semantic clustering, and background workers all run on schedules. Do not call `memory_maintenance`, `memory_organize`, or individual admin tools unless cron is down or you need immediate results.

---

_For a quick setup, core tool cheatsheet, and disaster recovery procedures, see [AGENT_QUICKSTART.md](file:///Users/arka/.config/agentic-memory/docs/AGENT_QUICKSTART.md). For the full contract: hard rules, constitution, Critical Path, hook wiring, and test requirements — see `AGENTS.md` at the repo root. It is loaded automatically as workspace context and is authoritative for all maintainer-level decisions._
