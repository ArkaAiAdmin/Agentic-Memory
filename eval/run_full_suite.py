#!/usr/bin/env python3
"""Run the full agentic-memory test suite one file at a time.

Each test file is run in a subprocess to prevent torch/OpenMP threading
issues that cause segfaults when multiple test files share a process.
Additionally sets KMP_DUPLICATE_LIB_OK=TRUE env var before each run.

Counts are read from the JUnit XML report (--junit-xml) so they remain
stable across pytest output format changes.
"""

import os
import sqlite3
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

VENV_PYTHON = Path(sys.executable)
if not VENV_PYTHON.exists():
    VENV_PYTHON = HERE.parent / ".venv" / "bin" / "python"
    if not VENV_PYTHON.exists():
        VENV_PYTHON = HERE.parent / "venv" / "bin" / "python"

try:
    from eval._fixtures import bootstrap_temp_db_clean
except Exception:
    bootstrap_temp_db_clean = None

# ── Template DB cache ───────────────────────────────────────────────────────
# Create one template DB with the full schema, then copy it for each test
# file instead of running all 74 migrations per test.  This cuts ~2s per
# test file down to ~0.05s (file copy vs migration runner).
_template_db: Path | None = None

def _get_template_db() -> Path:
    """Return a cached template DB path, creating it on first call."""
    global _template_db
    if _template_db is not None and _template_db.exists():
        return _template_db
    _template_db = Path(tempfile.mktemp(suffix=".db", prefix="template_db_"))
    if bootstrap_temp_db_clean is not None:
        bootstrap_temp_db_clean(_template_db)
    else:
        conn = sqlite3.connect(str(_template_db), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        from infra.db_migrations import run_schema_setup
        from fact import ensure_facts_schema
        run_schema_setup(conn)
        ensure_facts_schema(conn)
        conn.commit()
        conn.close()
    return _template_db

summary = {
    "passed": 0,
    "failed": 0,
    "skipped": 0,
    "xfailed": 0,
    "xpassed": 0,
    "errors": 0,
}
failures = []
results_file = HERE / "results" / "full_suite_results.txt"
results_file.parent.mkdir(parents=True, exist_ok=True)

env = os.environ.copy()
env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
env["OMP_NUM_THREADS"] = "1"
env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
# Downgrade config drift enforcement so env-var overrides don't block
# memory_mcp import in test subprocesses.
env["MEMORY_FAIL_ON_INTEGRITY_DRIFT"] = "0"
env["OMP_NUM_THREADS"] = "1"
env["OPENBLAS_NUM_THREADS"] = "1"
env["MKL_NUM_THREADS"] = "1"
env["VECLIB_MAXIMUM_THREADS"] = "1"
env["NUMEXPR_NUM_THREADS"] = "1"
env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

test_files = sorted(HERE.glob("test_*.py"))
test_files = [f for f in test_files if not f.name.startswith("test_all_")]

passed_names = []
failed_names = []
file_timings: list[tuple[float, str, str, int, int]] = []


def _parse_junit(junit_path: Path) -> dict:
    """Parse a pytest junit XML report into a counts dict."""
    counts = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "errors": 0,
    }
    if not junit_path.exists():
        return counts
    try:
        tree = ET.parse(junit_path)
    except ET.ParseError:
        return counts
    suite = tree.getroot()
    if suite.tag != "testsuite":
        suite = suite.find("testsuite")
    if suite is None:
        return counts
    counts["passed"] += (
        int(suite.get("tests", 0))
        - int(suite.get("failures", 0))
        - int(suite.get("errors", 0))
        - int(suite.get("skipped", 0))
    )
    counts["failed"] += int(suite.get("failures", 0))
    counts["errors"] += int(suite.get("errors", 0))
    for tc in suite.iter("testcase"):
        for child in tc:
            tag = child.tag
            if tag == "skipped":
                msg = child.get("message", "") or ""
                if "xfail" in msg.lower():
                    counts["xfailed"] += 1
                    counts["skipped"] -= 1
                else:
                    counts["skipped"] += 1
            elif tag == "failure":
                msg = ET.tostring(child, encoding="unicode")
                if "xpass" in msg.lower() or "XPASS" in msg:
                    counts["xpassed"] += 1
                    counts["failed"] -= 1
                    counts["passed"] += 1
    return counts


