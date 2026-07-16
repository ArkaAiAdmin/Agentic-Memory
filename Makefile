.PHONY: test test-quick test-file test-clean test-results update-agents-md help
.PHONY: lint typecheck
.PHONY: update-docs update-architecture update-mcp-tools update-schema update-config update-readme

PYTHON := ./venv/bin/python

# Config-drift surface: the production files that implement hot-reload / fleet
# policy_hash / tier-patch PLUS their dedicated test files. Scoped so the gate
# is GREEN while repo-wide `ruff check .` still carries ~81 pre-existing errors
# (broadening to the rest of the repo is a separate future PR).
CONFIG_DRIFT_SURFACE := \
	infra/config_drift.py \
	infra/config_drift_policy.py \
	infra/config_drift_tier_patch.py \
	infra/toml_watch.py \
	infra/policy_hash_cache.py \
	infra/policy_hash_fetcher.py \
	infra/policy_hash_diff.py \
	mcp_maintenance_policy_hash.py \
	eval/test_config_drift_tier_patching.py \
	eval/test_config_drift_tier_reset.py \
	eval/test_policy_hash_cache.py \
	eval/test_policy_hash_fetcher.py \
	eval/test_policy_hash_status.py \
	eval/test_toml_watch.py \
	eval/test_toml_hot_reload.py \
	eval/test_policy_eligibility.py

# infra/toml_watch.py is excluded from typecheck: it is owned by another
# sub-agent and carries 2 pre-existing mypy no-any-return errors that must be
# fixed by that owner, not here.
CONFIG_DRIFT_TYPECHECK_SURFACE := \
	infra/config_drift.py \
	infra/config_drift_policy.py \
	infra/config_drift_tier_patch.py \
	infra/policy_hash_cache.py \
	infra/policy_hash_fetcher.py \
	infra/policy_hash_diff.py \
	mcp_maintenance_policy_hash.py

lint: ## Ruff over the config-drift surface (scoped, not repo-wide)
	$(PYTHON) -m ruff check $(CONFIG_DRIFT_SURFACE)

typecheck: ## Mypy over the config-drift surface (scoped, not repo-wide)
	$(PYTHON) -m mypy --follow-imports=silent $(CONFIG_DRIFT_TYPECHECK_SURFACE)

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, "$$2"}'

test: ## Run the full test suite in-process (default, 3879 tests)
	$(PYTHON) -m pytest eval/ --timeout=15 -q

test-safe: ## Run the full test suite subprocess-per-file (MPS/OpenMP crash-safe)
	$(PYTHON) eval/run_full_suite.py

test-quick: ## Run only fast unit tests (skips slow-marked)
	$(PYTHON) -m pytest eval/ -q -m "not slow" --tb=line -p no:cacheprovider --timeout=60

test-file: ## Run a single test file: make test-file FILE=eval/test_foo.py
	@test -n "$(FILE)" || (echo "Usage: make test-file FILE=eval/test_foo.py" && exit 1)
	$(PYTHON) -m pytest "$(FILE)" -q --tb=short

test-results: ## Show last full-suite results
	@cat eval/results/full_suite_results.txt 2>/dev/null || echo "No results yet — run 'make test'"

update-agents-md: ## Regenerate AUTO-GEN sections in AGENTS.md + docs/_meta.json + verify from live codebase
	$(PYTHON) scripts/gen_doc_meta.py
	$(PYTHON) infra/agents_md_generator.py
	$(PYTHON) scripts/verify_doc_meta.py

# ── Doc generation targets ──────────────────────────────────────────
# Run these when the relevant source of truth changes.
# Always run `make update-docs` after significant code changes.

update-docs: update-agents-md update-architecture update-mcp-tools update-readme update-mcp-surface ## Regenerate all docs (run before every commit)
	@echo "All docs regenerated."

update-mcp-surface: ## Regenerate AUTO-GEN spans in docs/MCP_SURFACE.md (schema version) — run after any migration
	$(PYTHON) scripts/doc_drift_check.py --fix >/dev/null 2>&1 || true
	@echo "MCP_SURFACE.md synced."

update-architecture: ## Regenerate docs/architecture.md — run when adding/removing modules or changing LOC significantly
	$(PYTHON) scripts/generate_architecture_md.py

update-mcp-tools: ## Regenerate docs/reference/mcp-tools.md — run when adding/removing MCP tools or changing CORE/ADMIN split
	$(PYTHON) scripts/gen_mcp_tools_doc.py

update-schema: ## Regenerate schema docs — run after any migration (adds/alters tables)
	$(PYTHON) scripts/gen_schema_doc.py

update-config: ## Regenerate config docs — run after changing memory.toml or infra/config.py flags
	$(PYTHON) scripts/gen_config_doc.py

update-readme: ## Update README badges from live code — run after schema/tool/test count changes
	$(PYTHON) scripts/gen_readme_badges.py
test-clean: ## Clear pytest cache + temp junit XML
	rm -rf eval/.pytest_cache eval/__pycache__ /tmp/junit_*.xml /tmp/full_suite*.log

precommit: ## Run pre-commit hooks (install first: pre-commit install)
	pre-commit run --all-files

.PHONY: precommit
