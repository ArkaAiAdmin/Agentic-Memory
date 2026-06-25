# Temporal Knowledge Graph

The **temporal knowledge graph** adds bi-temporal validity to the fact
graph (see [Knowledge Graph](knowledge-graph.md) for the base system).
It answers questions like:

- "What did we know about Python on 2024-03-15?"
- "When did the team's policy on X change?"
- "Show me all the contradictions in our fact store this week."
- "What was true in 2023 but isn't true anymore?"

## Why Temporal Facts?

Plain facts are statements like `Python is_a language`. But facts have a
**lifespan** — they're true at some point in time and may not be true
forever:

- "John works_at Google" (true 2018-2022, then superseded by "John works_at Meta")
- "Python supports Python 2" (true 2010-2019, then expired)
- "We use Postgres" (true 2020-2022, then superseded by "We use SQLite")

Without temporal tracking, all these facts live together as "current",
even when only one is true *now*. The temporal KG preserves **history**
while keeping the current state queryable.

## Bi-Temporal Model

The schema uses **two time axes** (same approach as Graphiti/Zep):

| Axis | Column | Meaning | Source |
|---|---|---|---|
| **Event time** | `event_time` | When the fact was true in the world | Extracted from the memory text ("as of March 2026", "since 2020") |
| | `event_time_granularity` | Precision: `day` / `month` / `year` / `unknown` | Same as above |
| | `valid_at` | Earliest known time the fact was true | Defaults to `event_time` |
| | `invalid_at` | When the fact stopped being true | Set when superseded or invalidated |
| **Transaction time** | `transaction_time` | When WE learned it | Set to `now()` on INSERT |
| | `first_seen` / `last_seen` | Legacy transaction-time trackers | Kept in sync with `transaction_time` |

**Why two axes?** They answer different questions:
- "What was true on 2024-03-15?" — use `event_time` (a Python fact from a memory
  about March 2024 is true at that point, even if we learned about it today)
- "What did we know on 2024-03-15?" — use `transaction_time` (the system only
  surfaces facts that were indexed by that date)

The MVP defaults event_time = transaction_time for simplicity. Future
work may separate them more clearly.

## Schema (v21, cumulative)

The temporal KG spans four incremental migrations (018–021), each
adding columns or tables to `kg_facts`:

```sql
-- Migration history

-- 018_fact_temporal: bi-temporal columns + indexes
ALTER TABLE kg_facts ADD COLUMN event_time REAL;
ALTER TABLE kg_facts ADD COLUMN event_time_granularity TEXT;
ALTER TABLE kg_facts ADD COLUMN transaction_time REAL;
ALTER TABLE kg_facts ADD COLUMN valid_at REAL;
ALTER TABLE kg_facts ADD COLUMN invalid_at REAL;
ALTER TABLE kg_facts ADD COLUMN superseded_by INTEGER REFERENCES kg_facts(id) ON DELETE SET NULL;
ALTER TABLE kg_facts ADD COLUMN supersedes INTEGER REFERENCES kg_facts(id) ON DELETE SET NULL;
ALTER TABLE kg_facts ADD COLUMN contradiction_score REAL DEFAULT 0.0;
ALTER TABLE kg_facts ADD COLUMN invalidation_reason TEXT;

CREATE INDEX idx_kg_facts_validity      ON kg_facts(valid_at, invalid_at);
CREATE INDEX idx_kg_facts_superseded_by ON kg_facts(superseded_by);
CREATE INDEX idx_kg_facts_event_time    ON kg_facts(event_time);

-- 019_kg_facts_entity_fk: ON DELETE SET NULL on entity FKs
-- Recreates kg_facts so subject_entity_id / object_entity_id
-- FKs use ON DELETE SET NULL (prevents FK failures when
-- merged entities are deleted by kg_dedup).

-- 020_kg_facts_fts: full-text search on facts
CREATE VIRTUAL TABLE kg_facts_fts USING fts5(
    subject, predicate, object, context,
    content='kg_facts', content_rowid='id',
    tokenize='porter unicode61'
);
-- plus 3 sync triggers (ai / ad / au) keeping FTS in lockstep

-- 021_kg_crdt: CRDT tables for peer-to-peer KG replication
CREATE TABLE kg_entity_crdt ( ... );  -- 2P-Set Ops for entities
CREATE TABLE kg_edge_crdt   ( ... );  -- LWW edges with stable edge_id hash
```

