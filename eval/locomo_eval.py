#!/usr/bin/env python3
"""LoCoMo benchmark evaluation for agentic-memory.

Downloads the LoCoMo dataset (10 long conversations, ~2000 QA pairs),
builds and caches a persistent multi-indexed SQLite database with authentic
session timestamps, and evaluates retrieval Recall@k across conversation sessions
with complete 14-phase search orchestrator observability.

Category mapping (from LoCoMo paper):
  1 = single-hop,  2 = multi-hop,  3 = temporal,
  4 = open-domain, 5 = adversarial

Usage:
    python eval/locomo_eval.py --build-db-only     # build & cache multi-index DB
    python eval/locomo_eval.py                    # full benchmark run
    python eval/locomo_eval.py --max-questions 50 # smoke test
    python eval/locomo_eval.py --conversation conv-26  # single conversation
    python eval/locomo_eval.py --resume           # resume interrupted run
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bootstrap & Environment
# ---------------------------------------------------------------------------
EVAL_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EVAL_ROOT.parent
RESULTS_DIR = EVAL_ROOT / "results"
DATASET_DIR = EVAL_ROOT / "datasets"
CACHE_DIR = EVAL_ROOT / ".cache" / "dbs"
LOCOMO_JSON = DATASET_DIR / "locomo10.json"
DOWNLOAD_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(EVAL_ROOT))

import memory_mcp  # noqa: E402
if not hasattr(memory_mcp, "safety_wiring"):
    setattr(memory_mcp, "safety_wiring", False)

from infra.memory_common import open_db  # noqa: E402
from _fixtures import (  # noqa: E402
    bootstrap_temp_db_clean,
    format_query_progress,
    init_benchmark_stdout,
    populate_eval_memory_indexes_batch,
    print_stage_banner,
    print_summary_report,
    set_benchmark_env,
    write_live_progress,
)

init_benchmark_stdout()
set_benchmark_env()
os.environ["MEMORY_WRITE_QUEUE_TIMEOUT"] = "120.0"
os.environ["MEMORY_LLM_EXTRACTION"] = "false"
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
# Dataset Download & Timestamp Parsing
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
    return data


def parse_locomo_datetime(dt_str: str | None) -> str:
    """Parse LoCoMo date string (e.g. '1:56 pm on 8 May, 2023') to ISO-8601 UTC string."""
    if not dt_str:
        return datetime(2023, 1, 1, tzinfo=timezone.utc).isoformat()
    s = str(dt_str).strip().replace(",", "")
    for fmt in (
        "%I:%M %p on %d %B %Y",
        "%I:%M %p on %d %b %Y",
        "%H:%M on %d %B %Y",
        "%H:%M on %d %b %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass
    return datetime(2023, 1, 1, tzinfo=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Conversation -> Memory Ingestion
# ---------------------------------------------------------------------------

def dia_id_to_session_num(dia_id: str) -> str:
    """Extract session number from a dia_id like 'D3:14' -> '3'."""
    return dia_id.split(":")[0].lstrip("D")


def session_to_memory_id(sample_id: str, session_key: str) -> str:
    """Stable memory ID for a session within a conversation."""
    return f"locomo/{sample_id}/{session_key}"


def session_to_content(sample_id: str, session_key: str, turns: list[dict], date_time_str: str = "") -> str:
    """Serialize a conversation session into a searchable text block with metadata header."""
    header = f"[Conversation: {sample_id}, Session: {session_key}"
    if date_time_str:
        header += f" | Date: {date_time_str}"
    header += "]"
    lines = [header]
    for turn in turns:
        speaker = turn.get("speaker", "unknown")
        text = turn.get("text", "")
        dia_id = turn.get("dia_id", "")
        lines.append(f"({dia_id}) {speaker}: {text}")
    return "\n".join(lines)


def ingest_all_conversations(
    db_path: Path, data: list[dict]
) -> dict[str, dict[str, str]]:
    """Ingest all conversations using batched multi-indexing (FTS, Vectors, ColBERT, SPLADE, KG).

    Returns mapping: sample_id -> {session_key -> memory_id}.
    """
    all_session_maps: dict[str, dict[str, str]] = {}
    batch_items = []
    base_time = datetime(2023, 1, 1, tzinfo=timezone.utc)

    with open_db(db_path, pooled=False) as db:
        for conv_idx, sample in enumerate(data):
            sample_id = sample["sample_id"]
            conv = sample["conversation"]
            session_keys = sorted(
                [
                    k
                    for k in conv.keys()
                    if k.startswith("session_")
                    and not k.endswith("_date_time")
                    and not k.endswith("_observation")
                    and not k.endswith("_summary")
                ],
                key=lambda k: int(k.split("_")[1]),
            )
            session_map: dict[str, str] = {}

            for sess_idx, sk in enumerate(session_keys):
                turns = conv[sk]
                if not isinstance(turns, list):
                    continue
                raw_dt = conv.get(f"{sk}_date_time", "")
                sess_time = (
                    parse_locomo_datetime(raw_dt)
                    if raw_dt
                    else (base_time + timedelta(days=conv_idx * 30 + sess_idx)).isoformat()
                )
                mem_id = session_to_memory_id(sample_id, sk)
                content = session_to_content(sample_id, sk, turns, str(raw_dt))
                source_file = f"locomo/{sample_id}/{sk}"
                tags_list = [sample_id, sk]

                db.execute(
                    """INSERT OR REPLACE INTO memories
                       (id, content, source_file, tags, created_at, updated_at,
                        observed_at, pinned, importance, category, repo_id,
                        access_count, success_score, fitness_score, tenant_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, 3, 'sessions', ?, 1, 0.0, 1.0, ?)""",
                    (
                        mem_id,
                        content,
                        source_file,
                        json.dumps(tags_list),
                        sess_time,
                        sess_time,
                        sess_time,
                        sample_id,
                        f"locomo_{sample_id}",
                    ),
                )
                try:
                    db.execute(
                        "INSERT OR REPLACE INTO memories_fts (id, content) VALUES (?, ?)",
                        (mem_id, content),
                    )
                except Exception as exc:
                    logger.debug("FTS insert failed for %s (non-fatal): %s", mem_id, exc)

                batch_items.append((mem_id, content, "sessions", tags_list))
                session_map[sk] = mem_id

            all_session_maps[sample_id] = session_map

        # Fast batched multi-indexing (ColBERT, SPLADE, Vector, Chunks, KG)
        populate_eval_memory_indexes_batch(db, batch_items)
        db.commit()

    if batch_items:
        try:
            from rebuild_vec_index import rebuild_vec_index

            stats = rebuild_vec_index(str(db_path))
            print(
                f"✓ Vector index built: {stats.get('n_indexed')} items ({stats.get('serialized_bytes')} bytes) in {stats.get('elapsed_s', 0.0):.2f}s",
                flush=True,
            )
        except Exception as exc:
            logger.warning("vec index build failed (non-fatal): %s", exc)

    return all_session_maps


def build_or_load_db(
    data: list[dict],
    cache_db_path: Path,
    use_cache_db: bool = True,
    rebuild: bool = False,
    conversation_filter: str | None = None,
) -> tuple[Path, bool, float]:
    """Ensure multi-indexed database exists and is cached.

    Returns: (db_path, is_cleanup_needed, ingest_time_seconds)
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_tmp = False

    if use_cache_db and not conversation_filter and cache_db_path.exists() and not rebuild:
        try:
            with open_db(cache_db_path, pooled=False) as chk_conn:
                cnt = chk_conn.execute("SELECT count(*) FROM memories").fetchone()
                if cnt and cnt[0] > 0:
                    print(f"✓ Using cached LoCoMo database ({cnt[0]} sessions): {cache_db_path}", flush=True)
                    return cache_db_path, False, 0.0
        except Exception as exc:
            logger.debug("Cache validation failed, will rebuild: %s", exc)

    if use_cache_db and not conversation_filter:
        db_path = cache_db_path
        if db_path.exists():
            try:
                db_path.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        tmpdir = Path(tempfile.mkdtemp(prefix="locomo_eval_"))
        db_path = tmpdir / "memory.db"
        cleanup_tmp = True

    os.environ["MEMORY_DB_PATH"] = str(db_path)
    bootstrap_temp_db_clean(db_path)

    ingest_start = time.time()
    all_maps = ingest_all_conversations(db_path, data)
    ingest_time = time.time() - ingest_start
    total_sess = sum(len(m) for m in all_maps.values())
    print(f"✓ Ingested {total_sess} sessions from {len(data)} conversations in {ingest_time:.1f}s", flush=True)
    return db_path, cleanup_tmp, ingest_time


