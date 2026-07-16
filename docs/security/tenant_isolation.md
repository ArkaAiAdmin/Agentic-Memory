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

## Schema (Migrations 042-067)

- **042**: `memories.tenant_id TEXT NOT NULL DEFAULT 'default' CHECK(tenant_id != '')`
- **043**: `principals` + `principal_identities` tables (SSO round-tripping)
- **044**: `memory_audit_log` gets `tenant_id` + `principal_id` columns
- **050**: `kg_facts`, `kg_entities` get `tenant_id` column
- **052**: backfills `kg_facts`/`kg_entities` `tenant_id` from parent memory
- **055**: `kg_entity_crdt`, `kg_edge_crdt` get `tenant_id` column
- **066**: `kg_entity_crdt`/`kg_edge_crdt` get `applied` + `kg_entity_crdt.fingerprint`
- `memory_field_crdt` carries `tenant_id` + `last_writer_agent` (field-level LWWES)

## Known Gaps (remaining)

- Audit log `tenant_id`/`principal_id` population on writes is best-effort
- Worker/cron now read tenant_id from task payload (Sprint 1.2), but some crons still iterate all tenants by design

## Sprint 3 Improvements (2026-07-16)

- Added `resolve_tenant_for_principal()` helper for MCP tools
- Worker now reads tenant_id from task payload instead of hardcoding "default"
- Cron subprocess tenant isolation: `background_worker.py` passes `MEMORY_CRON_TENANT_ID`
  into each cron subprocess env; 10 cron scripts call `install_tenant_context(conn,
  tenant_id)` after opening their connection so tenant-scoped views/queries apply
  (fixes the `cron_train_ltr` crash that queried `tenant_memories` on a connection
  that had not registered the tenant function)
- All 75 tenant isolation tests passing

## Test Coverage

`eval/test_tenant_isolation_exhaustive.py` — 75 tests, ~200 assertions covering:
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
