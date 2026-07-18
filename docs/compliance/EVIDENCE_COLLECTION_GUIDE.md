# Evidence Collection Guide

**Version:** 1.0
**Effective Date:** 2026-07-19
**Owner:** System Administrator
**Review Cycle:** Annual

---

## 1. Purpose

This guide describes how to collect, organize, and present compliance evidence for SOC 2 audits and internal reviews.

## 2. Evidence Categories

### 2.1 Access Control Evidence (CC6.1)

| Evidence | Source | How to Collect |
|----------|--------|----------------|
| Principal list | `principals` table | Dashboard → Compliance → RBAC → Principals |
| Role bindings | `role_bindings` table | Dashboard → Compliance → RBAC → Current Bindings |
| ACL overrides | `acl_overrides` table | Dashboard → Compliance → ACL Rules → Current Rules |
| Access review records | `memory_audit_log` | Dashboard → Audit → Filter by authorization decisions |

**Export Command:**
```sql
SELECT * FROM principals;
SELECT * FROM role_bindings;
SELECT * FROM acl_overrides;
SELECT ts, tool, error FROM memory_audit_log WHERE error LIKE '%authorization%';
```

### 2.2 System Operations Evidence (CC7.2)

| Evidence | Source | How to Collect |
|----------|--------|----------------|
| Audit log entries | `memory_audit_log` | Dashboard → Audit → Export CSV |
| Dead letter log | `audit_sink_dead_letter.jsonl` | Dashboard → Compliance → SOC 2 → Dead Letter Log |
| Failed operations | `task_queue` (status=failed) | Dashboard → Operations → Scheduled Jobs |
| System health checks | Compliance dashboard | Dashboard → Compliance → Health Check |

**Export Command:**
```sql
SELECT ts, tool, latency_ms, error FROM memory_audit_log ORDER BY ts DESC;
SELECT * FROM task_queue WHERE status='failed';
```

### 2.3 Data Protection Evidence

| Evidence | Source | How to Collect |
|----------|--------|----------------|
| GDPR erasure requests | `gdpr_requests` table | Dashboard → Compliance → GDPR → Erasure History |
| Deletion certificates | `memory/backups/` directory | File system listing |
| Backup verification | `cron_backup_validate` logs | Dashboard → Operations → Scheduled Jobs |
| Data retention compliance | `memory_archive` table | Dashboard → Quality → Staleness Report |

### 2.4 Tenant Isolation Evidence

| Evidence | Source | How to Collect |
|----------|--------|----------------|
| Tenant data distribution | `memories` table (tenant_id) | Dashboard → Compliance → Tenants → Data by Tenant |
| Cross-tenant access attempts | `memory_audit_log` | SQL query for cross-tenant operations |
| Isolation feature flags | `memory.toml` | Dashboard → Compliance → Tenants → Isolation Controls |

**Export Command:**
```sql
SELECT tenant_id, COUNT(*) FROM memories GROUP BY tenant_id;
SELECT * FROM memory_audit_log WHERE tool LIKE '%cross%tenant%';
```

### 2.5 Configuration Evidence

| Evidence | Source | How to Collect |
|----------|--------|----------------|
| Policy hash status | `cron_check_config_drift` | Dashboard → Compliance → Policy → Check Status |
| Feature flags | `memory.toml` | Dashboard → Settings → Feature Flags |
| Schema version | `schema_version` table | Dashboard → Dashboard → Health → Schema |

**Export Command:**
```sql
SELECT * FROM schema_version;
```

## 3. Evidence Export Procedures

### 3.1 Dashboard Exports

The compliance dashboard provides one-click exports:
- **Audit Log**: Dashboard → Audit → Export CSV/JSON
- **Memories**: Dashboard → Settings → Export → JSON/CSV
- **Knowledge Graph**: Dashboard → Settings → Export → JSON/GraphML

### 3.2 SQL Exports

For detailed evidence, run SQL queries directly:
```bash
cd /Users/arka/.config/agentic-memory
sqlite3 memory/memory.db ".mode csv" ".headers on" \
  "SELECT * FROM memory_audit_log" > audit_log_export.csv

sqlite3 memory/memory.db ".mode csv" ".headers on" \
  "SELECT * FROM principals" > principals_export.csv

sqlite3 memory/memory.db ".mode csv" ".headers on" \
  "SELECT * FROM role_bindings" > bindings_export.csv
```

### 3.3 Log Exports

```bash
# Export dead letter log
cp memory/audit_sink_dead_letter.jsonl evidence/dead_letter_export.jsonl

# Export recent worker logs
tail -1000 memory/background_worker.launchd.log > evidence/worker_log_export.log

# Export scheduler logs
tail -1000 memory/scheduler.log > evidence/scheduler_log_export.log
```

## 4. Evidence Organization

### 4.1 Directory Structure
```
evidence/
├── access_control/
│   ├── principals.csv
│   ├── role_bindings.csv
│   └── acl_overrides.csv
├── audit_trail/
│   ├── audit_log_export.csv
│   └── dead_letter_export.jsonl
├── data_protection/
│   ├── gdpr_requests.csv
│   └── deletion_certificates/
├── tenant_isolation/
│   ├── tenant_distribution.csv
│   └── isolation_config.json
├── configuration/
│   ├── schema_version.csv
│   ├── feature_flags.json
│   └── policy_hash_status.json
└── evidence_index.md
```

### 4.2 Evidence Index

Create an `evidence_index.md` file documenting:
- Date of collection
- Collection method
- Data range covered
- Any anomalies found
- Collector identity

## 5. SOC 2 Mapping

### 5.1 Trust Service Criteria Mapping

| Criteria | Controls | Evidence |
|----------|----------|----------|
| CC6.1 | RBAC, ACL, tenant isolation | Principals, bindings, ACL rules, tenant data |
| CC6.2 | Authentication | Principal identity resolution |
| CC7.1 | Change management | Git history, migration files |
| CC7.2 | Audit logging | Audit log, dead letter log |
| CC8.1 | Configuration management | Policy hash, feature flags |

### 5.2 Evidence Retention

| Evidence Type | Retention Period |
|---------------|------------------|
| Access control records | 1 year |
| Audit logs | 1 year |
| GDPR requests | 7 years |
| Backup records | 1 year |
| Configuration snapshots | 1 year |
| Incident reports | 3 years |

## 6. Audit Preparation Checklist

### 6.1 Pre-Audit (2 weeks before)
- [ ] Run compliance health check
- [ ] Export all evidence categories
- [ ] Review access control matrix
- [ ] Verify backup integrity
- [ ] Check audit trail completeness
- [ ] Review GDPR request history

### 6.2 During Audit
- [ ] Provide evidence exports on request
- [ ] Demonstrate dashboard functionality
- [ ] Show live control operation
- [ ] Answer auditor questions
- [ ] Provide system access for review

### 6.3 Post-Audit
- [ ] Address any findings
- [ ] Update controls as needed
- [ ] Document lessons learned
- [ ] Schedule follow-up review

---

*Evidence collection is supported by the Compliance dashboard, which provides real-time access to all control data and one-click export functionality.*
