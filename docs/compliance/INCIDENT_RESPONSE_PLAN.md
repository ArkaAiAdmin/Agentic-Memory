# Incident Response Plan

**Version:** 1.0
**Effective Date:** 2026-07-19
**Owner:** System Administrator
**Review Cycle:** Annual

---

## 1. Purpose

This plan defines how security incidents affecting the agentic-memory system are detected, contained, eradicated, recovered, and documented.

## 2. Scope

This plan covers:
- Unauthorized access attempts
- Data breaches or exfiltration
- System compromise or malware
- Service disruption (DDoS, resource exhaustion)
- Configuration drift or policy violations
- Failed compliance controls

## 3. Incident Classification

### 3.1 Severity Levels

| Level | Description | Response Time | Escalation |
|-------|-------------|---------------|------------|
| P1 - Critical | Active breach, data exfiltration, system compromise | Immediate | System administrator + security team |
| P2 - High | Unauthorized access, failed access controls, audit trail gaps | Within 1 hour | System administrator |
| P3 - Medium | Policy violations, configuration drift, failed backups | Within 24 hours | System administrator |
| P4 - Low | Anomalous behavior, minor policy deviations | Within 72 hours | System administrator |

### 3.2 Incident Types

| Type | Example | Severity |
|------|---------|----------|
| Unauthorized Access | Principal accesses resource without role binding | P2 |
| Data Exfiltration | Bulk memory export without authorization | P1 |
| System Compromise | Malware injection, unauthorized code execution | P1 |
| Audit Trail Gap | Dead letter log entries spike | P2 |
| Configuration Drift | Policy hash mismatch across agents | P3 |
| Backup Failure | Multiple consecutive backup failures | P3 |

## 4. Detection

### 4.1 Monitoring Sources

| Source | What It Catches | Alert Threshold |
|--------|-----------------|-----------------|
| `memory_audit_log` | Failed authorization attempts | >5 failures/hour |
| `dead_letter.jsonl` | Audit sink failures | >10 entries/hour |
| `drift_alarms` | Concept drift events | Critical alarms |
| `task_queue` | Failed background tasks | >3 failures/hour |
| Compliance dashboard | Control failures | Any P1/P2 failure |

### 4.2 Automated Alerts

The system generates alerts via:
- Dashboard health checks (real-time)
- Cron job monitoring (hourly)
- Heartbeat monitoring (minute-level)

## 5. Response Procedure

### 5.1 Immediate Actions (P1/P2)

1. **Contain**: Revoke affected principal's access immediately
2. **Isolate**: Disable affected agent or service
3. **Preserve**: Do not delete logs or evidence
4. **Notify**: Alert system administrator

### 5.2 Investigation (All Severity)

1. **Gather Evidence**:
   - Export `memory_audit_log` for affected time period
   - Export `dead_letter.jsonl` for audit sink failures
   - Review `drift_alarms` for related events
   - Capture system state (DB snapshot, config files)

2. **Root Cause Analysis**:
   - Identify how the incident occurred
   - Determine scope of affected data
   - Assess whether data was exfiltrated
   - Review access control failures

3. **Containment Verification**:
   - Verify affected principals are revoked
   - Verify affected configurations are corrected
   - Verify audit trail is intact

### 5.3 Eradication

1. Remove any malicious code or configurations
2. Rotate any potentially compromised credentials
3. Update access control rules as needed
4. Patch any identified vulnerabilities

### 5.4 Recovery

1. Restore from last known good backup (if needed)
2. Verify system integrity via compliance health check
3. Re-enable affected services
4. Monitor for recurrence

## 6. Communication

### 6.1 Internal Notification

For P1/P2 incidents:
- System administrator notified immediately
- Incident documented in compliance records
- Post-incident review scheduled within 48 hours

### 6.2 External Notification

For data breaches affecting personal data:
- GDPR: Notify supervisory authority within 72 hours
- Affected data subjects notified without undue delay
- Documentation of notification preserved

## 7. Documentation

### 7.1 Incident Report

Every incident produces a report containing:
- Incident ID and timestamp
- Severity classification
- Affected systems and data
- Root cause analysis
- Containment actions taken
- Recovery actions taken
- Lessons learned
- Preventive measures

### 7.2 Evidence Preservation

Evidence is preserved for:
- Minimum 1 year for P1/P2 incidents
- Minimum 90 days for P3/P4 incidents
- Indefinite for regulatory inquiries

## 8. Post-Incident Review

### 8.1 Review Meeting

Within 5 business days of incident closure:
- Review incident timeline
- Identify control failures
- Update this plan if needed
- Implement preventive measures

### 8.2 Metrics

Track:
- Mean time to detect (MTTD)
- Mean time to respond (MTTR)
- Mean time to recover
- Number of incidents per quarter
- Recurrence rate

## 9. Testing

### 9.1 Tabletop Exercises

Quarterly tabletop exercises covering:
- Unauthorized access scenario
- Data exfiltration scenario
- System compromise scenario
- Audit trail gap scenario

### 9.2 Technical Drills

Monthly technical drills:
- Backup restoration test
- Access control verification
- Compliance health check
- Dead letter log review

---

*This plan is supported by technical controls in `infra/rbac.py`, `infra/authorizer.py`, and `infra/audit_sink.py`. The Compliance dashboard provides real-time visibility into incident-relevant metrics.*
