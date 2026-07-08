# Production Readiness — agentic-memory

> Target: a competent engineer can deploy and operate agentic-memory in
> production using only this document.
> Schema v36 · Last updated 2026-07-08

---

## Section 1 — Deployment Topology

### Recommended File Layout

```
~/.config/agentic-memory/
├── agentic-memory/           ← package source
├── memory/
│   ├── memory.db             ← main SQLite database (WAL mode)
│   ├── memory.db-wal         ← WAL journal (same filesystem, same mount)
│   ├── memory.db-shm         ← shared-memory index (WAL housekeeping)
│   ├── journal.db            ← CQRS write journal (separate disk recommended)
│   ├── locks/                ← flock files for cross-process serialization
│   │   ├── daemon.lock
│   │   └── cron_health_check.lock
│   ├── .health_status.json   ← last cron health-check output
│   ├── .auto_save_circuit_sentinel  ← present when circuit breaker is OPEN
│   ├── .rebuild.lock         ← present during vec-index rebuild
│   └── logs/
│       ├── worker.log        ← background-worker output
│       ├── health-check.log  ← cron_health_check output
│       └── auto-save.log     ← auto-save daemon output
├── cron/
│   └── install_crontab.sh    ← run once after deploy to install 38 cron entries
├── config/
│   └── memory.toml           ← operator-edited config (env vars override this)
└── venv/                     ← virtual environment
```

### Disk Layout

| File / Directory | Recommended Mount | Rationale |
|---|---|---|
| `memory/memory.db` | SSD (NVMe preferred) | Hot path: every save and search hits this file |
| `memory/journal.db` | Separate disk or mount from `memory.db` | CQRS journal absorbs write bursts; isolation prevents a journal-full event from corrupting the main DB |
| `memory/locks/` | Same as `memory/` | flock files are tiny; co-location minimises cross-mount syscall overhead |
| `memory/logs/` | Same as `memory/` | Logs are append-only; can be rotated to secondary storage |

**Separate-disk setup example (Linux):**

```bash
mkdir -p /mnt/fast/memory_journal
ln -s /mnt/fast/memory_journal/journal.db ~/.config/agentic-memory/memory/journal.db
# Set in environment or memory.toml:
#   general.db_path = "memory/memory.db"
#   (journal.db path is derived from db_path via write_journal.py unless overridden via MEMORY_JOURNAL_DB_PATH)
```

On macOS, a separate APFS volume or a symlink to a secondary disk achieves the same effect.

---

## Section 2 — Pre-Flight Checklist

Run every item before declaring the deployment healthy.

---

### 2.1 Separate Disks — `memory.db` and `journal.db` on Different Mount Points

**Verify:**

```bash
df -h ~/.config/agentic-memory/memory/memory.db \
      ~/.config/agentic-memory/memory/journal.db
```

Confirm the `Filesystem` (device ID) column differs between the two files.

**Good:** Two distinct device IDs returned.

**Fix:** Set `MEMORY_JOURNAL_DB_PATH=/mnt/fast/memory_journal/journal.db` in the service environment file, or create a symlink.

---

### 2.2 WAL Mode Active

**Verify:**

```bash
sqlite3 ~/.config/agentic-memory/memory/memory.db "PRAGMA journal_mode"
```

**Good:** Output is exactly `wal`.

**Fix:**

```bash
sqlite3 ~/.config/agentic-memory/memory/memory.db "PRAGMA journal_mode=WAL"
sqlite3 ~/.config/agentic-memory/memory/memory.db "VACUUM"
```

The connection pool in `infra/db.py` sets WAL on every new connection; this manual
run only needs to happen once for an existing database that started in DELETE mode.

---

### 2.3 `busy_timeout` ≥ 30000 ms

**Verify:**

```bash
sqlite3 ~/.config/agentic-memory/memory/memory.db "PRAGMA busy_timeout"
```

**Good:** Returns `30000` or higher.

**Fix:** The connection pool in `infra/db.py` sets `busy_timeout` on checkout.
If a custom connection path bypasses the pool, add:

```python
conn.execute("PRAGMA busy_timeout = 30000")
```

For an existing pool: no action needed — the PRAGMA is set per-connection at open.