All columns are NULL-able so pre-v18 DBs upgrade without data
migration. `ensure_facts_schema()` adds the columns to fresh DBs.

## Event Time Extraction

The `extract_event_time(content)` function uses 12 regex patterns
to detect when a fact was true:

| Pattern | Example | Granularity |
|---|---|---|
| ISO date | `2026-03-15` | `day` |
| ISO slash | `2026/03/15` | `day` |
| US slash | `3/15/2026` | `day` |
| Day-first named | `15 March 2026` | `day` |
| Month-first named | `March 15, 2026` | `day` |
| Bare month+year | `March 2026` | `month` |
| Quarter | `Q1 2026` | `month` |
| early/mid/late | `early 2024` | `month` |
| Preposition+year | `in 2024`, `since 2020` | `year` |
| Preposition+month+year | `in March 2026` | `month` |
| Preposition+ISO | `as of 2026-03-15` | `day` |
| Present tense | `currently`, `now`, `today` | `day` (now) |

**Bare "YYYY" without a preposition is rejected** — too noisy (matches
version numbers like "v2024", IDs like "id 1234", code references).

**Memory-level time applied to all facts**: each memory has a single
event_time (extracted once from the content), which is applied to
every fact extracted from that memory. Per-fact LLM time is captured
in the prompt but not yet wired through.

## Contradiction Detection

When a new fact is saved, `reconcile_fact_supersession(conn, new_fact_id)`
checks for contradictions:

```
Two facts (A, P, O1) and (A, P, O2) contradict iff:
  1. Same subject (case-insensitive)
  2. Same predicate
  3. Different object (case-insensitive)
  4. event_times match within granularity (or either is unknown)
```

