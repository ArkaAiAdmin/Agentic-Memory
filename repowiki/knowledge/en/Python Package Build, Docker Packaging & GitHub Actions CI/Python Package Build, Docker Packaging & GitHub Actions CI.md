---
kind: build_system
name: Python Package Build, Docker Packaging & GitHub Actions CI
category: build_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - setup.py
    - Dockerfile
    - docker-compose.yml
    - Makefile
    - .github/workflows/ci.yml
---

## What system/approach is used

Agentic Memory uses a flat-layout Python package built with setuptools (metadata in `pyproject.toml`, discovery via a minimal `setup.py`) and distributed as both an editable install (`pip install -e .`) and a PyPI wheel/sdist. Containerization is provided by a single multi-service `Dockerfile` plus a `docker-compose.yml` that runs three services (MCP server, sync server, cron scheduler) from the same image, selected at runtime via the `SERVICE` environment variable. Continuous integration is implemented entirely in GitHub Actions (`.github/workflows/`).

## Key files and packages

- **Package metadata & build config** — `pyproject.toml` (version `1.1.0`, `requires-python >=3.11`, optional dependency groups for embeddings/reranker/ltr/ner/dev/docs/dashboard/crewai/all), `setup.py` (flat-layout module discovery), `requirements.txt` (legacy lock).
- **Container images** — `Dockerfile` (python:3.12-slim, tini init, optional extras via `INSTALL_EXTRAS=1` build arg), `docker-compose.yml` (three services sharing a `/data` volume).
- **Local dev/build targets** — `Makefile` (lint/typecheck/test/doc-generation/precommit targets; scoped to a "config-drift surface" to keep gates green while repo-wide ruff has ~81 pre-existing errors).
- **CI pipelines** — `.github/workflows/ci.yml` (ruff lint, dashboard DB-access gate, mypy typecheck, drift checks, pytest with coverage gate ≥70%, sdist+wheel build, closed-auth security job), `.github/workflows/publish.yml`, `.github/workflows/docs.yml`, `.github/workflows/extended-tests.yml`, `.github/workflows/update-test-badge.yml`.
- **Runtime entrypoint** — `docker/entrypoint.sh` (dispatches `$SERVICE` to the right subcommand), `run-mcp-server.sh`.

## Architecture and conventions

### Flat layout + explicit inclusion
The project mixes top-level `.py` modules (119 of them) with conventional packages (`infra/`, `save/`, `search/`, `cron/`, `hooks/`, etc.). `setup.py`'s `_root_modules()` glob discovers every root `.py` (excluding `_*` shims) and `find_packages(exclude=["eval","memory","venv"])` ships the rest. This lets `pip install agentic-memory` expose both the `agentic_memory.*` SDK and the legacy CLI entrypoints under `agentic-memory-*` console scripts declared in `pyproject.toml`.

### Optional feature surfaces
Heavy dependencies are split into `[project.optional-dependencies]` groups (`embeddings`, `reranker`, `ltr`, `ner`, `dashboard`, `langchain`, `crewai`, `all`). The Dockerfile mirrors this with an `INSTALL_EXTRAS=1` build arg that pulls `model2vec`, `usearch`, and `numpy>=2` only when requested. CI installs `[dev,embeddings]` but disables reranking/CTR at runtime via env flags so tests stay fast.

### Multi-service container
A single image builds once and serves three roles selected by `ENV SERVICE`:
- `mcp` — MCP stdio server (no exposed port, stdin/stdout).
- `sync` — HTTP sync server on port 9877 with mTLS/HMAC auth.
- `cron` — long-lived Python scheduler loop.

All three share a named volume (`memory-data:/data`) so SQLite WAL + flock-based cross-process locking works across containers. Healthchecks probe for the presence of `memory.db`.

### CI quality gates
- **Lint**: `ruff check .` on ubuntu-latest with Python 3.12.
- **Dashboard DB-access gate**: dedicated job running `eval/lint_dashboard_db_access.py`.
- **Typecheck**: `mypy` over an explicitly enumerated file list in `pyproject.toml` (baseline 0; any new error fails CI).
- **Drift checks**: tool registry, docs, and schema-version consistency scripts.
- **Tests**: `pytest eval/` with `-n auto --dist=loadfile`, coverage reported per `infra/background/search/save/mcp_*` with `--cov-fail-under=70`. A separate `security-closed-auth` job re-runs the auth/RBAC subset under `MEMORY_AUTH_MODE=closed`.
- **Build artifact**: `python -m build` produces sdist+wheel uploaded as `dist/` artifact for 7 days.

### Local development workflow
- `make test` / `make test-quick` / `make test-file FILE=...` drive pytest through the local venv.
- `make lint` and `make typecheck` run ruff/mypy only over the "config-drift surface" to avoid failing on pre-existing repo-wide issues.
- `make update-docs` regenerates architecture/MCP-tool/schema/config/readme docs from source ASTs.
- `make precommit` invokes `pre-commit run --all-files`.

### Versioning & release
Version lives in one place: `pyproject.toml` `[project].version = "1.1.0"`. There is no `CHANGELOG.md` automation target in Makefile; releases appear to be manual (a `publish.yml` exists but its contents were not read here). The Docker image tag is not pinned in compose — it always builds `agentic-memory:latest` from context.

## Rules developers should follow

1. **Keep `pyproject.toml` as the single source of truth** for version, Python floor (`>=3.11`), dependencies, and console scripts. Any new optional feature must add an entry to `[project.optional-dependencies]` and document it in the Dockerfile's `INSTALL_EXTRAS` path if it affects the runtime image.
2. **New top-level modules must be added to the mypy file list** in `pyproject.toml[tool.mypy].files`; otherwise CI will silently skip them.
3. **Do not add heavy deps to core `dependencies`** — use optional groups so the base image stays lean. If a new optional group is introduced, mirror it in the Dockerfile's conditional install.
4. **When adding or changing MCP tools**, run `make update-mcp-tools` and `make update-schema` so generated docs stay in sync; CI's drift-check jobs will fail otherwise.
5. **For containerized deployments**, set `SERVICE=mcp|sync|cron` and mount a persistent `/data` volume. For production sync, supply `MEMORY_SYNC_TOKEN`, `MEMORY_SYNC_HMAC_SECRET`, and TLS cert/key via environment variables as shown in compose.
6. **Run `make precommit` before pushing**. The CI matrix covers Python 3.11/3.12/3.14; locally pinning to 3.12 (as the workflows do) avoids surprises.
7. **If you touch the dashboard**, remember the separate `lint-dashboard` gate enforces that dashboard tabs route DB access through the API, not direct SQLite.