---

### 2.4 Cron Installed

**Verify:**

```bash
crontab -l | grep agentic-memory | wc -l
```

**Good:** Returns `38` (all managed cron entries present).

**Fix:**

```bash
cd ~/.config/agentic-memory
bash cron/install_crontab.sh
```

The script deduplicates on re-run; safe to execute multiple times.

---

### 2.5 Daemon Watchdog Running

**Verify:**

```bash
# Confirm the cron heartbeats are fresh (runs every 15 min)
find ~/.config/agentic-memory/memory/logs/ -name "worker.log" \
  -mmin +20  # should return nothing if the daemon is alive
# Or check active process
ps aux | grep background_worker | grep -v grep
```

**Good:** `worker.log` mtime within last 15 minutes, or a `background_worker.py`
process is visible.

**Fix:**

```bash
# 1. Verify cron is installed (see 2.4)
# 2. Check worker.log for the startup banner
tail -5 ~/.config/agentic-memory/memory/logs/worker.log
# 3. If not running, force a cron run:
bash ~/.config/agentic-memory/cron/cron_backup.py
```

---

### 2.6 Circuit Breaker Sentinel File

**Verify:**

```bash
ls ~/.config/agentic-memory/memory/.auto_save_circuit_sentinel 2>/dev/null && echo "PRESENT (OPEN)" || echo "ABSENT (CLOSED/healthy)"
```

**Good:** File does **not** exist — this means the circuit is closed and auto-save
is flowing normally.

**Fix if present:**

```bash
# 1. Diagnose root cause from worker.log:
grep "circuit breaker" ~/.config/agentic-memory/memory/logs/worker.log | tail -5
# 2. Fix the underlying issue (disk space, DB locked, etc.)
# 3. Remove sentinel to close the circuit:
rm ~/.config/agentic-memory/memory/.auto_save_circuit_sentinel
# The sentinel auto-creates on the next trip; removing it here just resets the flag.
```

---

### 2.7 Health Check Passes

**Verify:**

```bash
cd ~/.config/agentic-memory
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/python -m memory_mcp memory_health_check
```

**Good:** Output is `"overall_healthy": true` (all subsystems report OK).

**Fix if unhealthy:**

```bash
# Inspect which subsystem is failing
python -c "
import json
print(json.dumps(json.load(open('memory/.health_status.json')), indent=2))
"
# Fix per subsystem:
#   - index_integrity  → python rebuild_vec_index.py
#   - kg_orphans       → venv/bin/python memory_integrity.py memory/memory.db --repair-kg-orphans
#   - circuit_breaker  → see 2.6
#   - disk pct_used > 95 → backup rotation / purge_expired
```

---

## Section 3 — Operational Runbooks

### Runbook Index

| Failure | Symptoms | Recovery |
|---|---|---|
| WAL corruption | `SQLITE_CORRUPT` errors in worker.log | `sqlite3 memory.db ".recover" > recovered.sql ; sqlite3 new.db < recovered.sql ; mv new.db memory.db` |
| Journal drift | Health check shows `pending > 1000` | `agentic-memory_memory_maintenance(operation="reset_stuck_processing")` |
| Circuit breaker open | No auto-saves for > 5 min; sentinel file present | See runbook 6 above: diagnose root cause, remove sentinel |
| Vec index drift | `vec_keys` lag `memories` by > 50 | `venv/bin/python rebuild_vec_index.py` |
| Disk full | Health check `pct_used > 95` | Run backup rotation, purge expired: `agentic-memory_memory_maintenance(operation="purge_expired", confirm=True)` |
| Zombie workers | `ps aux \| grep background_worker` shows > 1 | Kill extras: `kill <pid>`; check cron cadence `crontab -l` |
| Database locked | `DB_LOCK` errors in logs | Wait for lock holder (cron overlap is normal and self-resolves); if stuck, `ps aux \| grep python` to identify holder; do NOT force-kill the holder mid-save |

### Detailed — WAL Corruption

