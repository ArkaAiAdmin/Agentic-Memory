# Access Control Policy

**Version:** 1.0
**Effective Date:** 2026-07-19
**Owner:** System Administrator
**Review Cycle:** Annual

---

## 1. Purpose

This policy defines how access to the agentic-memory system is controlled, including authentication, authorization, role assignment, and access review procedures.

## 2. Scope

This policy applies to all principals (agents, users, services) that access the agentic-memory system, including:
- AI agents (OpenCode, MIMOCODE, and future agents)
- Dashboard operators
- API consumers
- Background workers

## 3. Authentication

### 3.1 Principal Identity
Every entity accessing the system must have a registered principal identity in the `principals` table. Principals are categorized as:
- **agent**: Automated AI agents
- **user**: Human operators
- **service**: Background processes and integrations

### 3.2 Identity Resolution
Principal identity is resolved via:
- `MEMORY_AGENT_ID` environment variable
- API token mapping (SSO/OIDC)
- Session context

### 3.3 Fail-Closed Default
When no principals are configured, the system operates in **fail-open** mode (all access permitted). When principals are registered, the system switches to **fail-closed** mode (access denied unless explicitly granted).

## 4. Authorization

### 4.1 Role-Based Access Control (RBAC)
Access is controlled through roles assigned to principals:

| Role | Description | Permissions |
|------|-------------|-------------|
| `admin` | Full access | All operations |
| `writer` | Read and write | memory:read, memory:write, memory:delete |
| `reader` | Read-only | memory:read |

### 4.2 Role Bindings
Roles are assigned to principals via the `role_bindings` table. Each binding includes:
- `principal_id`: The entity being granted access
- `role_id`: The role being assigned
- `granted_at`: Timestamp of grant
- `granted_by`: Who authorized the grant

### 4.3 ACL Overrides
Per-principal access overrides can be set via `acl_overrides`:
- **allow**: Explicitly grant access (overrides role denial)
- **deny**: Explicitly deny access (overrides role allowance)

ACL rules are evaluated in order: deny > allow > role default.

## 5. Least Privilege

### 5.1 Default Deny
All access is denied by default. Access is only granted through explicit role assignment or ACL override.

### 5.2 Role Scoping
Roles are scoped to specific resources:
- `memory:*` — Memory operations
- `kg_*` — Knowledge graph operations
- `admin:*` — System administration
- `ops:*` — Operational data

### 5.3 Tenant Isolation
Principals are scoped to tenants. A principal in tenant A cannot access data in tenant B unless explicitly granted cross-tenant access.

## 6. Access Review

### 6.1 Quarterly Review
All role bindings and ACL overrides are reviewed quarterly:
- Verify all principals still require their assigned roles
- Remove stale bindings for decommissioned agents
- Review ACL overrides for necessity

### 6.2 Automated Monitoring
The compliance dashboard provides:
- Real-time permission matrix view
- Role binding audit trail
- ACL override history

## 7. Enforcement

### 7.1 MCP Tool Enforcement
Every MCP tool call passes through `mcp_authorize()` which:
1. Resolves the principal identity
2. Checks role bindings
3. Evaluates ACL overrides
4. Grants or denies access

### 7.2 Audit Trail
All authorization decisions are logged to `memory_audit_log` with:
- Principal ID
- Action attempted
- Resource accessed
- Decision (allow/deny)
- Timestamp

## 8. Exceptions

Any exception to this policy must be:
- Documented with business justification
- Approved by the system administrator
- Time-limited with automatic expiration
- Logged in the audit trail

## 9. Enforcement Actions

Violations of this policy result in:
1. Immediate access revocation
2. Audit log review
3. Principal suspension pending investigation
4. Permanent revocation for repeated violations

---

*This policy is enforced by the technical controls in `infra/rbac.py` and `infra/authorizer.py`. The compliance dashboard provides visibility into policy adherence.*
