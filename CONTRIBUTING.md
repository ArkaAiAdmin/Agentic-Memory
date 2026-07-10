# Contributing

Thanks for your interest in contributing to Agentic Memory. This is a
local-first persistent memory layer for AI agents; contributions that
respect the local-first, no-LLM-in-the-write-path principles are most
welcome.

## Quick start

1. Fork and clone the repo.
2. Create a virtualenv at `./venv/` (any name works; the cron scripts
   default to `./venv/bin/python`).
3. `pip install -r requirements.txt` plus the optional extras
   (`pip install -e ".[all]"`).
4. Run the test suite: `./venv/bin/python -m pytest eval/ -q`.

## Architecture at a glance

- `save_pipeline.py` — the canonical write path (`save_memory()`).
- `search_pipeline.py` — the canonical read path (`search_memories()`).
- `mcp_tools.py` — all `@mcp.tool()` decorators; thin wrappers over the
  pipeline functions.
- `memory_common.py` — re-exports + `atomic_write` + `RateLimiter`.
- `db.py` — connection pool with WAL mode, busy_timeout, FK enforcement.
- `db_migrations.py` — schema migrations (currently v21; v18 added fact-level temporal KG, v19 fixed pre-existing kg_facts entity FKs, v20 added kg_facts FTS5 index, v21 added kg_crdt tables).

For the full schema, see `docs/reference/schema.md`.
For operational patterns, see `memory_workflow.md`.

## Coding conventions

- Type hints on new public functions.
- `try/except` only where the exception is meaningful and recoverable;
  bare `except Exception: pass` is forbidden in new code (audit finding H-tier).
- Every save must invalidate the search cache. Use `_search_cache.clear()`
  in `cache.py` if the canonical path isn't applicable.
- SQLite transactions: prefer `with conn:` blocks over manual commit/rollback.
- Magic numbers belong in `config.py` or at the top of the module with
  a comment explaining the threshold.

## Testing

- New behaviour needs a unit test. Aim for exact-value assertions
  (`assertEqual`, `assertAlmostEqual`) over `assertTrue` smoke tests.
- Use `tempfile.TemporaryDirectory()` for test DBs; do NOT hit the
  production `memory.db` (see `test_safety_wiring.py:60-109` for the
  `_ProdDBGuarded` mixin pattern that snapshots the prod DB).
- Mark tests as `xfail(strict=False, reason=...)` only when the fixture
  is genuinely incomplete; fix the fixture rather than xfail.

## Submitting changes

- One logical change per PR.
- Run the full test suite before pushing: `./venv/bin/python -m pytest eval/ -q`.
- Reference the audit item in your commit message if your change
  addresses a known issue (e.g., "C1 fix: half-indexed write in
  auto_save._upsert_memory").

## Reporting issues

When filing an issue, please include:
1. The exact command or tool call that triggered the bug.
2. The expected vs. actual behaviour.
3. A minimal reproduction (ideally a test case in `eval/test_*.py`).
4. Output of `./venv/bin/python -m pytest eval/ -q 2>&1 | tail -20`
   if the bug manifests as a test failure.

## License

By contributing you agree your contributions will be licensed under the
project's Apache 2.0 license.

## Documentation checklist (every PR)

When your PR changes any of the following, you MUST update docs:

- [ ] Schema version changed → update `docs/_meta.json`, run `venv/bin/python scripts/verify_doc_meta.py`
- [ ] Tool count (CORE/ADMIN) changed → update `docs/_meta.json`, run the verify script
- [ ] New config key → reflected in `docs/reference/configuration.md` (run `scripts/gen_config_doc.py`)
- [ ] New feature → add a how-to guide and/or concept doc
- [ ] Changed behavior → update existing docs that reference it
- [ ] Run `pre-commit run --all-files` (includes doc-freshness hooks)
