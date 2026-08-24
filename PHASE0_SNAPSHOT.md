# Phase 0 Baseline Snapshot

Captured: 2026-08-24T13:12:30+05:30
Branch: feat/prune-mcp-surface

## Registry Counts
- CORE_TOOLS: 25
- ADMIN_TOOLS: 92
- DEPRECATED: 3
- Total Tools: 120

## Skill System State
- SQLite memory_skills count: 0
- ~/.agents/skills directory count: 171
- Flagship junk skills present:
  - comprehensive-git-commit-activity-report-last-10-commits
  - git-commit-report-recent-commits
  - recent-commit-report
  - session-summary-auto-derived-at-compaction
  - summary-overview
  - the-deploy-service-configuration-is-disabled-for-production
  - the-deploy-service-configuration-is-enabled-for-production
  - this-is-a-test-memory-about-integrity-checking
- Backup created: ~/.agents/skills.bak (171 dirs)

## Baselines
- Rule enforcement tests: PASSED (test_rule6_mcp_tool_surface_contract, test_rule21_no_ritual_maintenance)
- IDE tests: PASSED (pnpm -r test: 204 test files passed, 2101 tests passed)
- Docs meta: provenance.last_meta_regenerated = 2026-08-24
