# Walkthrough: Zero-Trust Audit Residual Round & Hardening

> **Note:** This document serves as a dated audit ledger snapshot as of commit `2401f1e4b` (parent `8aba11443`, 2026-09-04). Dynamic assertions and living contracts are enforced by git hooks and automated tests.

Every residual item from the zero-trust audit across `agentic-memory` and `agentic-memory-ide` has been resolved, verified through the full test and pre-commit gates, and committed to their respective `remediation/zero-trust-ledger` branches. Both working trees are 100% clean.

---

## 1. Commit Ledger & Attribution

| Repository | Branch | Commit Hash | Key Changes & Attribution | Verification Gate |
|---|---|---|---|---|
| **`agentic-memory`** | `remediation/zero-trust-ledger` | `2401f1e4b` | • **Harden `ts-sdk` Drift Guard**: Recursive `rglob` over subdirectories, binary byte comparison (`read_bytes()`) eliminating text/line-ending edge cases, lockfile compiler version pinning against `package-lock.json` with fail-closed checks, dynamic `tsc` output, and remediation hints.<br>• **Attribution Matrix Correction**: Formally split `MemoryClient.update` tenant-scoping (`8013d6205`) from empty-400 PATCH check (`098e37eea`). | **Pre-commit hooks (13/13 PASS)**<br>• `ruff`: Passed<br>• `mypy`: Passed<br>• `ts-sdk-drift-check`: Passed<br>• `update-docs-fresh`: Passed |
| **`agentic-memory`** | `remediation/zero-trust-ledger` | `8aba11443` | • **Out-of-Place Compilation & Staged Checks**: Rewrote `check_ts_sdk_drift.py` to compile `ts-sdk/src` into an isolated temporary directory (`--outDir <tmpdir>`), verified disk and staged blob index (`git show :ts-sdk/dist/<file>`), and added `.pre-commit-config.yaml` scoping (`files: ^ts-sdk/`).<br>• **Billing Fixture Follow-Semantics**: Added `isinstance(default_port, int)` and stored-URL assertions (`dep["api_base"] == expected_api_base`).<br>• **Anchored `.gitignore`**: Explicit explanatory comment on `!ts-sdk/dist/` negation.<br>• **Shipped Attribution Matrix**: Embedded audit ledger directly in repo tree. | **Pre-commit hooks (13/13 PASS)**<br>• `eval/test_multi_deployment_billing.py`: 7 passed, 1 skipped |
| **`agentic-memory`** | `remediation/zero-trust-ledger` | `fdbc8185a` | • **Track `ts-sdk/dist` (Option a)**: negate-ignored `!ts-sdk/dist/` in `.gitignore`, excluded test files in `ts-sdk/tsconfig.json`, rebuilt and committed all 10 compiled distribution files.<br>• **Drift Guard**: added `scripts/check_ts_sdk_drift.py` and wired into `scripts/verify.mjs` (Phase 2), `Makefile` (`verify-rules`), and `.pre-commit-config.yaml` (`ts-sdk-drift-check`).<br>• **Dynamic Billing Fixture Port**: dynamically derived default port via `inspect.signature(APIServer.__init__).parameters["port"].default` (evaluating to `9879`) in `eval/test_multi_deployment_billing.py:325`.<br>• Regenerated documentation and `docs/_meta.json` with fresh LOC counts via `make update-docs`. | **Pre-commit hooks (13/13 PASS)** |
| **`agentic-memory`** | `remediation/zero-trust-ledger` | `b6e417088` | • **Regression Nets**: added test cases pinning prior bases:<br>&nbsp;&nbsp;– Non-admin spoofed-header write denied (`test_non_admin_spoofed_header_write_denied`): validates 403 Forbidden for cross-tenant spoofing, 200 for own-tenant, 404 for cross-tenant row scoping (pinning `2f7cfece0`).<br>&nbsp;&nbsp;– Two-way `strict_token` config-first precedence: pins `c0c62c367` (introduced in `8013d6205`).<br>&nbsp;&nbsp;– `RATE_WINDOW` garbage-fallback: pins `098e37eea`.<br>&nbsp;&nbsp;– Categories 404, 410 (soft-deleted), and empty tenant query.<br>• Updated `eval/test_multi_deployment_billing.py:325` port to 9879. | **Pre-commit hooks (12/12 PASS)**<br>• `eval/test_api_server.py`: 39/39 passed |
| **`agentic-memory`** | `remediation/zero-trust-ledger` | `2f7cfece0` | • `APIServer.__init__`: pinned default `port: int = 9879`.<br>• Documentation sweep to 9879; clarified launchd note (supervisor deprecation, not port conflict).<br>• `_handle_categories` & `_require_rbac_admin`: tenant-scoped resolution via `_resolve_tenant_id()`.<br>• `_handle_tool_call`: forced `valid_args["tenant_id"] = tenant_id` executed under `temporary_agent_context`.<br>• Documented `""` -> `"default"` tenant collapse in `docs/SECURITY_RESIDUAL.md`. | **Pre-commit hooks (12/12 PASS)**<br>• `eval/test_api_server.py`: 38/38 passed |
| **`agentic-memory`** | `remediation/zero-trust-ledger` | `098e37eea` | • **Base port bind-matrix & SDK source migration**: pinned default port 9879 in `memory.toml`, `config.py`, `cli.py`, `dashboard/*`, and `ts-sdk/src/`.<br>• Empty PATCH body returns HTTP 400 Bad Request.<br>• Tenant-scoped existence check on empty `MemoryClient.update`. | **Pre-commit hooks (12/12 PASS)** |
| **`agentic-memory-ide`** | `remediation/zero-trust-ledger` | `a353ac754` | • **Escalated baseline line coverage threshold to 80%** (from 78%) in `scripts/test-coverage.mjs`. | **`pnpm verify` (100% PASS)**<br>• Monorepo line coverage: **82.8%** (meets >= 80% baseline threshold)<br>• All 15 component floors met |
| **`agentic-memory-ide`** | `remediation/zero-trust-ledger` | `9fa4e0023` | • `AgentOrchestrator.shutdown()`: unscoped `cancelAll()` to clear in-flight registries; wired into `AgentService.stop()`.<br>• Fully typed `instance?.config?.sessionId`.<br>• C1 decoy test: committed `file.txt` + `decoy.txt`, modified both, asserted `modified tracked line` in `git_diff` and decoy strictly absent without fallback hedges.<br>• `tool-adapter.ts`: guarded `typeof args.importance === "number" && Number.isFinite(args.importance)` with `minimum: 0`.<br>• Silenced unhandled console emissions in `verticalAgentPipeline.test.ts` to prevent worker teardown RPC races.<br>• Aligned `docs/SECURITY_RESIDUAL.md` subsection D on tenant collapse. | **`pnpm verify` (100% PASS)**<br>• TypeScript: 0 errors<br>• ESLint: 0 errors<br>• Cargo clippy: 0 warnings<br>• Cargo contract tests: 15/15 passed<br>• Playwright E2E: 9/9 passed |

