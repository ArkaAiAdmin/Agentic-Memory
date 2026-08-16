# How to Run a Schema Migration

## Goal

Add or apply a schema change to the agentic-memory SQLite database — adding tables, columns, indexes, or constraints through the numbered migration system.

## Prerequisites

- [ ] Python 3.10+
- [ ] Access to the `migrations/` directory and `migration_runner.py`
- [ ] A backup of the current database: `venv/bin/python cron/cron_backup.py`
- [ ] Understanding of SQLite schema limitations (e.g., `ALTER TABLE` support)

## When to use this

- Adding a new table, column, index, or constraint.
- Changing the shape of an existing table (e.g., adding a CHECK constraint).

## When NOT to use this

- You're just changing data (use `memory_save` or `memory_advanced`).
- You're rebuilding an index (use `rebuild_*.py` or `memory_advanced(operation="rebuild")`).

## The current schema version

The current schema version is published as `migration_runner.SCHEMA_VERSION`.  Migrations are auto-discovered from the `migrations/` directory by `_get_available_migrations()` (sorted by numeric prefix), so you do **not** maintain a list — drop a file in and bump the version constant.

```bash
venv/bin/python -c "
import migration_runner
print('SCHEMA_VERSION:', migration_runner.SCHEMA_VERSION)
"
```

## Steps to add a new migration

1. **Create two files** in `migrations/`:

   - `migrations/017_your_change.sql` (forward)
   - `migrations/017_your_change.down.sql` (rollback)

   Example `migrations/017_add_memory_audit_metadata.sql`:

   ```sql
   -- Forward: add metadata column
   ALTER TABLE memory_audit_log ADD COLUMN metadata TEXT DEFAULT '{}';
   CREATE INDEX IF NOT EXISTS idx_audit_metadata ON memory_audit_log(json_extract(metadata, '$.user_id'));
   ```

   Example `migrations/017_add_memory_audit_metadata.down.sql`:

   ```sql
   -- Rollback: drop index, drop column (SQLite supports DROP COLUMN since 3.35)
   DROP INDEX IF EXISTS idx_audit_metadata;
   ALTER TABLE memory_audit_log DROP COLUMN metadata;
   ```

2. **Bump `SCHEMA_VERSION`** in `migration_runner.py`. The new
   value must equal the highest migration number on disk — the
   runner uses it as the target version.  No `MIGRATIONS = [...]`
   list to update; the runner discovers files via glob.

3. **Apply** the migration:

   ```bash
   venv/bin/python migration_runner.py --db memory/memory.db
   # Or, in Python:
   # from migration_runner import run_migrations
   # from memory_common import open_db
   # with open_db("memory/memory.db", timeout=10.0) as conn:
   #     run_migrations(conn)
   ```

## Verification

```bash
venv/bin/python memory_integrity.py memory/memory.db
venv/bin/python -m pytest eval/ -q
```

Expected output: `memory_integrity` exits with exit code 0; pytest reports no failures.

5. **Update `memory_workflow.md`** (Database Tables section) to document the new column or table.

## Patterns

### Adding a new column

```sql
-- Up
ALTER TABLE memories ADD COLUMN source_url TEXT;
CREATE INDEX IF NOT EXISTS idx_memories_source_url ON memories(source_url);
```

```sql
-- Down
DROP INDEX IF EXISTS idx_memories_source_url;
ALTER TABLE memories DROP COLUMN source_url;
```

### Adding a new table

```sql
-- Up
CREATE TABLE IF NOT EXISTS my_new_table (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_my_new_table_valid_to ON my_new_table(valid_to);
```

```sql
-- Down
DROP TABLE IF EXISTS my_new_table;
```

### Adding a CHECK constraint

```sql
-- Up
-- SQLite doesn't support adding CHECK to existing column directly.
-- Recreate the table or add a trigger.
CREATE TRIGGER IF NOT EXISTS my_table_status_check
BEFORE INSERT ON task_queue
WHEN NEW.status NOT IN ('pending', 'processing', 'completed', 'failed')
BEGIN
    SELECT RAISE(ABORT, 'invalid status');
END;
```

## Troubleshooting

### Migration not applied

**Cause**: The migration number is higher than `SCHEMA_VERSION` or lower than the current applied version.
**Fix**: Check `SCHEMA_VERSION` in `migration_runner.py` — it must match the highest migration number on disk. Run `venv/bin/python migration_runner.py --db memory/memory.db --dry-run` to preview.

### Rollback fails

**Cause**: The down migration references a table or column that doesn't exist.
**Fix**: Write down migrations idempotently using `IF EXISTS`. Test by running apply → rollback → re-apply in a test database.

### Migration causes data loss

**Cause**: A `DROP COLUMN` or `DROP TABLE` was run without verifying the data is safe to lose.
**Fix**: Never drop columns or tables with live data. Use soft-delete via `valid_to = datetime.now()` instead.

### Additional pitfalls

- **Don't edit the live DB schema by hand.** Always go through a migration.
- **Don't use `ALTER TABLE ... RENAME` without a down migration.** Rollback must work.
- **Don't add NOT NULL columns without a DEFAULT.** Existing rows would fail.
- **Don't drop a table that has data you might need.** Soft-delete via `valid_to` instead.
- **Don't run migrations on a hot DB without a backup.** Run `cron/cron_backup.py` first.
- **Don't forget the index.** A migration that adds a column that's queried will be slow without an index.

## Testing

A migration test pattern:

```python
def test_migration_017():
    # Apply migration
    apply_migration(17)
    # Verify schema
    cols = [r[1] for r in c.execute("PRAGMA table_info(my_table)").fetchall()]
    assert "new_column" in cols
    # Rollback
    rollback_migration(17)
    cols = [r[1] for r in c.execute("PRAGMA table_info(my_table)").fetchall()]
    assert "new_column" not in cols
    # Re-apply for state
    apply_migration(17)
```

## Related

- All migrations: `migrations/NNN_*.sql` (auto-discovered by numeric prefix)
- Migration runner: `migration_runner.py` (`SCHEMA_VERSION` is the only knob to bump)
- Auto-discovery helper: `_get_available_migrations()` (sorted by `int(stem.split('_')[0])`)
- Down-migration helper: `_get_down_migrations()` (matches `*.down.sql`)
- Schema documentation: `memory_workflow.md` (Database Tables section)
- [Self-Hosting](../self-hosting.md) — Production deployment
- [Add a Cron Job](add-a-cron-job.md) — For recurring maintenance
