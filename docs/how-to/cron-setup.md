# How to Set Up Cron Jobs

Agentic Memory uses cron jobs for background processing. Here's how to set them up.

## Required Jobs

### 1. Background Task Worker (Every 5 minutes)

Processes pending background tasks (entity resolution, fact consolidation, etc.).

```bash
# Edit crontab
crontab -e

# Add this line
*/5 * * * * agentic-memory-worker
```

### 2. Compact and Dedup (Daily at 2 AM)

Runs entity deduplication and fact consolidation.

```bash
0 2 * * * agentic-memory-compact
```

### 3. Backup Database (Daily at 3 AM)

Creates a timestamped backup of the database.

```bash
0 3 * * * cd /path/to/agentic-memory && venv/bin/python cron/cron_backup.py
```

## Optional Jobs

### Health Check (Daily at 4 AM)

Runs integrity checks and repairs.

```bash
0 4 * * * agentic-memory-integrity
```

### Tier Migration (Weekly on Sunday at 5 AM)

Moves memories between hot/warm/cold tiers.

```bash
0 5 * * 0 agentic-memory-tier
```

### Quality Filter (Weekly on Sunday at 6 AM)

Runs quality gates on memories.

```bash
0 6 * * 0 cd /path/to/agentic-memory && venv/bin/python cron/cron_quality_filter.py
```

### KG Backfill (Weekly on Sunday at 3:30 AM)

Refreshes kg_facts, kg_entities, and kg_edges from the current memory
corpus using the entity quality filters (P3.2, 2026-06-19). Runs after
the FTS rebuild at 02:30 and before the heavier `cron_compact` runs.
Uses `--incremental` so it never wipes existing KG data — a kill/OOM
is safe and partial progress persists.

```bash
30 3 * * 0 cd /path/to/agentic-memory && venv/bin/python cron/cron_kg_backfill.py
```

Log output: `memory/kg-backfill-cron.log` (one JSON line per run).

## Full Crontab Example

```cron
# Agentic Memory background tasks
# Process task queue every 15 minutes (reduced from */5 on 2026-06-22 to prevent runaway workers)
*/15 * * * * agentic-memory-worker

# Compact and dedup daily at 2 AM
0 2 * * * agentic-memory-compact

# Backup database daily at 3 AM
0 3 * * * cd /Users/arka/.config/agentic-memory && venv/bin/python cron/cron_backup.py

# Health check daily at 4 AM
0 4 * * * agentic-memory-integrity

# Tier migration weekly on Sunday at 5 AM
0 5 * * 0 agentic-memory-tier

# Quality filter weekly on Sunday at 6 AM
0 6 * * 0 cd /path/to/agentic-memory && venv/bin/python cron/cron_quality_filter.py

# KG backfill weekly on Sunday at 3:30 AM (after FTS rebuild at 02:30)
30 3 * * 0 cd /path/to/agentic-memory && venv/bin/python cron/cron_kg_backfill.py
```

## Viewing Cron Output

### Redirect output to a log file

```cron
*/5 * * * * cd /path/to/agentic-memory && venv/bin/python background_worker.py --once >> /var/log/agentic-memory.log 2>&1
```

### Check cron logs

```bash
# macOS
grep CRON /var/log/system.log

# Linux
grep CRON /var/log/syslog
```

## Docker Cron

If running in Docker, use a cron container:

```yaml
services:
  cron:
    image: alpine:latest
    volumes:
      - ./crontab:/etc/crontabs/root
      - agentic-data:/data
    command: crond -f -l 2
```

## Troubleshooting

### Cron job not running

1. Check cron service is running: `sudo service cron status`
2. Check permissions: `chmod +x cron/cron_your_op.py`
3. Check paths: Use absolute paths in crontab
4. Check environment: Cron doesn't load shell profile

### Tasks not processing

```bash
# Check pending tasks
agentic-memory-integrity
```

### Database locked errors

Ensure only one worker runs at a time:

```cron
# Use flock to prevent concurrent runs
*/5 * * * * flock -n /tmp/agentic-memory-worker.lock agentic-memory-worker
```

## Further Reading

- [Background Tasks](../concepts/background-tasks.md) — How the task queue works
- [Self-Hosting](../self-hosting.md) — Production deployment guide
- [KG Backfill: Data-Loss Recovery](../../lessons/2026-06-19-kg-backfill-data-loss-recovery.md) — Why the cron uses --incremental
- [KG LLM Extraction Speed](../../lessons/2026-06-19-kg-llm-extraction-speed.md) — Why the cron defaults to regex-only
