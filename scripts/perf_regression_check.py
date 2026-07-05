"""Performance regression check for CI.

Runs a lightweight benchmark (1K corpus, --quick mode) and compares
p50/p95 latencies against a stored baseline.  Fails CI only on large
degradation (>threshold * baseline) to avoid flaky CI from runner variance.

Usage:
  python scripts/perf_regression_check.py [--baseline=PATH] [--threshold=FLOAT]

Exit 0: within threshold
Exit 1: regression detected
Exit 2: setup error (missing deps, baseline malformed, etc.)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PERF_ENVELOPE = REPO_ROOT / "eval" / "perf_envelope_v3.py"
RESULTS_PATH = REPO_ROOT / "eval" / "results" / "perf-envelope-v3.json"
DEFAULT_BASELINE = REPO_ROOT / "eval" / "results" / "perf-envelope-v3-baseline.json"


def _load_measurements(path: Path) -> dict[str, dict[str, float]]:
    with open(path, "r") as f:
        data = json.load(f)
    out: dict[str, dict[str, float]] = {}
    for corpus in data.get("corpora", []):
        for m in corpus.get("measurements", []):
            name = m.get("name", "")
            out[name] = {
                "p50_s": m.get("p50_s", 0.0) or 0.0,
                "p95_s": m.get("p95_s", 0.0) or 0.0,
            }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="perf regression gate for CI")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--threshold", type=float, default=3.0)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    if not PERF_ENVELOPE.exists():
        print(f"perf envelope not found: {PERF_ENVELOPE}")
        return 2

    # Run quick benchmark (1K corpus, ~30s)
    print("Running quick perf envelope (1K corpus)...")
    proc = subprocess.run(
        [sys.executable, str(PERF_ENVELOPE), "--quick"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        print(f"perf envelope failed: {proc.stderr[-1000:]}")
        return 2
    print(proc.stdout.strip())

    if not RESULTS_PATH.exists():
        print(f"results file missing after run: {RESULTS_PATH}")
        return 2

    current = _load_measurements(RESULTS_PATH)

    if args.update_baseline or not args.baseline.exists():
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RESULTS_PATH, args.baseline)
        print(f"Wrote baseline to {args.baseline}")
        return 0

    baseline = _load_measurements(args.baseline)
    regressions: list[str] = []
    for name, cur in current.items():
        base = baseline.get(name)
        if not base:
            continue
        for metric in ("p50_s", "p95_s"):
            base_val = base[metric]
            cur_val = cur[metric]
            if base_val <= 0:
                continue
            ratio = cur_val / base_val
            if ratio > args.threshold:
                regressions.append(
                    f"{name}.{metric}: {cur_val*1000:.2f}ms "
                    f"(baseline {base_val*1000:.2f}ms, {ratio:.1f}x)"
                )

    if regressions:
        print("PERF REGRESSION DETECTED:")
        for r in regressions:
            print(f"  {r}")
        return 1

    print("Perf check passed — within threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
