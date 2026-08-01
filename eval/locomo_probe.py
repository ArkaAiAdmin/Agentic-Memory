#!/usr/bin/env python3
"""Diagnostic probe for LoCoMo retrieval quality.

Measures, per question, where recall is lost:
  * pool recall@30 (lenient eval metric AND strict exact-memory-id metric)
  * which search phases actually fired (phase_latencies keys)
  * which CE mode was selected (weak/combined/deep)
  * phase errors

Usage:
    python eval/locomo_probe.py --max-questions 50
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EVAL_ROOT.parent
RESULTS_DIR = EVAL_ROOT / "results"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(EVAL_ROOT))

import locomo_eval as le  # noqa: E402  (sets benchmark env on import)
import memory_mcp  # noqa: E402
import search.orchestrator as _orch  # noqa: E402
import search.rerankers as _rerank  # noqa: E402

K_VALUES = [1, 5, 10, 20, 30]

_ce_modes_seen: list[str] = []
_orig_select = _orch._select_ce_mode


def _probe_select_ce_mode(query: str, deep_rerank_param: bool = False) -> str:
    mode = _orig_select(query, deep_rerank_param)
    _ce_modes_seen.append(mode)
    return mode


_orig_select_rr = _rerank._select_ce_mode
_rerank._select_ce_mode = _probe_select_ce_mode
_orch._select_ce_mode = _probe_select_ce_mode


def _session_nums(ids: list[str]) -> set[str]:
    out = set()
    for mid in ids:
        parts = mid.split("/")
        if len(parts) == 3 and parts[0] == "locomo":
            out.add(parts[2].split("_")[1])
    return out


def main(max_questions: int | None) -> None:
    data = le.ensure_dataset()
    tmpdir = Path(tempfile.mkdtemp(prefix="locomo_probe_"))
    db_path = tmpdir / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(db_path)
    from _fixtures import bootstrap_temp_db_clean

    bootstrap_temp_db_clean(db_path)

    wall_start = time.time()
    all_session_maps: dict[str, dict[str, str]] = {}
    for sample in data:
        all_session_maps[sample["sample_id"]] = le.ingest_conversation(db_path, sample)
    ingest_time = time.time() - wall_start
    total_sessions = sum(len(m) for m in all_session_maps.values())
    print(f"Ingested {total_sessions} sessions from {len(data)} conversations "
          f"in {ingest_time:.1f}s")

    questions = []
    for sample in data:
        sid = sample["sample_id"]
        for qa in sample["qa"]:
            gold_nums = le.extract_gold_sessions(qa)
            strict = {le.session_to_memory_id(sid, f"session_{n}") for n in gold_nums}
            questions.append({
                "sample_id": sid,
                "question": qa["question"],
                "category": le.CATEGORY_MAP.get(qa.get("category", 0), "unknown"),
                "gold_nums": gold_nums,
                "gold_mem_ids": strict,
            })
    if max_questions:
        questions = questions[:max_questions]

    print(f"Probing {len(questions)} questions ...")

    rows: list[dict] = []
    for i, q in enumerate(questions):
        if hasattr(memory_mcp, "_search_cache"):
            memory_mcp._search_cache.clear()
        q_start = time.time()
        result = _orch.search_memories(
            db_path,
            q["question"],
            limit=30,
            include_global=True,
            rerank=True,
            include_facts=False,
            safety_wiring=False,
            tenant_id="locomo",
            category="sessions",
        )
        latency_ms = (time.time() - q_start) * 1000
        ids = [r["id"] for r in result.get("results", [])]
        num_ids = [n for mid in ids for n in [mid.split("/")[2].split("_")[1]]
                   if mid.split("/")[0] == "locomo"]

        row = {
            "i": i,
            "category": q["category"],
            "question": q["question"][:120],
            "n_results": len(ids),
            "latency_ms": round(latency_ms, 1),
            "ce_mode": _ce_modes_seen[-1] if _ce_modes_seen else "?",
            "phases": sorted(result.get("phase_latencies", {}).keys()),
            "phase_errors": sorted(result.get("phase_errors", {}).keys()),
            "lenient": {k: 0 for k in K_VALUES},
            "strict": {k: 0 for k in K_VALUES},
        }
        for k in K_VALUES:
            top = ids[:k]
            row["lenient"][k] = 1 if (_session_nums(top) & q["gold_nums"]) else 0
            row["strict"][k] = 1 if (set(top) & q["gold_mem_ids"]) else 0
        rows.append(row)

    # Aggregates
    n = len(rows)
    cats = sorted({r["category"] for r in rows})
    agg: dict = {
        "n_questions": n,
        "recall": {"lenient": {}, "strict": {}},
        "pool30": {"lenient": 0, "strict": 0},
        "phase_fire_rate": {},
        "phase_error_rate": {},
        "ce_modes": {},
        "avg_n_results": round(sum(r["n_results"] for r in rows) / n, 2),
        "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / n, 1),
        "per_category": {},
    }
    for k in K_VALUES:
        agg["recall"]["lenient"][k] = round(
            sum(r["lenient"][k] for r in rows) / n, 4)
        agg["recall"]["strict"][k] = round(
            sum(r["strict"][k] for r in rows) / n, 4)
    agg["pool30"]["lenient"] = round(
        sum(r["lenient"][30] for r in rows) / n, 4)
    agg["pool30"]["strict"] = round(
        sum(r["strict"][30] for r in rows) / n, 4)
    for cat in cats:
        crows = [r for r in rows if r["category"] == cat]
        cn = len(crows)
        agg["per_category"][cat] = {
            "n": cn,
            "lenient@10": round(sum(r["lenient"][10] for r in crows) / cn, 4),
            "lenient@30": round(sum(r["lenient"][30] for r in crows) / cn, 4),
            "strict@10": round(sum(r["strict"][10] for r in crows) / cn, 4),
            "strict@30": round(sum(r["strict"][30] for r in crows) / cn, 4),
            "ce_modes": {},
        }
        for mode in sorted({r["ce_mode"] for r in crows}):
            agg["per_category"][cat]["ce_modes"][mode] = sum(
                1 for r in crows if r["ce_mode"] == mode)
    all_phases = sorted({p for r in rows for p in r["phases"]})
    for p in all_phases:
        agg["phase_fire_rate"][p] = round(
            sum(1 for r in rows if p in r["phases"]) / n, 4)
    all_errs = sorted({e for r in rows for e in r["phase_errors"]})
    for e in all_errs:
        agg["phase_error_rate"][e] = round(
            sum(1 for r in rows if e in r["phase_errors"]) / n, 4)
    for mode in sorted(set(_ce_modes_seen)):
        agg["ce_modes"][mode] = sum(1 for m in _ce_modes_seen if m == mode)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / (
        f"locomo-probe-{max_questions}.json" if max_questions else "locomo-probe-full.json")
    out_path.write_text(json.dumps({"summary": agg, "rows": rows}, indent=2))
    print(f"\nProbe results -> {out_path}\n")

    print(f"== {n} questions, avg {agg['avg_n_results']} results/q, "
          f"avg {agg['avg_latency_ms']}ms ==")
    print("Recall (lenient eval metric): " + "  ".join(
        f"@{k}={agg['recall']['lenient'][k]:.3f}" for k in K_VALUES))
    print("Recall (strict exact-id):     " + "  ".join(
        f"@{k}={agg['recall']['strict'][k]:.3f}" for k in K_VALUES))
    print(f"Pool@30: lenient={agg['pool30']['lenient']:.3f}  "
          f"strict={agg['pool30']['strict']:.3f}")
    print(f"CE modes: {agg['ce_modes']}")
    print("Phase fire rates: " + "  ".join(
        f"{p}={agg['phase_fire_rate'][p]:.2f}" for p in all_phases))
    if all_errs:
        print("Phase error rates: " + "  ".join(
            f"{e}={agg['phase_error_rate'][e]:.2f}" for e in all_errs))
    print("Per category (lenient@10 / lenient@30 / strict@10 / strict@30):")
    for cat in cats:
        d = agg["per_category"][cat]
        print(f"  {cat:12s} n={d['n']:3d}  "
              f"{d['lenient@10']:.3f} / {d['lenient@30']:.3f} / "
              f"{d['strict@10']:.3f} / {d['strict@30']:.3f}   {d['ce_modes']}")

    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LoCoMo retrieval diagnostic probe")
    parser.add_argument("--max-questions", type=int, default=None)
    args = parser.parse_args()
    main(max_questions=args.max_questions)