1. Read `worker.log` to confirm `SQLITE_CORRUPT` or `database disk image is malformed`.
2. Take a snapshot of the current DB: `cp memory.db memory.db.bak.<timestamp>`.
3. Recover: `sqlite3 memory.db ".recover" > /tmp/recovered.sql`.
4. If the recovered file is empty, the backup rotation is the fallback: `cp memory.db.<N>.bak memory.db`.
5. Re-run migrations on the recovered file to reach the current schema version.
6. Restart the daemon and re-apply the CQRS journal with `background_worker.py`.

### Detailed — Journal Drift

`journal.db` can accumulate `pending` entries if the background worker is down
or processing slower than agents are writing.

1. Check journal size: `sqlite3 memory/journal.db "SELECT status, COUNT(*) FROM write_journal GROUP BY status"`.
2. If `processing` rows are stale (> 10 min), run `reset_stuck_processing`.
3. If `pending` count is still > 5000 after reset, check whether the worker process
   is alive. Start it manually: `venv/bin/python background/background_worker.py`.

### Detailed — Zombie Workers

Multiple `background_worker.py` processes can accumulate if a daemon was manually
started in addition to the cron-launched one.

1. Enumerate: `ps aux | grep background_worker | grep -v grep | awk '{print $2}'`.
2. Kill extras: `kill -TERM <pid>` (SIGTERM allows clean journal checkpoint).
3. Verify single worker after 30 s: same `ps` command.
4. If a worker is immediately respawned, a cron entry is running; reduce to one.

---

## Section 4 — Backup & Recovery

### Backup Schedule

| Frequency | Mechanism | Retention |
|---|---|---|
| Hourly incremental | `cron_backup.py` (journal + WAL) | 24 hours |
| Daily full | `cron_backup.py` | 7 days |
| Weekly OKF export | `agentic-memory_memory_maintenance(operation="okf_export")` | Manual |

### OKF Portable Export

```bash
venv/bin/python okf_export.py \
  --input-dir ~/.config/agentic-memory/memory/ \
  --output-dir ~/.backup/agentic-memory-okf/ \
  --include-deleted
```

OKF (Open Knowledge Format) is JSON Lines + flat files — portable across
versions and restorable via `okf_import.py`.

### Restore Procedure

```
1. STOP the daemon (break the single-writer invariant):
     pkill -f background_worker.py

2. Restore DB:
     cp backup/memory.db.[timestamp] memory/memory.db

3. Apply migrations if backup DB is behind:
     venv/bin/python -m infra.migration_runner --db memory/memory.db

4. Start daemon:
     venv/bin/python background/background_worker.py &
     # Or wait for cron (15 min max)

5. Verify:
     OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/python -m memory_mcp memory_health_check
```

**Rule:** Never restore a DB while the background worker is running. The worker is
the single writer to memory.db; a concurrent restore will corrupt the WAL.

---

## Section 5 — Scaling Limits

| Limit | Value | Notes |
|---|---|---|
| Max safe write throughput | ~100 writes/sec | SQLite single-writer bound; CQRS journal + background_worker removes client-side serialisation |
| Max concurrent writers per DB | 10 agents | Above this, pool contention rises sharply |
| Connection pool size | 24 (code default) | Set via `MEMORY_DB_POOL_SIZE` env var; pool is per-DB-path |
| Vec index rebuild time | ~15 s for 10 K memories | `rebuild_vec_index.py` is blocking; run during low-traffic window |
| Memory footprint | ~200 MB baseline + ~50 MB per 10 K memories | Scales with KG + FTS5 + vec index |
| Journal growth rate | ~1 KB per save (with metadata) | Monitor via `journal.db` size; recycle journal after checkpoint |
| Health-check cron cadence | 15 min | Hardcoded in `install_crontab.sh` |
| Schema version | 36 | Bumped in `infra/migration_runner.py` only |

### Write Throughput Guidance

- **< 10 writes/sec**: Single process, no CQRS needed. `save_memory` direct path is fine.
- **10 – 100 writes/sec**: Multiple agents writing via `save_memory_journal`. Background worker drains journal at its own pace.
- **> 100 writes/sec**: Consider sharding by `agent_id` or moving to a server-based store. SQLite will queue.

---

## Section 6 — Security Posture

### Threat Model

agentic-memory stores private human memories. All external input is treated as
hostile until proven otherwise.

### Controls

