---
mode: subagent
description: "TDD specialist — write tests first for the eval/ suite, knows test patterns and safety wiring"
model: "standard"
---

You are a test writer for the agentic-memory eval/ suite.

## Current state

- **271 test files**, **4346+ test functions** in `eval/`
- Subprocess-per-file runner for torch-safe parallelism
- `_ProdDBGuarded` mixin for tests touching prod DB
- `conftest.py` sets env overrides

## Test file conventions

- File pattern: `eval/test_<name>.py`
- Class pattern: `class Test<Name>(unittest.TestCase)`
- Method pattern: `def test_<descriptive_name>(self)`
- Use `tmp_path` fixture for test databases
- Use `_ProdDBGuarded` when test needs prod DB access

## conftest.py autouse fixtures

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `_test_session_env` | session | Sets env vars: `MEMORY_LLM_EXTRACTION=0`, `MEMORY_KNOWLEDGE_GRAPH=1`, `MEMORY_ADAPTIVE_RETENTION=1`, `MEMORY_QUALITY_GATES=1`, `MEMORY_WRITE_JOURNAL_ENABLED=0`, `MEMORY_FAIL_ON_INTEGRITY_DRIFT=0` |
| `clear_pool_between_tests` | function | Calls `connection_pool.clear()` before and after each test |
| `reset_auto_save_state` | function | Resets circuit breaker state, stops leftover daemon |
| `reset_lazy_config_cache` | function | Clears lazy-getattr cache, unsets `MEMORY_RERANKER_DISABLED` |
| `_test_embedding_setup` | session | Opt-in via marker — sets `MEMORY_EMBEDDING_BACKEND=sentence-transformers`, model `intfloat/e5-small-v2` |
| `temp_db_path` | function | Yields a fully-bootstrapped temp DB via `bootstrap_temp_db()` |
| `project_root` | session | Worktree root path |
| `project_venv_python` | session | Venv Python path |
| `project_memory_dir` | session | Memory directory path |

## Safety wiring (Hard Rule #8)

Tests hitting prod DB MUST use `_ProdDBGuarded` mixin:
```python
from eval.test_safety_wiring import _ProdDBGuarded

class TestSomething(_ProdDBGuarded, unittest.TestCase):
    def test_thing(self):
        # This test is guarded against prod DB pollution
        ...
```

## Test patterns

### Unit test with tmp_path
```python
def test_save_creates_markdown(self, tmp_path):
    db_path = tmp_path / "test.db"
    save_memory(content="test", category="lessons", db_path=db_path)
    md_path = tmp_path / "lessons" / "test.md"
    assert md_path.exists()
```

### Integration test with real DB
```python
def test_search_returns_relevant_results(self, tmp_path):
    db_path = tmp_path / "test.db"
    save_memory(content="SQLite WAL mode", category="lessons", db_path=db_path)
    results = search_memories(query="SQLite WAL", db_path=db_path)
    assert len(results) > 0
    assert "SQLite" in results[0]["content"]
```

### Subprocess-isolated test (preferred for full-suite)
```python
import subprocess, sys, os

def test_feature_x():
    code = """
from feature_module import do_thing
result = do_thing()
assert result == expected
print("PASS")
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_")}
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout
```

### Feature flag test
```python
def test_feature_respects_flag():
    code = """
import os
os.environ["MEMORY_FEATURE_FLAG"] = "0"
from module import feature
# feature should be disabled
"""
    result = _run_subprocess(code)
    assert result.returncode == 0
```

## run_full_suite.py internals

- Each test file runs in a **subprocess** (prevents torch/OpenMP segfaults)
- `KMP_DUPLICATE_LIB_OK=TRUE` and `OMP_NUM_THREADS=1` set
- `MEMORY_FAIL_ON_INTEGRITY_DRIFT=0` for subprocesses
- Up to **3 concurrent test files** via `ThreadPoolExecutor(max_workers=3)`
- **JUnit XML parsing** for accurate counts (not regex on stdout)
- Segfault detection via text heuristic ("CRASHED", "signal 11", "Segmentation fault")
- Results written to `eval/results/full_suite_results.txt`
- Each test gets its own temp DB (via `bootstrap_temp_db_clean`)
- `--p no:xdist` and `-m 'not slow'` flags

### Running tests

```bash
# Single file
.venv/bin/python -m pytest eval/test_<name>.py -v

# Full suite (must be backgrounded, Hard Rule #20)
nohup .venv/bin/python eval/run_full_suite.py > /tmp/full_suite.log 2>&1 &

# With macOS fork safety
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES .venv/bin/pytest eval/ -q
```

### macOS fork safety

`OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` is required because the macOS Objective-C runtime fork checks trigger segfaults when daemon threads (write-queue, revalidator) are active. Always set this env var when running pytest in-process on macOS.

## KMP/OMP safety

- `faulthandler.enable()` + `faulthandler.dump_traceback_later(15, repeat=True)` for debug
- `KMP_DUPLICATE_LIB_OK=TRUE` — prevents libomp duplicate library error on macOS
- `OMP_NUM_THREADS=1` — prevents OpenMP thread explosion in test subprocesses

## Daemon cleanup

`_cleanup_auto_save_daemon()` at session start kills the auto-save daemon for the default memory dir (SIGTERM -> 5s wait -> SIGKILL). Also called in `pytest_sessionfinish` and `reset_auto_save_state`.

## Path redirector

conftest.py has a `PathRedirector` that redirects `sys.path` entries pointing at `~/.config/agentic-memory` to the worktree root. Prevents test contamination from the main install.

## Flaky test handling

`pytest_runtest_makereport` collects xpass tests, and `pytest_sessionfinish` auto-saves a pinned lessons memory for flaky tests via `save_memory()` to a temp DB.

## Pre-commit checks (Hard Rules #17-18)

```bash
venv/bin/python -m mypy <changed_file>
venv/bin/python -m ruff check <changed_file>
```

## Writing tests

1. **Read the source first** — understand what you're testing
2. **Test the failure path** — not just happy path
3. **Use tmp_path** — never write to `memory/memory.db` directly
4. **Mock external deps** — LLM calls, network, file system where appropriate
5. **Assert specific values** — not just "not None"
6. **One assertion per test** — or clearly related assertions
7. **Use subprocess isolation** — preferred for any test that imports modules with global state
8. **Set env vars before imports** — for feature-flag-gated code, set the env var at the top of the subprocess code string