# ---------------------------------------------------------------------------
# Gold Evidence Extraction
# ---------------------------------------------------------------------------

def extract_gold_sessions(qa: dict) -> set[str]:
    """Extract gold session numbers from evidence dia_ids."""
    sessions = set()
    for dia_id in qa.get("evidence", []):
        sess_num = dia_id_to_session_num(dia_id)
        sessions.add(sess_num)
    return sessions


# ---------------------------------------------------------------------------
# Search Execution with 14-Phase Observability
# ---------------------------------------------------------------------------

def run_search(
    db_path: Path,
    query: str,
    limit: int = 30,
    tenant_id: str = "locomo",
    light: bool = False,
) -> tuple[list[str], dict[str, float], dict[str, Any]]:
    """Run hybrid search and return (ranked memory IDs, phase latencies, phase errors)."""
    from search.orchestrator import search_memories

    result = search_memories(
        db_path=db_path,
        query=query,
        limit=max(limit, 30),
        include_global=True,
        rerank=not light,
        include_facts=True,
        safety_wiring=False,
        tenant_id=tenant_id,
        category="sessions",
        hybrid=True,
        mode="fts" if light else "hybrid",
    )
    ranked = [r["id"] for r in result.get("results", [])]
    phase_lats = result.get("phase_latencies", {})
    phase_errs = result.get("phase_errors", {})
    return ranked, phase_lats, phase_errs


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------