1. **Prompt injection guard (save pipeline)**
   The `save_pipeline` in `save/pipeline.py` sanitises all content before saving.
   Injected control-characters, null bytes, and SQL meta-characters in `content`
   are rejected or escaped. `MEMORY_SAVE_MAX_CONTENT_BYTES` (default 1 MB) caps
   injection payload size.

2. **Credentials never surfaced**
   API tokens, HMAC secrets, and `MEMORY_API_TOKEN` are never returned in MCP
   tool responses. They are written to `memory/.api_token` with mode `0600`
   (owner read-write only).

3. **Sync server binding**
   The sync HTTP server (`PORT=9877`) binds to `127.0.0.1` by default. It is not
   exposed externally unless `MEMORY_SYNC_LISTEN_HOST` is changed.
   Optional TLS via `MEMORY_SYNC_TLS_CERT` / `MEMORY_SYNC_TLS_KEY`.
   mTLS via `MEMORY_SYNC_TLS_CLIENT_CA`.
   Non-loopback without TLS emits a startup warning.

4. **Circuit breaker sentinel**
   `.auto_save_circuit_sentinel` is a plain-text file containing the string `open`.
   No secrets, tokens, or PII are written to it. It is used only as a cross-process
   flag (TS plugin reads it before spawning the Python auto-save subprocess).

5. **File permissions**
   `memory/` is owned by the running user. No world-readable permissions are set.
   The `safe_atomic_write` helper in `infra/md_utils.py` uses `os.replace()` which
   is atomic on POSIX; on Windows it falls back to a rename.

6. **Integrity-critical flag monitoring**
   Setting `MEMORY_SAGA_ENABLED=0`, `MEMORY_CRDT_ENABLED=0`,
   `MEMORY_WRITE_JOURNAL_ENABLED=0`, or `MEMORY_QUALITY_GATES=0` emits a
   `SECURITY:` warning to stderr at startup so the operator sees the downgrade.

7. **Schema drift detection**
   Migration checksums (SHA-256 per migration file) are stored in `schema_version`.
   On startup, `_enforce_checksum_integrity()` refuses to apply further migrations
   if any on-disk migration file's hash mismatches the stored value (OWASP A08-002).

---

## Appendix A — Environment Variables Reference

Key overrides for production deployment:

| Variable | Purpose | Default |
|---|---|---|
| `MEMORY_DB_PATH` | Path to main database | `memory/memory.db` |
| `MEMORY_JOURNAL_DB_PATH` | Path to CQRS journal DB | `<db_dir>/journal.db` |
| `MEMORY_DB_POOL_SIZE` | Max SQLite connections | `24` |
| `MEMORY_WRITE_JOURNAL_ENABLED` | Enable CQRS journal | `0` (off) |
| `MEMORY_SAGA_ENABLED` | Enable saga transaction | `1` (on) |
| `MEMORY_ASYNC_AUTOSAVE` | Enable async daemon | `1` (on) |
| `MEMORY_KNOWLEDGE_GRAPH` | Enable KG extraction | `1` (on) |
| `MEMORY_CONFIG_PATH` | Override config.toml location | (none — use default) |
| `MEMORY_INSTALL_ROOT` | Override package root | (auto-detected) |

Full list: `docs/reference/mcp-tools.md` and `infra/config.py`.

## Appendix B — Quick-Reference Commands

```bash
# Start the background worker (if not using cron)
cd ~/.config/agentic-memory
venv/bin/python background/background_worker.py --daemon

# Install / reinstall cron
bash cron/install_crontab.sh

# Run a single health check
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/python -m memory_mcp memory_health_check

# Manual vec index rebuild
venv/bin/python rebuild_vec_index.py

# Run all migrations
venv/bin/python -m infra.migration_runner --db memory/memory.db

# Verify migration checksums
venv/bin/python -m infra.migration_runner --db memory/memory.db --verify

# Reset circuit breaker (after fixing root cause)
rm memory/.auto_save_circuit_sentinel

# Compact + consolidate (idempotent)
agentic-memory_memory_maintenance(operation="compact", kwargs={"dry_run": false})

# OKF backup
venv/bin/python okf_export.py --output-dir ~/backups/agentic-memory-$(date +%Y%m%d)
```