import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

lock = threading.Lock()


def run_one_test(f):
    start = time.time()
    import shutil

    worker_dir = Path(tempfile.mkdtemp(prefix="runner_worker_"))
    junit_path = worker_dir / "junit.xml"
    temp_db_path = worker_dir / "memory.db"

    # Copy template DB instead of running all migrations per test
    template = _get_template_db()
    shutil.copy2(str(template), str(temp_db_path))

    # Copy base env and set hermetic per-worker environment
    test_env = env.copy()
    test_env["TMPDIR"] = str(worker_dir)
    test_env["AGENTIC_MEMORY_DIR"] = str(worker_dir)
    test_env["MEMORY_CONFIG_DIR"] = str(worker_dir)
    test_env["MEMORY_DB_PATH"] = str(temp_db_path)
    test_env["PYTHONPATH"] = str(HERE.parent)

    include_slow = (
        os.environ.get("INCLUDE_SLOW") == "1"
        or os.environ.get("MEMORY_TEST_INCLUDE_SLOW") == "1"
        or "--include-slow" in sys.argv
    )
    marker_args = [] if include_slow else ["-m", "not slow"]

    _all_slow = False
    output = ""
    pp, ff, ss, xxf, xxp, ee = 0, 0, 0, 0, 0, 0
    result = None

    try:
        result = subprocess.run(
            [
                str(VENV_PYTHON),
                "-m",
                "pytest",
                str(f),
                "-p",
                "no:xdist",
                *marker_args,
                "--tb=line",
                "-q",
                f"--junit-xml={junit_path}",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=test_env,
        )
        output = result.stdout + result.stderr
        _all_slow = (
            result.returncode == 5
            and ("deselected" in output or "0 tests" in output or "collected 0 items" in output)
        )

        counts = _parse_junit(junit_path)
        pp = counts["passed"]
        ff = counts["failed"]
        ss = counts["skipped"]
        xxf = counts["xfailed"]
        xxp = counts["xpassed"]
        ee = counts["errors"]
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout.decode() if exc.stdout else "") + (exc.stderr.decode() if exc.stderr else "") + "\n[TIMEOUT after 180s]"
        pp, ff, ss, xxf, xxp, ee = 0, 1, 0, 0, 0, 0
        result = type("", (), {})()
        result.returncode = -1  # type: ignore[attr-defined]
    except Exception as exc:
        output = str(exc)
        pp, ff, ss, xxf, xxp, ee = 0, 1, 0, 0, 0, 0
        result = type("", (), {})()
        result.returncode = -1  # type: ignore[attr-defined]
    finally:
        shutil.rmtree(str(worker_dir), ignore_errors=True)

    dur = time.time() - start

    # Check for segfault (text heuristic — XML alone doesn't show this)
    is_segfault = (
        "CRASHED" in output
        or "signal 11" in output
        or "Segmentation fault" in output
        or "Fatal Python error" in output
    )

    rc = getattr(result, "returncode", -1)
    if is_segfault:
        status = "SEGFAULT"
        ff = (ff or 0) + 1
    elif _all_slow:
        status = f"SKIP (all slow) ({dur:.1f}s)"
    elif rc == 5 and pp == 0 and ff == 0:
        status = f"EMPTY ({dur:.1f}s)"
    elif ff == 0 and rc == 0:
        status = f"OK ({dur:.1f}s)"
    else:
        status = f"FAIL ({ff}f {ee}e) {dur:.1f}s"

    with lock:
        file_timings.append((dur, f.name, status, pp, ff))
        if status.startswith("OK") or status.startswith("EMPTY") or status.startswith("SKIP"):
            passed_names.append(f.name)
        else:
            failed_names.append(f.name)

        summary["passed"] += pp
        summary["failed"] += ff
        summary["skipped"] += ss
        summary["xfailed"] += xxf
        summary["xpassed"] += xxp
        summary["errors"] += ee

        if ff > 0 or is_segfault:
            failures.append((f.name, output[-2000:]))

        print(f"  {f.name} ... {status}", flush=True)

