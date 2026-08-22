# Disaster Recovery: Restoring Memory Backups

This runbook describes the authoritative procedure for restoring an **agentic-memory** database and associated markdown memory notes from backups into the active `$MEMORY_HOME/data` directory.

---

## 1. Backup Locations and Artifacts

Backups created by `cron/cron_backup.py` and automatic safety rotations are stored in:

- **Active backup directory**: `$MEMORY_HOME/backups/` (e.g. `~/Library/Application Support/AgenticMemory/backups/` on macOS or `~/.local/share/AgenticMemory/backups/` on Linux).
- **Snapshot naming conventions**:
  - Full archive: `backup_YYYYMMDD_HHMMSS.tar.gz` (contains `memory.db` + category subdirectories).
  - Standalone SQLite snapshot: `backup_YYYYMMDD_HHMMSS.db` or `memory.db.bak.YYYYMMDD-HHMMSS`.

---

## 2. Pre-Restoration Checklist

1. **Stop active writers**:
   Stop any running daemons, background workers, or editor plugins touching the database:
   ```bash
   # Kill background workers and auto-save daemons if running
   pkill -f "background_worker.py" || true
   pkill -f "auto_save.py daemon" || true
   ```

2. **Identify target directory**:
   ```bash
   python -c "from infra.memory_config import get_global_memory_dir; print(get_global_memory_dir())"
   # Output: /Users/<user>/Library/Application Support/AgenticMemory/data
   ```

3. **Archive current corrupt/stale state**:
   ```bash
   export MEM_DATA="$(python -c 'from infra.memory_config import get_global_memory_dir; print(get_global_memory_dir())')"
   cp "$MEM_DATA/memory.db" "$MEM_DATA/memory.db.pre-restore-$(date +%s)" 2>/dev/null || true
   ```

---

## 3. Restore Procedures

### Scenario A: Restoring from a Full Archive (`.tar.gz`)

1. **Extract the backup archive to a temporary staging area**:
   ```bash
   mkdir -p /tmp/agentic-restore
   tar -xzvf "$MEMORY_HOME/backups/backup_YYYYMMDD_HHMMSS.tar.gz" -C /tmp/agentic-restore
   ```

2. **Atomically replace the database and markdown files**:
   ```bash
   # Copy SQLite database
   cp /tmp/agentic-restore/memory.db "$MEM_DATA/memory.db"

   # Synchronize category markdown notes without deleting unbacked new files unless desired
   for dir in lessons decisions preferences projects sessions; do
       if [ -d "/tmp/agentic-restore/$dir" ]; then
           mkdir -p "$MEM_DATA/$dir"
           cp -R /tmp/agentic-restore/"$dir"/* "$MEM_DATA/$dir/" 2>/dev/null || true
       fi
   done

   rm -rf /tmp/agentic-restore
   ```

---

### Scenario B: Restoring from a Standalone SQLite Snapshot (`.db` or `.db.bak`)

1. **Verify snapshot integrity before copy**:
   ```bash
   sqlite3 "$SNAPSHOT_PATH" "PRAGMA integrity_check;"
   # Expected output: ok
   ```

2. **Copy snapshot into target**:
   ```bash
   cp "$SNAPSHOT_PATH" "$MEM_DATA/memory.db"
   ```

---

## 4. Post-Restoration Verification & Re-indexing

1. **Run SQLite & Knowledge Graph Integrity Checks**:
   ```bash
   venv/bin/python memory_integrity.py "$MEM_DATA/memory.db"
   ```
   Verify that output reports `0 critical issues`.

2. **Rebuild FTS5 and Vector Indexes**:
   In accordance with Hard Rule 3 (vec rebuild occurs after index updates):
   ```bash
   # Re-index markdown files into FTS5
   venv/bin/python cron/cron_rebuild_fts.py

   # Rebuild vector index
   venv/bin/python rebuild_vec_index.py
   ```

3. **Restart background worker / daemons**:
   ```bash
   venv/bin/python background_worker.py --once
   ```

---

## 5. Emergency Rollback

If the restored database fails integrity checks, roll back to the pre-restore copy created in step 2:
```bash
mv "$MEM_DATA/memory.db.pre-restore-"* "$MEM_DATA/memory.db"
```