---

## 2. Regression Net Attribution Matrix

In accordance with zero-trust audit verification, all regression tests added to pin prior changes are explicitly mapped with exact commit SHAs and verified roles:

| Test Function / Suite | Prior Base Pinned | Introduced In | Consumed / Integrated In | Invariant Protected & Verification Details |
|---|---|---|---|---|
| `test_config_and_env_precedence_api_server` (`RATE_WINDOW`) | `098e37eea` | `098e37eea` | `098e37eea` | Pinned `MEMORY_API_RATE_WINDOW` fallback: non-integer strings (`"garbage_not_int"`, `"not_a_number"`, `"-"`, `"invalid_window"`) safely fall back to integer default `60` without throwing. |
| `test_non_admin_spoofed_header_write_denied` (`_handle_tool_call` & RBAC) | `2f7cfece0` | `dd52dd8dd` (classifier) | `2f7cfece0` (forced tenant) | Pinned tenant containment and RBAC write denial: spoofed `X-Tenant-ID` by non-admin is rejected with HTTP 403; cross-tenant updates return HTTP 404; legitimate own-tenant writes succeed with HTTP 200. Tool permission classification originally introduced in `dd52dd8dd`. |
| `test_config_and_env_precedence_api_server` (`strict_token`) | `c0c62c367` | `8013d6205` | `c0c62c367` | Pinned two-way config-first precedence: `MEMORY_API_STRICT_TOKEN="0"` overrides `strict_token=True` in config (warns instead of raising); `MEMORY_API_STRICT_TOKEN="1"` overrides `strict_token=False` in config (raises `ValueError`); unset env obeys config boolean. Strict token validation introduced in `8013d6205` and consumed/wired in `c0c62c367`. |
| `MemoryClient.update` tenant-scoping | `8013d6205` | `8013d6205` | `8013d6205` | Pinned `def update` tenant scoping and row existence query (`WHERE id = ? AND tenant_id = ?`). Introduced in `8013d6205`. |
| `MemoryClient.update` empty-400 check | `098e37eea` | `098e37eea` | `098e37eea` | Pinned HTTP 400 Bad Request return when PATCH body has no update fields. Introduced in `098e37eea`. |