# Run up to 6 tests concurrently (subprocess isolation prevents threading issues).
# Use as_completed with per-future timeout so a single stuck worker doesn't hang the whole suite.
import threading as _threading

_SUITE_DEADLINE_S = 3600

def _suite_watchdog():
    _threading.Event().wait(timeout=_SUITE_DEADLINE_S)
    print(f"\n⚠ SUITE WATCHDOG: {_SUITE_DEADLINE_S}s elapsed — terminating", flush=True)
    import faulthandler
    faulthandler.dump_traceback()
    os._exit(2)

_watchdog_thread = _threading.Thread(target=_suite_watchdog, daemon=True, name="suite-watchdog")
_watchdog_thread.start()

max_workers = int(os.environ.get("SUITE_WORKERS") or min(os.cpu_count() or 8, 12))
with ThreadPoolExecutor(max_workers=max_workers) as executor:
    future_to_file = {executor.submit(run_one_test, f): f for f in test_files}
    for future in as_completed(future_to_file, timeout=_SUITE_DEADLINE_S):
        f = future_to_file[future]
        try:
            future.result(timeout=1020)
        except Exception as exc:
            print(f"  {f.name} ... WORKER ERROR: {exc}", flush=True)

_watchdog_thread.join(timeout=0)

sorted_timings = sorted(file_timings, key=lambda x: x[0], reverse=True)
total_dur = sum(t[0] for t in file_timings)

# Write results file
with open(results_file, "w") as r:
    r.write(
        f"Total: {summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped, {summary['xfailed']} xfailed (Suite time: {total_dur:.1f}s aggregate)\n"
    )
    r.write(f"Passed files: {len(passed_names)}\n")
    r.write(f"Failed/segfault files: {len(failed_names)}\n\n")
    for name in failed_names:
        r.write(f"  FAILED: {name}\n")
    r.write("\n")
    for name in passed_names:
        r.write(f"  PASSED: {name}\n")
    r.write("\n\nAll File Timings (Slowest First):\n")
    for dur, name, st, p, f_cnt in sorted_timings:
        r.write(f"  {dur:6.2f}s | {name:<50} | {p}p {f_cnt}f | {st}\n")
    r.write("\n\nFailure details:\n")
    for name, out in failures:
        r.write(f"\n=== {name} ===\n{out}\n")

print(f"\n{'=' * 75}")
print(f"TOP 25 SLOWEST TEST FILES (Aggregate time: {total_dur:.1f}s):")
print(f"{'-' * 75}")
for dur, name, st, p, f_cnt in sorted_timings[:25]:
    print(f"  {dur:6.2f}s | {name:<45} | {p}p {f_cnt}f | {st}")
print(f"{'=' * 75}")
print(
    f"SUMMARY: {summary['passed']}p {summary['failed']}f {summary['skipped']}s {summary['xfailed']}xf {summary['xpassed']}xp {summary['errors']}e"
)
print(f"Failures: {[n for n, _ in failures]}")
print(f"Results saved to: {results_file}")

# Cleanup template DB
if _template_db and _template_db.exists():
    _template_db.unlink(missing_ok=True)
    Path(str(_template_db) + "-wal").unlink(missing_ok=True)
    Path(str(_template_db) + "-shm").unlink(missing_ok=True)