def warmup_search_pipeline(db_path: Path) -> None:
    """Pre-warm dense vectors, cross-encoders, and search orchestrator."""
    print("Pre-warming dense vectors & cross-encoders...")
    try:
        from infra._lazy_imports import get_embedding_search

        es = get_embedding_search()
        if hasattr(es, "model") and es.model is not None:
            _ = es.model.encode(["warmup query"], show_progress_bar=False)
    except Exception as exc:
        logger.debug("Dense vector warmup notice: %s", exc)

    try:
        _ranked, _lats, _ = run_search(db_path, "warmup query", limit=1)
        print("✓ Encoders pre-warmed successfully.", flush=True)
    except Exception as exc:
        print(f"  ⚠ Warmup non-fatal notice: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Main Evaluation Function
# ---------------------------------------------------------------------------

def evaluate(
    max_questions: int | None = None,
    conversation_filter: str | None = None,
    k_values: list[int] | None = None,
    output_path: Path | None = None,
    use_cache_db: bool = True,
    rebuild: bool = False,
    resume: bool = False,
    light: bool = False,
    build_db_only: bool = False,
) -> dict:
    """Run LoCoMo evaluation with full 14-phase observability and persistent caching."""
    k_vals = k_values or K_VALUES

    print(f"\n{'='*80}", flush=True)
    print("BENCHMARK SUITE: LOCOMO LONG-CONVERSATION MEMORY EVALUATION", flush=True)
    print(f"{'='*80}", flush=True)

    # Phase 1: Dataset Loading
    print_stage_banner(1, "Dataset Loading", "LoCoMo Dataset (Snap Research)")
    t_load = time.time()
    data = ensure_dataset()

    if conversation_filter:
        data = [s for s in data if s["sample_id"] == conversation_filter]
        if not data:
            raise ValueError(f"Conversation '{conversation_filter}' not found")

    total_qa = sum(len(s.get("qa", [])) for s in data)
    print(
        f"✓ Loaded {len(data)} conversations ({total_qa} QA pairs) in {time.time() - t_load:.2f}s",
        flush=True,
    )

    # Phase 2: Database Ingestion & Multi-Index Building
    print_stage_banner(2, "Database Ingestion & Multi-Index Building", f"cache={use_cache_db}, rebuild={rebuild}")
    cache_db_path = CACHE_DIR / "locomo_full.db"
    db_path, cleanup_tmp, ingest_time = build_or_load_db(
        data=data,
        cache_db_path=cache_db_path,
        use_cache_db=use_cache_db,
        rebuild=rebuild,
        conversation_filter=conversation_filter,
    )

    if build_db_only:
        print(f"\n✓ Database build complete: {db_path} ({db_path.stat().st_size / 1024:.1f} KB)")
        return {"status": "db_built", "db_path": str(db_path), "ingest_time_s": ingest_time}

    # Collect questions
    questions = []
    for sample in data:
        sid = sample["sample_id"]
        for q_idx, qa in enumerate(sample.get("qa", [])):
            gold = extract_gold_sessions(qa)
            cat_num = qa.get("category", 0)
            cat_name = CATEGORY_MAP.get(cat_num, f"unknown-{cat_num}")
            questions.append({
                "question_id": f"{sid}_q{q_idx:04d}",
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

    # Resolve output path
    if output_path is None:
        suffix = f"-{max_questions}" if max_questions else "-full"
        if conversation_filter:
            suffix = f"-{conversation_filter}"
        output_path = RESULTS_DIR / f"locomo-eval{suffix}.json"

    checkpoint_path = Path(str(output_path) + ".checkpoint")
    completed_qids: set[str] = set()
    per_question: list[dict] = []
    per_category_hits: dict[str, list[float]] = {}
    latencies: list[float] = []
    phase_latencies_accum: dict[str, list[float]] = {}
    reciprocal_ranks: list[float] = []

    # Checkpoint resumption
    if resume and (output_path.exists() or checkpoint_path.exists()):
        read_p = output_path if output_path.exists() else checkpoint_path
        try:
            with open(read_p, "r", encoding="utf-8") as f:
                prev = json.load(f)
                for item in prev.get("per_question", []):
                    qid = item.get("question_id") or item.get("question")
                    completed_qids.add(qid)
                    per_question.append(item)
                    cat = item.get("category", "unknown")
                    score_val = 1.0 if item.get("hit_top10") else 0.0
                    per_category_hits.setdefault(cat, []).append(score_val)
                    if "latency_ms" in item:
                        latencies.append(item["latency_ms"])
            print(f"✓ Resuming run: loaded {len(completed_qids)} completed questions from {read_p}")
        except Exception as exc:
            print(f"WARNING: Failed to read checkpoint {read_p} ({exc}), starting from scratch")
            completed_qids.clear()
            per_question.clear()
            per_category_hits.clear()
            latencies.clear()

    # Phase 3: Warmup
    print_stage_banner(3, "Search Pipeline Warmup", "Pre-warming dense vectors & cross-encoders")
    warmup_search_pipeline(db_path)

    # Phase 4: Evaluation Execution
    print_stage_banner(
        4,
        "Evaluation Execution",
        f"{len(questions)} questions across {len(data)} conversations (14-phase search orchestrator)",
    )

    # Initialize metrics structure
    metrics: dict[int, dict[str, dict]] = {}
    for k in k_vals:
        metrics[k] = {}
        for cat in CATEGORY_MAP.values():
            metrics[k][cat] = {"total": 0, "session_hits": 0, "session_recall_at_k": 0.0}
        metrics[k]["__overall__"] = {"total": 0, "session_hits": 0, "session_recall_at_k": 0.0}

    # Replay loaded items into metrics
    for item in per_question:
        cat = item.get("category", "unknown")
        for k in k_vals:
            if cat in metrics[k]:
                metrics[k][cat]["total"] += 1
            metrics[k]["__overall__"]["total"] += 1
            hit_k = item.get(f"hit_top{k}", False)
            if hit_k:
                if cat in metrics[k]:
                    metrics[k][cat]["session_hits"] += 1
                metrics[k]["__overall__"]["session_hits"] += 1

    progress_file = RESULTS_DIR / ".progress.json"
    suite_progress_file = RESULTS_DIR / ".progress_locomo.json"
    wall_start = time.time()

    def _save_checkpoint():
        if not per_question:
            return
        tmp_ckpt = checkpoint_path.with_suffix(".tmp")
        try:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_ckpt, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "n_completed": len(per_question),
                        "total_questions": len(questions),
                        "per_question": per_question,
                    },
                    f,
                    indent=2,
                )
            tmp_ckpt.replace(checkpoint_path)
        except Exception as exc:
            logger.debug("Checkpoint save failed (non-fatal): %s", exc)

    try:
        for idx, q in enumerate(questions, start=1):
            qid = q["question_id"]
            if qid in completed_qids or q["question"] in completed_qids:
                continue

            if hasattr(memory_mcp, "_search_cache"):
                memory_mcp._search_cache.clear()

            q_start = time.time()
            retrieved, phase_lats, phase_errs = run_search(
                db_path=db_path,
                query=q["question"],
                limit=max(k_vals),
                tenant_id=f"locomo_{q['sample_id']}",
                light=light,
            )
            latency_ms = (time.time() - q_start) * 1000.0
            latencies.append(latency_ms)

            for pname, plat in phase_lats.items():
                phase_latencies_accum.setdefault(pname, []).append(plat)

            # Exact gold memory IDs for this specific conversation
            gold_mem_ids = {
                session_to_memory_id(q["sample_id"], f"session_{n}")
                for n in q["gold_sessions"]
            }
            hit_10 = bool(set(retrieved[:10]) & gold_mem_ids)
            cat = q["category"]

            # Compute reciprocal rank (MRR)
            rr = 0.0
            for rank_idx, mid in enumerate(retrieved, start=1):
                if mid in gold_mem_ids:
                    rr = 1.0 / rank_idx
                    break
            reciprocal_ranks.append(rr)

            # Check hits at each k
            hits_by_k = {}
            for k in k_vals:
                top_k_ids = set(retrieved[:k])
                hit_k = bool(top_k_ids & gold_mem_ids)
                hits_by_k[k] = hit_k

                if cat in metrics[k]:
                    metrics[k][cat]["total"] += 1
                metrics[k]["__overall__"]["total"] += 1
                if hit_k:
                    if cat in metrics[k]:
                        metrics[k][cat]["session_hits"] += 1
                    metrics[k]["__overall__"]["session_hits"] += 1

            score_val = 1.0 if hit_10 else 0.0
            per_category_hits.setdefault(cat, []).append(score_val)

            q_record = {
                "question_id": qid,
                "question": q["question"][:120],
                "category": cat,
                "category_num": q["category_num"],
                "sample_id": q["sample_id"],
                "gold_mem_ids": sorted(gold_mem_ids),
                "retrieved_top5": retrieved[:5],
                "hit_top1": hits_by_k.get(1, False),
                "hit_top5": hits_by_k.get(5, False),
                "hit_top10": hit_10,
                "hit_top20": hits_by_k.get(20, False),
                "reciprocal_rank": round(rr, 4),
                "latency_ms": round(latency_ms, 1),
                "phase_latencies": {k: round(v, 2) for k, v in phase_lats.items()},
            }
            if phase_errs:
                q_record["phase_errors"] = phase_errs

            per_question.append(q_record)
            completed_qids.add(qid)

            running_acc = (
                metrics[10]["__overall__"]["session_hits"]
                / max(1, metrics[10]["__overall__"]["total"])
            )
            running_per_type = {
                c: sum(scores) / len(scores) for c, scores in per_category_hits.items() if scores
            }

            # Single-line query progress
            line_msg = format_query_progress(
                q_num=len(per_question),
                total_q=len(questions),
                score=score_val,
                latency_ms=latency_ms,
                running_acc=running_acc,
                category=cat,
                query_text=q["question"],
                extra_metric_label="Rec@10",
            )
            print(line_msg, flush=True)

            # Atomic live progress writer
            for p_file in (progress_file, suite_progress_file):
                write_live_progress(
                    progress_file=p_file,
                    q_num=len(per_question),
                    total_q=len(questions),
                    category=cat,
                    question_text=q["question"],
                    score=score_val,
                    latency_ms=latency_ms,
                    running_overall=running_acc,
                    running_per_type=running_per_type,
                    extra_fields={
                        "benchmark": "LoCoMo",
                        "conversation_sample_id": q["sample_id"],
                        "mrr": round(sum(reciprocal_ranks) / max(1, len(reciprocal_ranks)), 4),
                    },
                )

            # Periodic garbage collection to control memory growth
            if len(per_question) % 25 == 0:
                gc.collect()

            # Incremental checkpoint every 5 questions or first/last
            if (len(per_question) % 5 == 0) or idx == 1 or idx == len(questions):
                _save_checkpoint()

    finally:
        pass

    # Compute recall@k rates
    for k in k_vals:
        for cat_data in metrics[k].values():
            t = cat_data["total"]
            if t > 0:
                cat_data["session_recall_at_k"] = round(
                    cat_data["session_hits"] / t, 4
                )

    # Phase 5: Results Aggregation & Verification
    print_stage_banner(5, "Results Aggregation & Verification", f"{len(per_question)} questions analyzed")

    from eval.bench.metrics import calculate_latency_stats

    latency_stats = calculate_latency_stats(latencies)
    wall_time = time.time() - wall_start
    mrr_val = sum(reciprocal_ranks) / max(1, len(reciprocal_ranks))

    phase_lats_avg = {
        p: round(sum(lats) / len(lats), 2)
        for p, lats in sorted(phase_latencies_accum.items())
        if lats
    }

    results = {
        "benchmark": "LoCoMo-10",
        "n_questions_total": len(per_question),
        "n_conversations": len(data),
        "wall_time_seconds": round(wall_time, 2),
        "latency_ms": latency_stats,
        "mrr": round(mrr_val, 4),
        "phase_latencies_avg_ms": phase_lats_avg,
        "ingest_time_seconds": round(ingest_time, 2),
        "metrics": {f"k={k}": metrics[k] for k in k_vals},
        "config": {
            "dataset": str(LOCOMO_JSON),
            "k_values": k_vals,
            "max_questions": max_questions,
            "conversation_filter": conversation_filter,
            "cached_db": not cleanup_tmp,
            "light": light,
        },
        "per_question": per_question,
    }

    # Save output file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    # Remove temporary checkpoint upon clean completion
    if checkpoint_path.exists():
        try:
            checkpoint_path.unlink(missing_ok=True)
        except OSError:
            pass

    cat_rec10 = {
        cat: metrics[10][cat]["session_recall_at_k"]
        for cat in CATEGORY_MAP.values()
        if metrics[10][cat]["total"] > 0
    }
    cat_counts = {
        cat: metrics[10][cat]["total"]
        for cat in CATEGORY_MAP.values()
        if metrics[10][cat]["total"] > 0
    }
    retrieval_recalls = {
        f"Recall@{k}": metrics[k]["__overall__"]["session_recall_at_k"]
        for k in k_vals
    }
    retrieval_recalls["MRR"] = round(mrr_val, 4)
    overall_r10 = metrics[10]["__overall__"]["session_recall_at_k"]

    print_summary_report(
        benchmark_name="LoCoMo",
        total_q=len(per_question),
        wall_time_s=wall_time,
        overall_metric=overall_r10,
        metric_name="Recall@10 (overall)",
        category_scores=cat_rec10,
        category_counts=cat_counts,
        latency_stats=latency_stats,
        retrieval_recalls=retrieval_recalls,
        output_path=output_path,
    )

    if phase_lats_avg:
        print("\nSearch Phase Latency Breakdown (Mean ms):")
        for pname, pms in phase_lats_avg.items():
            print(f"  {pname:<35}: {pms:6.1f} ms")

    if cleanup_tmp and db_path.exists():
        try:
            shutil.rmtree(db_path.parent, ignore_errors=True)
        except OSError:
            pass

    return results


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="LoCoMo benchmark evaluation")
    parser.add_argument(
        "--build-db-only",
        action="store_true",
        help="Build and cache the multi-index database only, then exit",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=None,
        help="Limit number of questions (for smoke testing)",
    )
    parser.add_argument(
        "--conversation",
        type=str,
        default=None,
        help="Evaluate only one conversation (sample_id, e.g. conv-26)",
    )
    parser.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=None,
        help="k values for recall@k (default: 1 5 10 20)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON path",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild DB index from scratch",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not use cached prebuilt DB",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from existing output or checkpoint file",
    )
    parser.add_argument(
        "--light",
        action="store_true",
        help="Run in light search mode (fast FTS evaluation)",
    )
    args = parser.parse_args()

    out = Path(args.output) if args.output else None
    evaluate(
        max_questions=args.max_questions,
        conversation_filter=args.conversation,
        k_values=args.k,
        output_path=out,
        use_cache_db=not args.no_cache,
        rebuild=args.rebuild,
        resume=args.resume,
        light=args.light,
        build_db_only=args.build_db_only,
    )


if __name__ == "__main__":
    main()