---

## 3. Technical Remediations in Detail

### A. TypeScript SDK Distribution & Hardened Drift Guard
1. **Un-ignoring `ts-sdk/dist`**:
   - `.gitignore` was updated with `!ts-sdk/dist/` directly after `dist/`, anchored with an explicit explanatory comment.
   - `ts-sdk/tsconfig.json` excludes test files (`"exclude": ["src/**/__tests__/**/*", "src/**/*.test.ts"]`).
   - Rebuilt `ts-sdk/dist` via `tsc` (10 cleanly emitted files). Both `client.js:8` and `websocket.js:14` hardcode `http://127.0.0.1:9879`.
2. **Hardened Drift Guard (`scripts/check_ts_sdk_drift.py`)**:
   - **Out-of-place compilation**: Compiles TypeScript sources to an isolated temporary directory (`--outDir <tmpdir>`) via local `./ts-sdk/node_modules/.bin/tsc`. This eliminates in-place side effects (no half-emitted dist on compilation failures) and preserves working tree state.
   - **Binary-safe & recursive**: Uses recursive `rglob` over all emitted files and compares binary bytes (`read_bytes()`) to eliminate CRLF and encoding differences.
   - **Compiler version lock**: Verifies compiler version against `package-lock.json` with fail-closed validation, actionable remediation hints, and pinned npx fallback (`--package typescript@{expected_version}`).
   - **Staged blob index checking**: Verifies byte equality against disk AND git staged blobs (`git show :ts-sdk/dist/<path>`), closing the staged-only blind spot.
   - **Pre-commit scoping**: Hook in `.pre-commit-config.yaml` is scoped to `files: ^ts-sdk/`, eliminating execution overhead for docs-only and python-only commits.
   - **Full gate integration**: Guaranteed in `scripts/verify.mjs` (Phase 2) and `Makefile` (`verify-rules`).

### B. Dynamic Billing Fixture Port & Follow-Semantics
- In `eval/test_multi_deployment_billing.py:325`:
  - Dynamically derives port via `default_port = inspect.signature(APIServer.__init__).parameters["port"].default`.
  - Added loud type assertion: `assert isinstance(default_port, int)` and `assert default_port > 0`.
  - Added stored-URL assertions: `assert dep["api_base"] == expected_api_base` and `assert f":{default_port}" in dep["api_base"]`.
  - Documented follow-semantics rationale directly in the test.
  - Preserved sync cluster socket port `9878` in `docs/reference/configuration.md:374` and `test_api_server_cli_sync.py:225,235`.

### C. Desktop IDE Baseline Coverage Threshold Escalation (`a353ac754`)
- In `scripts/test-coverage.mjs`, escalated baseline line coverage quality gate threshold from **78.0%** to **80.0%**.
- Monorepo measured total line coverage is **82.8%** (41,659 / 50,315 lines), comfortably passing the new 80% requirement alongside all 15 crate and package floors.
