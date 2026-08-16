#!/usr/bin/env python3
"""LoCoMo benchmark evaluation for agentic-memory.

Downloads the LoCoMo dataset (10 long conversations, ~2000 QA pairs),
ingests each conversation's sessions as memory notes, then measures
retrieval recall@k by checking whether the gold evidence sessions appear
in the top-k search results.

Category mapping (from LoCoMo paper):
  1 = single-hop,  2 = multi-hop,  3 = temporal,
  4 = open-domain, 5 = adversarial

Usage:
    python eval/locomo_eval.py                    # full run
    python eval/locomo_eval.py --max-questions 50 # smoke test
    python eval/locomo_eval.py --conversation conv-26  # single conversation
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
EVAL_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EVAL_ROOT.parent
RESULTS_DIR = EVAL_ROOT / "results"
DATASET_DIR = EVAL_ROOT / "datasets"
CACHE_DIR = EVAL_ROOT / ".cache" / "dbs"
LOCOMO_JSON = DATASET_DIR / "locomo10.json"
DOWNLOAD_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"

sys.path.insert(0, str(REPO_ROOT))

import memory_mcp  # noqa: E402
from infra.memory_common import open_db  # noqa: E402
from _fixtures import (  # noqa: E402
    bootstrap_temp_db_clean,
    populate_eval_memory_indexes,
    populate_eval_memory_indexes_batch,
    set_benchmark_env,
)

set_benchmark_env()
os.environ["MEMORY_WRITE_QUEUE_TIMEOUT"] = "120.0"
os.environ["MEMORY_LLM_EXTRACTION"] = "false"
# The eval deliberately points MEMORY_DB_PATH at a dedicated eval DB.
# Arm the config-drift escape hatch (audited, time-bounded) so the
# stability-tier drift this creates is downgraded to a warning.
os.environ["MEMORY_ESCAPE_HATCH"] = (
    "ignore-stability;locomo-benchmark-temp-db;locomo_eval;14400;60"
)


CATEGORY_MAP = {
    1: "single-hop",
    2: "multi-hop",
    3: "temporal",
    4: "open-domain",
    5: "adversarial",
}

K_VALUES = [1, 5, 10, 20]


# ---------------------------------------------------------------------------
# Dataset download
# ---------------------------------------------------------------------------

def ensure_dataset() -> list[dict]:
    """Download locomo10.json if missing, return parsed list of samples."""
    import urllib.request

    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    if not LOCOMO_JSON.exists():
        print(f"Downloading LoCoMo dataset to {LOCOMO_JSON} ...")
        urllib.request.urlretrieve(DOWNLOAD_URL, str(LOCOMO_JSON))
        print("  done.")

    with open(LOCOMO_JSON, encoding="utf-8") as f:
        data = json.load(f)
    print(f"Loaded {len(data)} conversations, "
          f"{sum(len(s['qa']) for s in data)} total questions.")
    return data


# ---------------------------------------------------------------------------
# Conversation -> memory ingestion
# ---------------------------------------------------------------------------

def dia_id_to_session_num(dia_id: str) -> str:
    """Extract session number from a dia_id like 'D3:14' -> '3'."""
    return dia_id.split(":")[0].lstrip("D")


def session_to_memory_id(sample_id: str, session_key: str) -> str:
    """Stable memory ID for a session within a conversation."""
    return f"locomo/{sample_id}/{session_key}"


def session_to_content(sample_id: str, session_key: str, turns: list[dict]) -> str:
    """Serialize a conversation session into a searchable text block."""
    lines = [f"[Conversation: {sample_id}, Session: {session_key}]"]
    for turn in turns:
        speaker = turn.get("speaker", "unknown")
        text = turn.get("text", "")
        dia_id = turn.get("dia_id", "")
        lines.append(f"({dia_id}) {speaker}: {text}")
    return "\n".join(lines)


def ingest_all_conversations(
    db_path: Path, data: list[dict]
) -> dict[str, dict[str, str]]:
    """Ingest all conversations using fast batched multi-indexing.

    Returns mapping: sample_id -> {session_key -> memory_id}.
    """
    all_session_maps: dict[str, dict[str, str]] = {}
    batch_items = []

    base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)

    with open_db(db_path, pooled=False) as db:
        for conv_idx, sample in enumerate(data):
            sample_id = sample["sample_id"]
            conv = sample["conversation"]
            session_keys = sorted(
                [k for k in conv.keys()
                 if k.startswith("session_")
                 and not k.endswith("_date_time")
                 and not k.endswith("_observation")
                 and not k.endswith("_summary")],
                key=lambda k: int(k.split("_")[1]),
            )
            session_map: dict[str, str] = {}

            for sess_idx, sk in enumerate(session_keys):
                turns = conv[sk]
                if not isinstance(turns, list):
                    continue
                mem_id = session_to_memory_id(sample_id, sk)
                content = session_to_content(sample_id, sk, turns)
                source_file = f"locomo/{sample_id}/{sk}"
                # Valid ISO timestamp for chronological ordering & temporal reasoning
                sess_time = (base_time + timedelta(days=conv_idx * 30 + sess_idx)).isoformat()

                db.execute(
                    """INSERT OR REPLACE INTO memories
                       (id, content, source_file, tags, created_at, updated_at,
                        observed_at, pinned, importance, category, repo_id,
                        access_count, success_score, fitness_score, tenant_id)
                       VALUES (?, ?, ?, '[]', ?, ?, ?, 0, 3, 'sessions', ?, 1, 0.0, 1.0, 'locomo')""",
                    (mem_id, content, source_file,
                     sess_time, sess_time, sess_time, sample_id),
                )
                try:
                    db.execute(
                        "INSERT OR REPLACE INTO memories_fts (id, content) VALUES (?, ?)",
                        (mem_id, content),
                    )
                except Exception as exc:
                    logger.debug("FTS index insert failed for %s (non-fatal): %s", mem_id, exc)

                batch_items.append((mem_id, content, "sessions", [sample_id]))
                session_map[sk] = mem_id

            all_session_maps[sample_id] = session_map

        # Fast batched multi-indexing (ColBERT, SPLADE, Vector, Chunks, KG)
        populate_eval_memory_indexes_batch(db, batch_items)
        db.commit()

    return all_session_maps


def ingest_conversation(
    db_path: Path, sample: dict
) -> dict[str, str]:
    """Compatibility wrapper for single conversation ingestion."""
    maps = ingest_all_conversations(db_path, [sample])
    return maps.get(sample["sample_id"], {})


# ---------------------------------------------------------------------------
# Gold session extraction
# ---------------------------------------------------------------------------

def extract_gold_sessions(qa: dict) -> set[str]:
    """Extract gold session numbers from evidence dia_ids.

    Returns set of session numbers (as strings) that contain the answer.
    """
    sessions = set()
    for dia_id in qa.get("evidence", []):
        sess_num = dia_id_to_session_num(dia_id)
        sessions.add(sess_num)
    return sessions


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def run_search(db_path: Path, query: str, limit: int = 30) -> list[str]:
    """Run hybrid search and return list of memory IDs in rank order."""
    from search.orchestrator import search_memories

    result = search_memories(
        db_path,
        query,
        limit=max(limit, 30),
        include_global=True,
        rerank=True,
        include_facts=False,
        safety_wiring=False,
        tenant_id="locomo",
        category="sessions",
    )
    return [r["id"] for r in result.get("results", [])]



def evaluate(
    max_questions: int | None = None,
    conversation_filter: str | None = None,
    k_values: list[int] | None = None,
    output_path: Path | None = None,
    use_cache_db: bool = True,
    rebuild: bool = False,
) -> dict:
    """Run the full LoCoMo evaluation.

    Args:
        max_questions: Cap on total questions (None = all).
        conversation_filter: Only evaluate one conversation (by sample_id).
        k_values: Which k values to measure recall@k at.
        output_path: Where to write results JSON.
        use_cache_db: If True and full dataset, reuse cached prebuilt DB.
        rebuild: Force rebuild DB even if cached.

    Returns:
        Results dict with per-category and overall metrics.
    """
    k_vals = k_values or K_VALUES
    data = ensure_dataset()

    if conversation_filter:
        data = [s for s in data if s["sample_id"] == conversation_filter]
        if not data:
            raise ValueError(f"Conversation '{conversation_filter}' not found")

    wall_start = time.time()
    cleanup_tmp = False

    # Check for cached full DB
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_db_path = CACHE_DIR / "locomo_full.db"

    if use_cache_db and not conversation_filter and cache_db_path.exists() and not rebuild:
        db_path = cache_db_path
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        print(f"Using cached LoCoMo database: {db_path}")
        ingest_time = 0.0
        total_sessions = sum(
            len([k for k in s["conversation"].keys() if k.startswith("session_") and "_" not in k[8:]])
            for s in data
        )
    else:
        if use_cache_db and not conversation_filter:
            db_path = cache_db_path
            if db_path.exists():
                try:
                    db_path.unlink()
                except OSError:
                    pass
        else:
            tmpdir = Path(tempfile.mkdtemp(prefix="locomo_eval_"))
            db_path = tmpdir / "memory.db"
            cleanup_tmp = True

        os.environ["MEMORY_DB_PATH"] = str(db_path)
        bootstrap_temp_db_clean(db_path)

        ingest_start = time.time()
        all_session_maps = ingest_all_conversations(db_path, data)
        ingest_time = time.time() - ingest_start
        total_sessions = sum(len(m) for m in all_session_maps.values())
        print(f"Ingested {total_sessions} sessions from {len(data)} conversations "
              f"in {ingest_time:.1f}s")

    # Collect all questions
    questions = []
    for sample in data:
        sid = sample["sample_id"]
        for qa in sample["qa"]:
            gold = extract_gold_sessions(qa)
            cat_num = qa.get("category", 0)
            cat_name = CATEGORY_MAP.get(cat_num, f"unknown-{cat_num}")
            questions.append({
                "sample_id": sid,
                "question": qa["question"],
                "answer": qa.get("answer", ""),
                "category": cat_name,
                "category_num": cat_num,
                "gold_sessions": gold,
                "evidence": qa.get("evidence", []),
            })

    if max_questions:
        questions = questions[:max_questions]

    print(f"Evaluating {len(questions)} questions ...")

    # Initialize metrics
    metrics: dict[int, dict[str, dict]] = {}
    for k in k_vals:
        metrics[k] = {}
        for cat in CATEGORY_MAP.values():
            metrics[k][cat] = {
                "total": 0,
                "session_hits": 0,
                "session_recall_at_k": 0.0,
            }
        metrics[k]["__overall__"] = {
            "total": 0,
            "session_hits": 0,
            "session_recall_at_k": 0.0,
        }

    latencies: list[float] = []
    per_question: list[dict] = []

    for i, q in enumerate(questions):
        if (i + 1) % 25 == 0 or i == 0 or i == len(questions) - 1:
            _elapsed = time.time() - wall_start
            _rate = (i + 1) / _elapsed if _elapsed > 0 else 0
            print(f"  [{i+1}/{len(questions)}] {_elapsed:.0f}s elapsed, {_rate:.1f} q/s")

        # Clear search cache between questions to prevent leakage
        if hasattr(memory_mcp, "_search_cache"):
            memory_mcp._search_cache.clear()

        q_start = time.time()
        retrieved = run_search(db_path, q["question"], limit=max(k_vals))
        latency_ms = (time.time() - q_start) * 1000
        latencies.append(latency_ms)

        # Exact gold memory IDs for this specific conversation
        gold_mem_ids = {session_to_memory_id(q["sample_id"], f"session_{n}") for n in q["gold_sessions"]}

        # Check hits at each k against exact gold memory IDs
        for k in k_vals:
            top_k_ids = set(retrieved[:k])
            hit = bool(top_k_ids & gold_mem_ids)
            cat = q["category"]

            metrics[k][cat]["total"] += 1
            metrics[k]["__overall__"]["total"] += 1
            if hit:
                metrics[k][cat]["session_hits"] += 1
                metrics[k]["__overall__"]["session_hits"] += 1

        per_question.append({
            "question": q["question"][:100],
            "category": q["category"],
            "sample_id": q["sample_id"],
            "gold_mem_ids": sorted(gold_mem_ids),
            "retrieved_top5": retrieved[:5],
        })

    # Compute recall@k
    for k in k_vals:
        for cat_data in metrics[k].values():
            t = cat_data["total"]
            if t > 0:
                cat_data["session_recall_at_k"] = round(
                    cat_data["session_hits"] / t, 4
                )

    # Latency stats
    latencies.sort()
    n_lat = len(latencies) or 1
    latency_stats = {
        "mean": round(sum(latencies) / n_lat, 2),
        "p50": round(latencies[n_lat // 2], 2),
        "p95": round(latencies[int(n_lat * 0.95)], 2),
        "max": round(latencies[-1], 2),
    }

    results = {
        "n_questions_total": len(questions),
        "n_conversations": len(data),
        "n_sessions_ingested": total_sessions,
        "latency_ms": latency_stats,
        "ingest_time_seconds": round(ingest_time, 2),
        "metrics": {
            f"k={k}": metrics[k] for k in k_vals
        },
        "config": {
            "dataset": str(LOCOMO_JSON),
            "k_values": k_vals,
            "max_questions": max_questions,
            "conversation_filter": conversation_filter,
            "cached_db": not cleanup_tmp,
        },
    }

    # Write results
    if output_path is None:
        suffix = f"-{max_questions}" if max_questions else "-full"
        if conversation_filter:
            suffix = f"-{conversation_filter}"
        output_path = RESULTS_DIR / f"locomo-eval{suffix}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults written to {output_path}")

    # Print summary
    print("\n=== LoCoMo Evaluation Results ===")
    print(f"Conversations: {len(data)}, Sessions: {total_sessions}, "
          f"Questions: {len(questions)}")
    print(f"Ingest time: {ingest_time:.1f}s, "
          f"Mean search latency: {latency_stats['mean']}ms")
    for k in k_vals:
        overall = metrics[k]["__overall__"]
        print(f"\n  Recall@{k} (overall): {overall['session_recall_at_k']:.4f} "
              f"({overall['session_hits']}/{overall['total']})")
        for cat in ["single-hop", "multi-hop", "temporal", "open-domain", "adversarial"]:
            d = metrics[k][cat]
            if d["total"] > 0:
                print(f"    {cat:15s}: {d['session_recall_at_k']:.4f} "
                      f"({d['session_hits']}/{d['total']})")

    # Cleanup
    if cleanup_tmp:
        shutil.rmtree(db_path.parent, ignore_errors=True)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LoCoMo benchmark evaluation")
    parser.add_argument("--max-questions", type=int, default=None,
                        help="Limit number of questions (for smoke testing)")
    parser.add_argument("--conversation", type=str, default=None,
                        help="Evaluate only one conversation (sample_id)")
    parser.add_argument("--k", type=int, nargs="+", default=None,
                        help="k values for recall@k (default: 1 5 10 20)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output JSON path")
    parser.add_argument("--rebuild", action="store_true",
                        help="Rebuild DB index from scratch")
    parser.add_argument("--no-cache", action="store_true",
                        help="Do not use cached prebuilt DB")
    args = parser.parse_args()

    out = Path(args.output) if args.output else None
    evaluate(
        max_questions=args.max_questions,
        conversation_filter=args.conversation,
        k_values=args.k,
        output_path=out,
        use_cache_db=not args.no_cache,
        rebuild=args.rebuild,
    )
