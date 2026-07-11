# Tenant Isolation

Phase 0 hardening — prevents cross-tenant data leakage.

## Architecture

Every connection gets a `tenant_memories` TEMP VIEW that filters by `tenant_id()`:

```sql
CREATE TEMP VIEW tenant_memories AS
  SELECT * FROM memories WHERE tenant_id = tenant_id()
```

The `tenant_id()` SQLite function is registered per-connection via `connection_pool.get(tenant_id=...)`.

## Enforcement Points

| Layer | Mechanism | Files |
|-------|-----------|-------|
| Search | FTS JOIN via `tenant_memories` | `search/orchestrator.py` |
| Save | `tenant_id` column on insert | `save/pipeline.py` |
| Delete | `tenant_id` param on all delete ops | `memory_delete.py` |
| REST API | `is_global` default `False` | `infra/api_server.py` |
| Client | `get_db_connection` detects agent ctx | `agentic_memory/utils.py` |
| Background worker | `tenant_memories` view | `background/background_worker.py` |
| Sync server/client | `tenant_memories` view | `infra/sync_server.py`, `infra/sync_client.py` |

## Schema (Migrations 042-052)

- **042**: `memories.tenant_id TEXT NOT NULL DEFAULT 'default' CHECK(tenant_id != '')`
- **043**: `principals` + `principal_identities` tables (SSO round-tripping)
- **044**: `memory_audit_log` gets `tenant_id` + `principal_id` columns
- **050**: `kg_facts`, `kg_entities` get `tenant_id` column
- **052**: backfills `kg_facts`/`kg_entities` `tenant_id` from parent memory

## Known Gaps (remaining)

- `memory_field_crdt` lacks agent/tenant columns
- SyncServer lacks `tenant_id` filtering on reads
- Audit log `tenant_id`/`principal_id` population on writes is best-effort
- GDPR erase resolves tenant from the principal, not the request body (fixed in Phase hardening)

## Test Coverage

`eval/test_tenant_isolation_exhaustive.py` — 62 tests, ~200 assertions covering:
- Search isolation (8 tests)
- Write isolation (8 tests)
- Delete isolation (7 tests)
- REST API isolation (9 tests)
- KG isolation (5 tests)
- Vector index isolation (5 tests)
- FTS isolation (5 tests)
- Audit log tenant (7 tests)
- CRDT isolation (8 tests)
- Sync isolation (10 tests)