**Time match uses the LESS precise of the two granularities**: a
day-precision fact matches a year-precision fact only if the years
match. An "unknown" granularity always matches (treated as "always
true" — overlaps everything).

When a contradiction is detected:
- The OLD fact is marked `invalid_at = new.event_time`, `superseded_by = new.id`,
  `invalidation_reason = 'contradicted'`, `contradiction_score = 1.0`
- The NEW fact is marked `supersedes = old.id`

The OLD fact is **preserved for history** (still in the DB, still
queryable via `query_fact_supersession_chain`) but excluded from
"current state" queries (which filter on `invalid_at IS NULL`).

## Edit Invalidation

When a memory is **edited**, `invalidate_stale_facts(conn, memory_id,
new_fact_keys)` diffs the old facts attributed to the memory against
the new extraction:

```
For each fact in OLD \ NEW:
  invalidate(fact_id, reason='manual')
```

This is **implicit update detection** — no need to distinguish INSERT
from UPDATE in the save pipeline:
- **INSERT**: no old facts → nothing to invalidate
- **UPDATE adding**: new facts added, old kept → diff is empty
- **UPDATE removing**: old facts removed via edit → marked invalidated

Invalidation sets `invalid_at` and `invalidation_reason = 'manual'`,
but NOT `superseded_by` (no replacement fact — the user just deleted
the content).

## Time-Aware Queries

Three query functions support the time-aware surface:

### `query_facts_at_time(conn, as_of, query=None, limit=100)`

Returns facts valid at epoch `as_of`:
```sql
SELECT ... FROM kg_facts AS f
WHERE (f.valid_at IS NULL OR f.valid_at <= ?)
  AND (f.invalid_at IS NULL OR f.invalid_at >= ?)
  AND (subject/predicate/object LIKE ? -- if query provided)
ORDER BY f.transaction_time DESC
```

### `query_fact_supersession_chain(conn, fact_id)`

Walks the `superseded_by` chain starting at `fact_id`, returning the
full history oldest-first:

```python
chain = query_fact_supersession_chain(conn, 42)
# [
#   {"id": 38, "subject": "python", "predicate": "is_a", "object": "language", ...},
#   {"id": 40, "subject": "python", "predicate": "is_a", "object": "framework",
#    "superseded_by": 42, "invalidation_reason": "contradicted", ...},
#   {"id": 42, "subject": "python", "predicate": "is_a", "object": "snake", ...},
# ]
```

Bounded at 100 hops to prevent infinite loops on pathological data.

### `query_facts_changed_since(conn, since_ts, limit=100)`

Returns facts that changed (inserted OR invalidated) since `since_ts`:

```sql
SELECT ... FROM kg_facts
WHERE transaction_time > ? OR (invalid_at IS NOT NULL AND invalid_at > ?)
ORDER BY COALESCE(invalid_at, transaction_time) DESC
```

## Admin / CLI Surface

| Surface | Operation | Description |
|---|---|---|
| MCP `memory_temporal_query` | `operation="at_time" / "chain" / "changed_since"` | Single entry point for all 3 queries |
| MCP `memory_temporal_contradictions` | `since_ts / until_ts / reason` | List supersession events with old+new fact details |
| CLI `python memory_integrity.py <db> --temporal-summary` | Stats | Total facts, event_time coverage, supersession counts, reasons distribution |
| CLI `python memory_integrity.py <db> --temporal-query at_time 2026-03-15` | Query | Facts valid at the given date |
| CLI `python memory_integrity.py <db> --temporal-query chain <id>` | Query | Walk the chain for a fact |
| CLI `python memory_integrity.py <db> --temporal-query changed_since 2026-06-22` | Query | Recent changes |

All query surfaces use **read-only URI mode** (`mode=ro`) so they
work even when the live `auto_save.py daemon` holds the flock on
the main DB.

## Feature Flag (T8)

The entire temporal subsystem is gated behind `MEMORY_TEMPORAL_KG=1`
(default ON). When disabled:
- Basic fact extraction still works (no regression)
- No `event_time` is stored (column stays NULL)
- No contradiction reconciliation runs
- No edit invalidation runs
- No audit log writes

To disable:
```bash
export MEMORY_TEMPORAL_KG=0
# or in memory.toml:
# [features]
# feature_temporal_kg = false
```

For per-fact escape hatches (more targeted than the global flag), set
`kg_facts.locked = 1` to prevent a specific fact from being superseded
or invalidated.

## Audit Log

T5.4 reuses the existing `memory_audit_log` table with
`tool='kg_fact_temporal'`. Event details are JSON in the `args` column:

```json
{
  "event": "invalidate",
  "fact_id": 42,
  "reason": "manual",
  "subject": "python",
  "predicate": "is_a",
  "object": "language",
  "memory_id": "mem_a"
}
```

Queryable via:
```
memory_audit_query(tool_name="kg_fact_temporal", since_ts=..., limit=...)
```

## Performance

On the live prod DB (v21):
- `index_facts_for_memory` adds ~10ms per fact (event_time extraction +
  reconciliation)
- `invalidate_stale_facts` is O(old_facts) — one query, one row check per old fact
- `query_facts_at_time` uses the `(valid_at, invalid_at)` index
- `query_fact_supersession_chain` is bounded at 100 hops
- All query surfaces use read-only URI to avoid flock contention

The temporal KG is fast enough to be on the save hot path. No
measurable regression observed in the focused regression suite
(243/243 tests passing after T1-T8).

## Limitations

1. **Memory-level time, not per-fact**: All facts from a memory share
   the same `event_time`. LLM can return per-fact time but it's
   captured in the prompt and not yet propagated.
2. **Order-of-insertion matters**: If Memory A (event_time=2020) is
   inserted first with "John works_at Google", then Memory B
   (event_time=2024) is inserted with "John works_at Meta", the
   contradiction is correctly detected. But the reverse order
   (Memory B first, then Memory A) is NOT detected as a contradiction
   (different times) — the user must manually supersede.
3. **No LLM-scored contradiction**: The current detector always uses
   `contradiction_score = 1.0`. Future work may add LLM-scored soft
   contradictions (e.g., "Python is_a language" vs "Python is_a
   scripting language" might be a 0.7-contradiction, not a 1.0).
4. **No fact-type or qualifier awareness**: "John is_a doctor" and
   "John is_a patient" are technically contradictions (different O
   for same S+P), but the current detector doesn't know that a
   person can be both at different times — it just sees two
   different objects.

These are tracked as future work; the MVP focuses on the simple
"same S+P, different O, same time = contradiction" rule.

## See Also

- [Knowledge Graph](knowledge-graph.md) — base fact graph (pre-temporal)
- [Architecture](../architecture.md) — where the temporal KG fits in
  the system
- [AGENTS.md](../../AGENTS.md) — operator-facing doc with the
  feature-flag table
- `fact_temporal.py` — implementation (4 core functions + 3 query
  functions)
- `mcp_audit.py` — `memory_temporal_query` and `memory_temporal_contradictions`
- `memory_integrity.py` — `--temporal-summary` and `--temporal-query` CLI
- Migration `018_fact_temporal.sql` — schema upgrade
