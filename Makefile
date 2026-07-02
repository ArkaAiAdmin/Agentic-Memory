.PHONY: test test-quick test-file test-clean test-results help

PYTHON := ./venv/bin/python

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

test-clean: ## Clear pytest cache + temp junit XML
	rm -rf eval/.pytest_cache eval/__pycache__ /tmp/junit_*.xml /tmp/full_suite*.log

precommit: ## Run pre-commit hooks (install first: pre-commit install)
	pre-commit run --all-files

.PHONY: precommit
