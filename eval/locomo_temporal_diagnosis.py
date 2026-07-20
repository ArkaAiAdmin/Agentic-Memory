#!/usr/bin/env python3
"""Diagnose LoCoMo temporal-reasoning failures.

Runs only category=3 (temporal) questions and analyzes:
1. Which questions fail and why
2. Whether the temporal-KG path is activated
3. Whether the failure is retrieval (gold not in top-k) or ranking (gold below cutoff)
4. Whether the gold sessions have distinctive temporal markers
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EVAL_ROOT.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(EVAL_ROOT))

from locomo_eval import (
    ensure_dataset, ingest_conversation, extract_gold_sessions,
    session_to_memory_id, CATEGORY_MAP, K_VALUES,
)
from _fixtures import bootstrap_temp_db_clean
from search.orchestrator import search_memories


def run_diagnosis():
    data = ensure_dataset()

    # Set up fresh DB
    tmpdir = Path(tempfile.mkdtemp(prefix="locomo_temporal_diag_"))
    db_path = tmpdir / "memory.db"
    bootstrap_temp_db_clean(db_path)

    # Ingest all conversations
    all_session_maps = {}
    for sample in data:
        sid = sample["sample_id"]
        all_session_maps[sid] = ingest_conversation(db_path, sample)
    total_sessions = sum(len(m) for m in all_session_maps.values())
    print(f"Ingested {total_sessions} sessions from {len(data)} conversations")

    # Collect temporal questions only
    temporal_questions = []
    for sample in data:
        sid = sample["sample_id"]
        for qa in sample["qa"]:
            cat_num = qa.get("category", 0)
            if cat_num == 3:  # temporal
                gold = extract_gold_sessions(qa)
                temporal_questions.append({
                    "sample_id": sid,
                    "question": qa["question"],
                    "answer": qa.get("answer", ""),
                    "category": "temporal",
                    "gold_sessions": gold,
                    "evidence": qa.get("evidence", []),
                })

    print(f"\nTemporal questions: {len(temporal_questions)}")

    # Analyze each question
    failures = []
    successes = []
    k = 10

    for i, q in enumerate(temporal_questions):
        if (i + 1) % 20 == 0:
            print(f"  [{i+1}/{len(temporal_questions)}]")

        t0 = time.time()
        result = search_memories(
            db_path,
            q["question"],
            limit=20,  # Match full eval's default
            include_global=True,
            rerank=True,
            hybrid=True,  # Match full eval
            include_facts=False,
            safety_wiring=False,
            tenant_id="locomo",
            category="sessions",
        )
        latency_ms = (time.time() - t0) * 1000

        ranked = [r["id"] for r in result.get("results", [])]

        # Map to session numbers
        retrieved_sessions = set()
        for mid in ranked:
            parts = mid.split("/")
            if len(parts) == 3 and parts[0] == "locomo":
                sess_num = parts[2].split("_")[1]
                retrieved_sessions.add(sess_num)

        hit = bool(retrieved_sessions & q["gold_sessions"])
        gold_in_retrieved = q["gold_sessions"] & retrieved_sessions
        gold_missing = q["gold_sessions"] - retrieved_sessions

        # Check gold rank position
        gold_ranks = {}
        for mid_idx, mid in enumerate(ranked):
            parts = mid.split("/")
            if len(parts) == 3 and parts[0] == "locomo":
                sess_num = parts[2].split("_")[1]
                if sess_num in q["gold_sessions"]:
                    gold_ranks[sess_num] = mid_idx + 1

        entry = {
            "question": q["question"][:120],
            "answer": q["answer"],
            "gold_sessions": sorted(q["gold_sessions"]),
            "retrieved_top10": sorted(retrieved_sessions)[:10],
            "hit": hit,
            "gold_in_top10": sorted(gold_in_retrieved),
            "gold_missing": sorted(gold_missing),
            "gold_ranks": gold_ranks,
            "latency_ms": round(latency_ms, 1),
            "n_results": len(ranked),
        }

        if hit:
            successes.append(entry)
        else:
            failures.append(entry)

    # Summary
    print(f"\n{'='*70}")
    print(f"TEMPORAL-REASONING DIAGNOSIS")
    print(f"{'='*70}")
    print(f"Total temporal questions: {len(temporal_questions)}")
    print(f"Recall@{k}: {len(successes)}/{len(temporal_questions)} = {len(successes)/len(temporal_questions):.4f}")
    print(f"Failures: {len(failures)}")

    # Failure analysis
    if failures:
        print(f"\n--- FAILURE ANALYSIS ---")

        # Categorize failure modes
        no_results = [f for f in failures if f["n_results"] == 0]
        gold_not_ranked = [f for f in failures if f["n_results"] > 0 and not f["gold_missing"]]
        gold_below_cutoff = [f for f in failures if f["gold_ranks"] and max(f["gold_ranks"].values()) > k]
        gold_missing_entirely = [f for f in failures if f["gold_missing"] and not f["gold_ranks"]]

        print(f"\n  No results returned: {len(no_results)}")
        print(f"  Gold not in top-{k} (ranked but below cutoff): {len(gold_below_cutoff)}")
        print(f"  Gold missing from retrieval entirely: {len(gold_missing_entirely)}")

        # Show worst failures (gold rank furthest from top)
        print(f"\n--- WORST FAILURES (gold rank > {k}) ---")
        ranked_failures = [f for f in failures if f["gold_ranks"]]
        ranked_failures.sort(key=lambda f: max(f["gold_ranks"].values()), reverse=True)
        for f in ranked_failures[:10]:
            worst_rank = max(f["gold_ranks"].values())
            print(f"\n  Q: {f['question']}")
            print(f"  Answer: {f['answer']}")
            print(f"  Gold sessions: {f['gold_sessions']}")
            print(f"  Gold ranks: {f['gold_ranks']} (worst={worst_rank})")
            print(f"  Retrieved top-10: {f['retrieved_top10']}")

        # Show gold-missing failures
        if gold_missing_entirely:
            print(f"\n--- GOLD MISSING FROM RETRIEVAL ---")
            for f in gold_missing_entirely[:5]:
                print(f"\n  Q: {f['question']}")
                print(f"  Answer: {f['answer']}")
                print(f"  Gold sessions: {f['gold_sessions']}")
                print(f"  Retrieved top-10: {f['retrieved_top10']}")

    # Latency stats
    all_latencies = [f["latency_ms"] for f in failures + successes]
    if all_latencies:
        all_latencies.sort()
        print(f"\n--- LATENCY ---")
        print(f"  Mean: {sum(all_latencies)/len(all_latencies):.1f}ms")
        print(f"  p50: {all_latencies[len(all_latencies)//2]:.1f}ms")
        print(f"  p95: {all_latencies[int(len(all_latencies)*0.95)]:.1f}ms")

    # Save results
    output = {
        "n_temporal": len(temporal_questions),
        "recall_at_k": {f"k={k}": len(successes) / len(temporal_questions)},
        "failures": failures,
        "successes": successes,
        "failure_analysis": {
            "no_results": len(no_results) if failures else 0,
            "gold_below_cutoff": len(gold_below_cutoff) if failures else 0,
            "gold_missing_entirely": len(gold_missing_entirely) if failures else 0,
        },
    }
    out_path = EVAL_ROOT / "results" / "locomo_temporal_diagnosis.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {out_path}")

    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    run_diagnosis()
