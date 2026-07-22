---
kind: dependency_management
name: Python-first dependency management with optional extras and lockfiles for embedded tooling
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - setup.py
    - requirements.txt
    - .mimocode/package.json
    - .opencode/package.json
    - ts-sdk/package.json
---

This repository manages dependencies primarily through Python's modern packaging stack, with a small number of embedded Node.js toolchains.

Primary system — pyproject.toml + setup.py (setuptools)
- The authoritative manifest is pyproject.toml, which declares the package name (agentic-memory), Python floor (>=3.11), core runtime deps (mcp, numpy), and all optional feature groups under [project.optional-dependencies]: embeddings, reranker, ltr, ner, dev, docs, langchain, dashboard, crewai, and an all meta-group that composes them.
- setup.py is intentionally minimal: it only augments setuptools discovery so that the flat layout (119 root-level .py modules plus top-level packages like infra/, save/, search/, cron/, hooks/) ships alongside the agentic_memory/ subpackage. It excludes eval/, memory/, and venv/ from distribution.
- Console entry points are declared in [project.scripts] (e.g. agentic-memory, agentic-memory-server, agentic-memory-dashboard, etc.), replacing the legacy CLI shim layer.

Secondary manifest — requirements.txt
- A flat requirements.txt mirrors the same version ranges as pyproject.toml and exists for environments/tools that expect a single requirements file. It duplicates both required and optional groups but does not use extras syntax.

Lockfiles for embedded Node toolchains
- Three small Node.js trees ship inside the repo and each pins its own dependencies:
  - .mimocode/package.json depends on @mimo-ai/plugin@0.1.5 with a package-lock.json.
  - .opencode/package.json depends on @opencode-ai/plugin@1.17.5 with a package-lock.json.
  - ts-sdk/package.json defines the public TypeScript SDK (@agentic-memory/sdk) with runtime dep ws and dev deps (typescript, jest, ts-jest, @types/*).
- These lockfiles are committed alongside their manifests; there is no monorepo-level lockfile or workspace configuration.

What is NOT used
- No vendoring (no vendor/ directory, no pip install --no-index --find-links, no Poetry/pipenv lockfiles, no uv.lock).
- No private PyPI registry or pip.conf / PIP_INDEX_URL configuration is present at the repo root.
- No Go module files were found at the repository root; any Go code lives outside this snapshot.