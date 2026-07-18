# Data Retention Policy

**Version:** 1.0
**Effective Date:** 2026-07-19
**Owner:** System Administrator
**Review Cycle:** Annual

---

## 1. Purpose

This policy defines how long data is retained in the agentic-memory system, when it is deleted, and who authorizes retention and deletion actions.

## 2. Scope

This policy applies to all data stored in the agentic-memory system:
- Memory notes (content, metadata, embeddings)
- Knowledge graph entities and edges
- Audit logs
- Backup files
- Configuration data

## 3. Retention Periods

### 3.1 Memory Notes

| Category | Retention | Auto-Archive | Auto-Delete |
|----------|-----------|--------------|-------------|
| Lessons | Indefinite | After 180 days, fitness < 0.3 | Never |
| Decisions | Indefinite | After 180 days, fitness < 0.3 | Never |
| Projects | Project lifecycle + 90 days | After project completion | After 1 year |
| Sessions | 90 days | After 30 days | After 90 days |
| Preferences | Indefinite | Never | Never |

### 3.2 Knowledge Graph

| Data Type | Retention | Action |
|-----------|-----------|--------|
| Entities | Indefinite (unless orphaned) | Orphan cleanup after 90 days |
| Edges | Indefinite (unless orphaned) | Orphan cleanup after 90 days |
| Facts | Indefinite (unless superseded) | Superseded facts retained for audit |

### 3.3 Audit Data

| Data Type | Retention | Action |
|-----------|-----------|--------|
| Audit log | 1 year | Archive after 1 year |
| Dead letter log | 90 days | Auto-purge after 90 days |
| GDPR requests | 7 years | Legal retention requirement |

### 3.4 Backups

| Backup Type | Retention | Max Count |
|-------------|-----------|-----------|
| Daily backups | 7 days | 7 |
| Weekly backups | 4 weeks | 4 |
| Monthly backups | 12 months | 12 |

## 4. Auto-Archive Process

### 4.1 Staleness Detection
Memories are flagged for archival when:
- `fitness_score < 0.3` (low relevance)
- `created_at > 90 days ago` (age threshold)
- `access_count = 0` (never accessed)

### 4.2 Archive Workflow
1. Stale memories moved to `memory_archive` table
2. Original memories deleted from `memories` table
3. Archive entry includes: original ID, content, metadata, archived_at, reason
4. Archive is irreversible (no restore capability)

### 4.3 Manual Override
Administrators can:
- Pin memories to prevent archival
- Manually archive memories before threshold
- Adjust staleness thresholds in `memory.toml`

## 5. Backup Management

### 5.1 Automated Backups
- Daily backup via `cron_backup` job
- Backup stored as gzip-compressed SQLite
- Backup validation via `cron_backup_validate` job

### 5.2 Backup Location
Backups stored in `memory/backups/` directory.

### 5.3 Backup Verification
Each backup is validated for:
- Gzip decompression integrity
- SQLite format validity
- Schema version match
- Table completeness

## 6. Deletion Procedures

### 6.1 Soft Delete
Most deletions are soft deletes:
- `deleted_at` timestamp set
- `deleted_by` principal recorded
- Data retained for 30 days before hard delete

### 6.2 Hard Delete
Hard deletion occurs:
- After 30-day soft delete period
- Via GDPR erasure request
- Via manual administrator action

### 6.3 GDPR Erasure
Right-to-be-forgotten requests:
1. All memories containing data subject identifier deleted
2. All KG entities referencing data subject deleted
3. Deletion certificate generated with timestamp
4. Certificate stored for audit trail

## 7. Backup Cleanup

### 7.1 Retention Enforcement
The `cron_purge_expired` job runs monthly to:
- Remove backups older than retention period
- Archive old audit logs
- Clean up stale auto-save entries

### 7.2 Manual Cleanup
Administrators can trigger cleanup via:
- Dashboard: Quality tab → Optimize → Archive Stale
- CLI: `memory_maintenance(operation="purge_expired")`

## 8. Exceptions

Data retention exceptions require:
- Written business justification
- Legal review (for GDPR/CCPA compliance)
- Approval from system administrator
- Documentation in compliance records

## 9. Monitoring

The compliance dashboard monitors:
- Backup age and count
- Memory staleness distribution
- Archive growth rate
- Retention policy compliance

---

*Retention policies are enforced by cron jobs and the background worker. The Quality tab provides visibility into data lifecycle status.*
