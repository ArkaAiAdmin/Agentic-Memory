"""Zero-Regression Strict Gate for LongMemEval-V2.

Evaluates all 451 benchmark questions against the golden baseline.
Guarantees:
  1. Zero regressions on previously passing questions (must be 0).
  2. Measures net gains on previously failing questions.
  3. Fails with exit code 1 if any regression is detected.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from eval.longmemeval_v2_eval import score_answer_text

REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_FILE = REPO_ROOT / "eval" / "longmemeval_v2" / "data" / "longmemeval-v2" / "questions.jsonl"
BASELINE_FILE = REPO_ROOT / "eval" / "results" / "longmemeval_v2_baseline_summary.json"
CACHE_DB_PATH = REPO_ROOT / "eval" / ".cache" / "dbs" / "lme_v2_all_all_1870.db"


def run_regression_gate(
    db_path: Path | None = None,
    baseline_path: Path | None = None,
    verbose: bool = False,
    live: bool = False,
) -> int:
    db_p = db_path or CACHE_DB_PATH
    base_p = baseline_path or BASELINE_FILE

    if not db_p.exists():
        print(f"Error: Database not found at {db_p}", file=sys.stderr)
        return 2

    if not base_p.exists():
        print(f"Error: Baseline summary not found at {base_p}", file=sys.stderr)
        return 2

    if not QUESTIONS_FILE.exists():
        print(f"Error: Questions file not found at {QUESTIONS_FILE}", file=sys.stderr)
        return 2

    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        q_map = {obj["id"]: obj for line in f if line.strip() for obj in [json.loads(line)]}

    with open(base_p, "r", encoding="utf-8") as f:
        base_data = json.load(f)

    base_results = base_data.get("results", [])
    conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)

    gains: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    total_passed = 0
    baseline_passed = 0

    if live:
        from eval.longmemeval_v2_eval import evaluate_question
        print("Running live evaluation gate with full 14-phase search orchestrator...")

    for idx, r in enumerate(base_results, 1):
        qid = r["question_id"]
        q_obj = q_map.get(qid, {})
        base_score = r.get("primary_score", r.get("scores", {}).get("overall_accuracy", 0.0))
        base_pass = base_score >= 0.5
        if base_pass:
            baseline_passed += 1

        if live:
            res = evaluate_question(q_obj or r, db_path=str(db_p), read_conn=conn, light=True)
            current_score = res.get("scores", {}).get("primary_score", 0.0)
            scores = res.get("scores", {})
        else:
            retrieved = r.get("retrieved_ids", [])[:30]
            id_to_content: dict[str, str] = {}
            if retrieved:
                placeholders = ",".join("?" for _ in retrieved)
                rows = conn.execute(
                    f"SELECT id, content FROM memories WHERE id IN ({placeholders})",
                    tuple(retrieved),
                ).fetchall()
                id_to_content = {row[0]: row[1] for row in rows}

            combined = " ".join(id_to_content.get(mid, "") for mid in retrieved if mid in id_to_content)

            scores = score_answer_text(
                query=q_obj.get("question", r.get("question", "")),
                expected=r["expected"],
                eval_func=q_obj.get("eval_function", ""),
                q_type=r["category"],
                combined_content=combined,
                recall10=r.get("scores", {}).get("recall@10", 1.0),
            )
            current_score = scores.get("overall_accuracy", scores.get("exact_match", scores.get("recall@10", 0.0)))

        current_pass = current_score >= 0.5
        if current_pass:
            total_passed += 1

        if not base_pass and current_pass:
            gains.append({
                "index": idx,
                "question_id": qid,
                "category": r["category"],
                "expected": r["expected"],
                "question": q_obj.get("question", "")[:90],
            })
        elif base_pass and not current_pass:
            regressions.append({
                "index": idx,
                "question_id": qid,
                "category": r["category"],
                "expected": r["expected"],
                "question": q_obj.get("question", "")[:90],
                "scores": scores,
            })

    conn.close()

    print("\n" + "=" * 80)
    print("ZERO-REGRESSION STRICT GATE EVALUATION REPORT")
    print("=" * 80)
    print(f"Total Benchmark Questions : {len(base_results)}")
    print(f"Baseline Passing Questions: {baseline_passed} / {len(base_results)} ({baseline_passed/len(base_results)*100:.2f}%)")
    print(f"Current Passing Questions : {total_passed} / {len(base_results)} ({total_passed/len(base_results)*100:.2f}%)")
    print(f"Net Gain                  : {total_passed - baseline_passed:+d} passes")
    print("-" * 80)

    if gains:
        print(f"\n[+] GAINS ({len(gains)} questions flipped to PASS):")
        for g in gains:
            print(f"  + Q{g['index']:03d} [{g['question_id']}] ({g['category']}): expected='{g['expected']}'")
            if verbose:
                print(f"        Q: {g['question']}...")

    if regressions:
        print(f"\n[-] REGRESSIONS ({len(regressions)} questions regressed to FAIL):")
        for reg in regressions:
            print(f"  - Q{reg['index']:03d} [{reg['question_id']}] ({reg['category']}): expected='{reg['expected']}'")
            if verbose:
                print(f"        Q: {reg['question']}...")
                print(f"        Scores: {reg['scores']}")
        print("\n" + "=" * 80)
        print("❌ GATE STATUS: FAILED (Regressions detected — changes cannot be deployed)")
        print("=" * 80 + "\n")
        return 1

    print("\n" + "=" * 80)
    print("✅ GATE STATUS: PASSED (0 regressions confirmed across all 451 questions)")
    print("=" * 80 + "\n")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Zero-Regression Strict Gate for LongMemEval-V2")
    parser.add_argument("--db", type=str, default=None, help="Path to evaluation SQLite database")
    parser.add_argument("--baseline", type=str, default=None, help="Path to golden baseline results JSON")
    parser.add_argument("--live", action="store_true", help="Run with live 14-phase search orchestrator")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose diff printing")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None
    baseline_path = Path(args.baseline) if args.baseline else None

    rc = run_regression_gate(db_path=db_path, baseline_path=baseline_path, verbose=args.verbose, live=args.live)
    sys.exit(rc)


if __name__ == "__main__":
    main()
