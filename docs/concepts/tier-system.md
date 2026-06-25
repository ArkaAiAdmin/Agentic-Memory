# Tier System

Agentic Memory automatically manages memory lifecycle through **hot, warm, and cold tiers**. Memories move between tiers based on access patterns, importance, and age.

## Tier Overview

```
┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐
│  Hot    │────▶│  Warm   │────▶│  Cold   │────▶│ Archive │
│ (frequent)│   │ (recent)│   │ (rare)  │   │ (old)   │
└─────────┘     └─────────┘     └─────────┘     └─────────┘
  Fast search    Normal search   Slow search     Deleted
```

| Tier | Description | Search Impact |
|------|-------------|---------------|
| **Hot** | Actively used, frequently accessed | Included in all searches |
| **Warm** | Recently created, moderate access | Included in most searches |
| **Cold** | Rarely accessed, older memories | Excluded from quick search |
| **Archive** | Very old, low importance | Excluded from all searches |

## How Tiers Are Assigned

When a memory is saved, it starts as **hot**. The tier is then adjusted by:

### 1. Access Count

```python
# Access frequency scoring
access_score = min(access_count / 10, 1.0)  # 0.0 to 1.0
```

- 0-2 accesses → likely cold
- 3-5 accesses → warm
- 6+ accesses → hot

### 2. Recency

```python
# Time since last access
days_since_access = (now - last_accessed).days
recency_score = max(0, 1 - days_since_access / 30)  # Linear decay
```

- Accessed today → 1.0
- Accessed last week → 0.77
- Accessed last month → 0.0

### 3. Success Score

```python
# Reinforcement from positive/negative feedback
success_score = sum(rewards) / max(len(rewards), 1)
```

- Always successful → high score → stays hot
- Always failed → low score → moves to cold

### 4. Importance Weight

```python
# Combined importance
importance = (access_score × 0.4) +
             (recency_score × 0.3) +
             (success_score × 0.3)
```

## Tier Migration

The `tier_migration.py` script runs periodically (via cron) to move memories between tiers:

```python
# Migration logic
if importance >= 0.7 and access_count >= 5:
    tier = "hot"
elif importance >= 0.4 and days_since_access <= 14:
    tier = "warm"
elif importance >= 0.2 or days_since_access <= 30:
    tier = "cold"
else:
    tier = "archive"
```

### What Happens During Migration

**Hot → Warm:**
- No change to search behavior
- Memory remains in all search results

**Warm → Cold:**
- Excluded from quick search (FTS5 only)
- Still accessible via explicit search
- Background tasks stop processing it

**Cold → Archive:**
- Excluded from all searches
- Markdown file preserved
- Can be restored by re-indexing

## Spaced Repetition

The tier system integrates with **SM-2 spaced repetition** to surface memories at optimal review intervals:

```python
# SM-2 scheduling
if quality >= 3:  # Good review
    interval *= ease_factor
    ease_factor += 0.1
else:  # Poor review
    interval = 1
    ease_factor -= 0.2
```

This creates a feedback loop:
1. Memory saved → starts as hot
2. Agent uses it → access count increases
3. Agent confirms it works → success score increases
4. Memory stays hot → gets surfaced more often
5. Memory becomes stale → access drops → moves to cold

## Pinning

Important memories can be **pinned** to prevent tier migration:

```python
save_memory(
    content="...",
    category="lessons",
    title_slug="critical-lesson",
    pinned=True,  # Never auto-archives
)
```

Pinned memories:
- Always remain in search results
- Never move to cold/archive tiers
- Show a pin indicator in the UI

## Configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `MEMORY_SELF_DIRECTED` | `0` | Enable tier migration |
| Pin decay check | Daily | Auto-unpin stale pinned memories |
| Archive threshold | 180 days | Move to archive after this period |

## Monitoring Tier Distribution

```bash
# Check tier distribution
python -c "
import sqlite3
conn = sqlite3.connect('memory.db')
for row in conn.execute('SELECT tier, COUNT(*) FROM memories GROUP BY tier'):
    print(f'{row[0]}: {row[1]} memories')
"
```

Or use the MCP tools:

```python
memory_tier_stats()              # Returns tier distribution and importance stats
memory_run_tier_migration(dry_run=True)   # Preview a migration pass
memory_run_tier_migration(dry_run=False)  # Commit a migration pass on demand
```

## Further Reading

- [Background Tasks](background-tasks.md) — How async processing works
- [Why Markdown](why-markdown.md) — Why tiers affect the index, not the files
