# GDPR Right-to-Be-Forgotten: `gdpr_erase`

One-shot cascading wipe for data subject erasure requests
(Article 17 — Right to Erasure / Right to be Forgotten).

## Surface

| Layer | Entry point |
|---|---|
| **MCP (admin)** | `memory_maintenance(operation="gdpr_erase", principal_id=..., data_subject_sub=..., tenant_id=..., confirm=True)` |
| **REST** | `POST /api/v1/compliance/gdpr/erase` with JSON body `{"data_subject_sub": "...", "tenant_id": "..."}` |
| **Python** | `infra.gdpr.gdpr_erase(conn, principal_id, data_subject_sub, tenant_id)` |

## What it deletes

Scoped to a single `tenant_id`:

| Table | Filter |
|---|---|
| `memories` | `tenant_id = ?` |
| `kg_facts` | `source_memory IN (SELECT id FROM memories WHERE tenant_id = ?)` |
| `kg_entities` | Entities no longer referenced after their facts are removed |
| `kg_edges` | Edges referencing deleted entities |
| `backlinks` | `source_id` / `target_id` in the tenant's memory set |
| `memory_chunks` | Chunks of tenant memories |
| `memory_chunk_embeddings` | Embeddings of tenant chunks |
| `memory_chunk_vec_keys` | Vec keys of tenant chunks |
| `memory_embeddings` | Embeddings of tenant memories |
| `memory_vec_keys` | Vec keys of tenant memories |
| `memory_audit_log` | `tenant_id = ?` |
| `.md` files | On-disk files referenced by `source_file` column |

Shared KG entities (referenced by multiple tenants) are preserved:
an entity is only deleted when *no* facts reference it after the
tenant's facts are removed.

## Authorization

- **MCP gate:** `mcp_authorize(principal, "compliance", "gdpr-erase")`
  — requires the `compliance:gdpr-erase` role.
- **REST gate:** same RBAC check via `mcp_authorize`.
- **API token:** also requires a valid `Bearer` token matching
  `MEMORY_API_TOKEN`.

## Deletion Certificate

Every call produces a signed `DeletionCertificate`:

```json
{
  "request_id": "gdpr-abc123def456",
  "principal_id": "admin",
  "data_subject_hash": "<sha256 of subject sub>",
  "tenant_id": "tenant-a",
  "requested_at": "2026-07-11T10:00:00Z",
  "completed_at": "2026-07-11T10:00:01Z",
  "status": "completed",
  "rows_deleted": {"memories": 5, "kg_facts": 3, ...},
  "md_files_deleted": 2,
  "certificate_hash": "<sha256 of all above fields>"
}
```

The certificate is recorded in `gdpr_requests` for audit. The
`certificate_hash` is a SHA-256 over the sorted JSON of all fields
(signing proof; downstream systems can verify data integrity).

## Migration

Migration 049 creates the `gdpr_requests` tracking table.
Rollback: `migrations/049_gdpr_requests.down.sql`.

## Test coverage

| File | Scope |
|---|---|
| `eval/test_gdpr_erase_full_cascade.py` | All table types, tenant isolation |
| `eval/test_gdpr_erase_certificate.py` | Certificate structure, hash, failure recording |
| `eval/test_gdpr_erase_refuses_cross_tenant.py` | Cross-tenant data preservation |

## Security considerations

- **Destructive:** requires `confirm=True` (MCP) and RBAC role.
- **Fail-closed on error:** partial erasure records a `failed`
  certificate before re-raising the exception.
- **Hash chain:** the certificate hash prevents silent tampering
  with audit records.
- **Shared entities:** KG entities co-owned by multiple tenants are
  not deleted; only tenant-specific facts and edges are removed.
