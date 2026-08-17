#!/usr/bin/env python3
"""BEAM Real Data Benchmark — uses the official BEAM-10M dataset.

Loads real conversations and probing questions from the BEAM parquet files,
ingests conversations as memory notes, and evaluates against ground truth.

Dataset: Mohammadta/BEAM-10M on HuggingFace
Structure: 10 conversations × 10 ability types × 2 questions = 200 questions
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

logger = logging.getLogger(__name__)

EVAL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_ROOT.parent.parent
RESULTS_DIR = EVAL_ROOT / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_PATH = RESULTS_DIR / "beam-real-run.json"

sys.path.insert(0, str(PROJECT_ROOT))

import memory_mcp  # noqa: E402
if not hasattr(memory_mcp, "safety_wiring"):
    setattr(memory_mcp, "safety_wiring", False)

from eval._fixtures import (  # noqa: E402
    populate_eval_memory_indexes_batch,
    set_benchmark_env,
)


set_benchmark_env()



# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

_env_beam_dir = os.environ.get("BEAM_DATA_DIR")
DATASET_BEAM_DIR = EVAL_ROOT.parent / "datasets" / "beam"
CACHE_DIR = EVAL_ROOT.parent / ".cache" / "dbs"

POSSIBLE_DIRS = [
    Path(_env_beam_dir) if _env_beam_dir else None,
    DATASET_BEAM_DIR,
    Path.home() / "Downloads",
]

PART_FILES = [
    "10M-00000-of-00002.parquet",
    "10M-00001-of-00002.parquet",
]

HF_BASE_URL = "https://huggingface.co/datasets/Mohammadta/BEAM-10M/resolve/main/data"


def ensure_beam_dataset() -> Path | None:
    """Find or download BEAM parquet files."""
    for d in POSSIBLE_DIRS:
        if d and d.exists():
            if all((d / pf).exists() for pf in PART_FILES):
                return d

    # If not found, try downloading into DATASET_BEAM_DIR
    DATASET_BEAM_DIR.mkdir(parents=True, exist_ok=True)
    import urllib.request

    all_downloaded = True
    for pf in PART_FILES:
        target = DATASET_BEAM_DIR / pf
        if not target.exists():
            url = f"{HF_BASE_URL}/{pf}"
            print(f"Downloading BEAM parquet part {pf} from HuggingFace ...")
            try:
                urllib.request.urlretrieve(url, str(target))
                print(f"  saved to {target}")
            except Exception as e:
                print(f"  failed to download {url}: {e}")
                all_downloaded = False
                break
    if all_downloaded and all((DATASET_BEAM_DIR / pf).exists() for pf in PART_FILES):
        return DATASET_BEAM_DIR

    # Fallback to any directory that has at least one part
    for d in POSSIBLE_DIRS:
        if d and d.exists() and any((d / pf).exists() for pf in PART_FILES):
            return d

    return None


def load_beam_dataset() -> list[dict]:
    """Load BEAM conversations and probing questions from parquet files."""
    import pyarrow.parquet as pq

    beam_dir = ensure_beam_dataset()
    if not beam_dir:
        print("WARNING: BEAM dataset parquet files not found and could not be downloaded.")
        return []

    conversations = []
    for part_file in PART_FILES:
        path = beam_dir / part_file
        if not path.exists():
            print(f"WARNING: {path} not found, skipping")
            continue

        table = pq.read_table(str(path))
        data = table.to_pydict()

        for i in range(len(data["conversation_id"])):
            cid = data["conversation_id"][i]
            seed = data["conversation_seed"][i]
            chat_data = data["chat"][i]
            pq_str = data["probing_questions"][i]

            # Parse probing questions
            probing = {}
            if pq_str:
                try:
                    probing = ast.literal_eval(pq_str)
                except Exception as exc:
                    logger.debug("Failed to parse probing questions for conversation (non-fatal): %s", exc)

            conversations.append({
                "conversation_id": cid,
                "category": seed.get("category", "unknown"),
                "subtopics": seed.get("subtopics", []),
                "chat": chat_data,
                "probing_questions": probing,
            })

    return conversations


def extract_conversation_content(chat_data: list) -> list[dict]:
    """Extract turn-level content from nested chat structure.

    Chat format: chat[0] → plan-N → batch → turns → turn_group → turn dict
    Returns list of (role, content) tuples in chronological order.
    """
    turns = []
    if not chat_data or not isinstance(chat_data, list):
        return turns

    for plan_data in chat_data[0].values():
        if not plan_data or not isinstance(plan_data, list):
            continue
        for batch in plan_data:
            for turn_group in batch.get("turns", []):
                if isinstance(turn_group, list):
                    for turn in turn_group:
                        if isinstance(turn, dict) and "content" in turn:
                            turns.append({
                                "role": turn.get("role", "unknown"),
                                "content": turn["content"],
                                "id": turn.get("id", ""),
                                "index": turn.get("index", 0),
                            })
    return turns


# ---------------------------------------------------------------------------
# Memory ingestion
# ---------------------------------------------------------------------------

def _get_db_connection(db_path: Path, tenant_id: str = "beam") -> sqlite3.Connection:
    """Get a SQLite connection with tenant_id function and views registered."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.create_function("tenant_id", 0, lambda: tenant_id)
    conn.execute(
        "CREATE TEMP VIEW IF NOT EXISTS tenant_memories AS "
        "SELECT * FROM memories WHERE tenant_id = tenant_id()"
    )
    return conn


def ingest_all_conversations(db_path: Path, conversations: list[dict]) -> dict[str, str]:
    """Ingest all BEAM conversations into DB with batched multi-indexing."""
    conn = _get_db_connection(db_path)
    session_map = {}
    CHUNK_SIZE = 2000
    batch_items = []

    try:
        for conv in conversations:
            turns = extract_conversation_content(conv["chat"])
            if not turns:
                continue

            chunks = []
            current_chunk = []
            current_len = 0

            for turn in turns:
                turn_text = f"[{turn['role'].upper()}] {turn['content']}\n"
                if current_len + len(turn_text) > CHUNK_SIZE and current_chunk:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = []
                    current_len = 0
                current_chunk.append(turn_text)
                current_len += len(turn_text)
            if current_chunk:
                chunks.append("\n".join(current_chunk))

            for idx, chunk in enumerate(chunks):
                memory_id = f"beam/conv{conv['conversation_id']}/chunk_{idx:04d}"
                timestamp = (datetime(2024, 1, 1) + timedelta(days=idx)).isoformat()
                chunk_with_meta = f"[Session Date: {timestamp[:10]}]\n{chunk}"
                tags_list = [f"conv_{conv['conversation_id']}", conv["category"]]
                conn.execute(
                    """INSERT OR REPLACE INTO memories
                       (id, content, source_file, tags, created_at, updated_at,
                        observed_at, pinned, importance, category, tenant_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, 3, 'sessions', 'beam')""",
                    (memory_id, chunk_with_meta, f"beam/conv{conv['conversation_id']}",
                     json.dumps(tags_list),
                     timestamp, timestamp, timestamp),
                )
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO memories_fts (id, content) VALUES (?, ?)",
                        (memory_id, chunk_with_meta),
                    )
                except Exception as exc:
                    logger.debug("FTS index insert failed for %s (non-fatal): %s", memory_id, exc)
                batch_items.append((memory_id, chunk_with_meta, "sessions", tags_list))
                session_map[f"conv{conv['conversation_id']}_chunk_{idx:04d}"] = memory_id

        populate_eval_memory_indexes_batch(conn, batch_items)
        conn.commit()
    finally:
        conn.close()

    # Build the persisted usearch vec index from the cached embeddings so the
    # first hybrid search doesn't silently re-encode every memory (4-6 min CPU
    # stall with no phase stats — the reason fresh benchmark runs looked hung).
    # Pure cache hits (content_hash matches what index_embeddings_batch wrote),
    # so this is seconds, not minutes. Must run AFTER embeddings are written.
    if batch_items:
        try:
            from rebuild_vec_index import rebuild_vec_index

            stats = rebuild_vec_index(str(db_path))
            logger.info(
                "vec index built: n_indexed=%s serialized=%sB elapsed=%.2fs",
                stats.get("n_indexed"),
                stats.get("serialized_bytes"),
                stats.get("elapsed_s", 0.0),
            )
        except Exception as exc:
            logger.warning("vec index build failed (non-fatal): %s", exc)

    return session_map


def ingest_conversation(db_path: Path, conv: dict) -> dict[str, str]:
    """Single conversation ingest compatibility wrapper."""
    return ingest_all_conversations(db_path, [conv])


def run_search(db_path: Path, query: str, limit: int = 20, light: bool = False) -> list[str]:
    """Run hybrid search and return list of memory IDs in rank order."""
    from search.orchestrator import search_memories

    result = search_memories(
        query=query,
        db_path=db_path,
        limit=max(limit, 20),
        hybrid=True,
        rerank=not light,
        mode="fts" if light else "hybrid",
        tenant_id="beam",
        category="sessions",
    )
    return [r["id"] for r in result.get("results", [])]


def score_answer(
    answer: str,
    expected: str,
    rubric: list[str] | None = None,
    compliance_indicators: list[str] | None = None,
    non_compliance_signs: list[str] | None = None,
    ability_type: str = "unknown",
) -> float:
    """Score predicted context against ground truth expectation and rubric."""
    from eval.bench.metrics import compute_text_metrics

    metrics = compute_text_metrics(
        prediction=answer,
        expected=expected,
        rubric=rubric,
        compliance_indicators=compliance_indicators,
    )
    score = metrics["overall_accuracy"]

    # Penalize non-compliance signs if present
    if non_compliance_signs and score > 0:
        ans_lower = answer.lower()
        for sign in non_compliance_signs:
            if sign.lower() in ans_lower:
                score = max(0.0, score - 0.5)
                break

    return score


def _write_live_progress(
    progress_file: Path,
    q_num: int,
    total_q: int,
    conv_idx: int,
    total_convs: int,
    cid: str,
    ability_type: str,
    question_text: str,
    score: float,
    latency_ms: float,
    results: list[dict],
    per_type: dict[str, list[float]],
) -> None:
    """Write atomic live progress JSON file for zero-latency monitoring."""
    tmp_file = progress_file.with_suffix(".tmp")
    running_acc = sum(r["score"] for r in results) / len(results) if results else 0.0
    type_acc = {
        t: sum(scores) / len(scores) if scores else 0.0
        for t, scores in per_type.items()
    }
    data = {
        "status": "running",
        "completed_questions": q_num,
        "total_questions": total_q,
        "percent_complete": round((q_num / max(1, total_q)) * 100, 1),
        "conversation_index": conv_idx + 1,
        "total_conversations": total_convs,
        "conversation_id": cid,
        "current_ability": ability_type,
        "current_question": question_text[:120],
        "last_question_score": score,
        "last_question_latency_ms": round(latency_ms, 1),
        "running_overall_accuracy": round(running_acc, 4),
        "running_per_type_accuracy": {k: round(v, 4) for k, v in type_acc.items()},
        "last_heartbeat_epoch": time.time(),
        "last_heartbeat_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_file, "w") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(progress_file)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def run_beam_real_eval(
    max_conversations: int = None,
    output_path: Path = None,
    use_cache_db: bool = True,
    rebuild: bool = False,
    light: bool = False,
) -> dict:
    """Run BEAM evaluation on real data."""
    from eval._fixtures import bootstrap_temp_db_clean

    print("Loading BEAM dataset...")
    conversations = load_beam_dataset()
    if not conversations:
        return {"error": "BEAM dataset not available"}

    if max_conversations:
        conversations = conversations[:max_conversations]
    print(f"Loaded {len(conversations)} conversations")

    # Count total questions
    total_q = sum(
        len(qs) for conv in conversations
        for qs in conv["probing_questions"].values()
        if isinstance(qs, list)
    )
    print(f"Total questions: {total_q}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_db_path = CACHE_DIR / "beam_real.db"
    cleanup_tmp = False

    if use_cache_db and not max_conversations and cache_db_path.exists() and not rebuild:
        db_path = cache_db_path
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        print(f"Using cached BEAM database: {db_path}")
        total_chunks = 0
    else:
        if use_cache_db and not max_conversations:
            db_path = cache_db_path
            if db_path.exists():
                try:
                    db_path.unlink()
                except OSError:
                    pass
        else:
            db_path = RESULTS_DIR / f"beam_real_{os.getpid()}.db"
            cleanup_tmp = True

        os.environ["MEMORY_DB_PATH"] = str(db_path)
        bootstrap_temp_db_clean(db_path)

        print("\nIngesting conversations...")
        session_map = ingest_all_conversations(db_path, conversations)
        total_chunks = len(session_map)
        print(f"Ingested {total_chunks} chunks")

    # Open persistent read connection for fetching note contents
    read_conn = sqlite3.connect(str(db_path), timeout=30.0)

    # Run evaluation
    print("\n" + "=" * 80, flush=True)
    print(f"STARTING BEAM EVALUATION ({total_q} questions across {len(conversations)} conversations)", flush=True)
    print("=" * 80, flush=True)
    results = []
    per_type = {}
    _q_num = 0
    progress_file = RESULTS_DIR / ".progress.json"

    try:
        for conv_idx, conv in enumerate(conversations):
            cid = conv["conversation_id"]
            category = conv["category"]
            probing = conv["probing_questions"]

            for ability_type, questions in probing.items():
                if not isinstance(questions, list):
                    continue

                for q in questions:
                    question_text = q.get("question", "")
                    if not question_text:
                        continue

                    _q_num += 1

                    if hasattr(memory_mcp, "_search_cache"):
                        memory_mcp._search_cache.clear()

                    expected = (
                        q.get("ideal_response")
                        or q.get("ideal_answer")
                        or q.get("ideal_summary")
                        or q.get("answer")
                        or q.get("expected_compliance")
                        or ""
                    )
                    rubric = q.get("rubric", [])
                    compliance_indicators = q.get("compliance_indicators", [])
                    non_compliance_signs = q.get("non_compliance_signs", [])
                    difficulty = q.get("difficulty", "unknown")

                    t0 = time.time()
                    retrieved = run_search(db_path, question_text, limit=20, light=light)
                    elapsed = (time.time() - t0) * 1000

                    retrieved_content = []
                    if retrieved:
                        for mid in retrieved[:10]:
                            row = read_conn.execute(
                                "SELECT content FROM memories WHERE id = ?", (mid,)
                            ).fetchone()
                            if row:
                                retrieved_content.append(row[0])

                    candidates_tuple = [(mid, content, "", "", "") for mid, content in zip(retrieved[:10], retrieved_content)]
                    combined_content = " ".join(retrieved_content)

                    try:
                        from search.phases.math_aggregator import extract_and_aggregate_quantities
                        math_sum = extract_and_aggregate_quantities(question_text, candidates_tuple)
                        if math_sum:
                            combined_content = f"{math_sum} " + combined_content
                    except Exception as exc:
                        logger.debug("Math aggregator phase failed (non-fatal): %s", exc)

                    try:
                        from search.phases.temporal_delta_solver import calculate_temporal_delta
                        temp_delta = calculate_temporal_delta(question_text, candidates_tuple)
                        if temp_delta:
                            combined_content = f"{temp_delta} " + combined_content
                    except Exception as exc:
                        logger.debug("Temporal delta solver phase failed (non-fatal): %s", exc)

                    try:
                        from search.phases.attribute_extractor import extract_entity_attribute
                        attr_val = extract_entity_attribute(question_text, candidates_tuple)
                        if attr_val:
                            combined_content = f"{attr_val} " + combined_content
                    except Exception as exc:
                        logger.debug("Attribute extractor phase failed (non-fatal): %s", exc)

                    score = score_answer(
                        combined_content,
                        expected,
                        rubric=rubric,
                        compliance_indicators=compliance_indicators,
                        non_compliance_signs=non_compliance_signs,
                        ability_type=ability_type,
                    )

                    results.append({
                        "conversation_id": cid,
                        "category": category,
                        "ability_type": ability_type,
                        "question": question_text,
                        "expected": expected[:200],
                        "difficulty": difficulty,
                        "score": score,
                        "latency_ms": round(elapsed, 1),
                        "num_retrieved": len(retrieved),
                    })

                    per_type.setdefault(ability_type, []).append(score)
                    running_acc = sum(r["score"] for r in results) / len(results)
                    status_icon = "✅ PASS" if score >= 0.6 else "❌ FAIL"
                    print(
                        f"  [Q {_q_num:2d}/{total_q:2d}] {status_icon} (Score: {score:.2f}, {elapsed:5.0f}ms) | Acc: {running_acc*100:5.1f}% | [{ability_type:<24}] Q: {question_text[:35]}...",
                        flush=True,
                    )

                    _write_live_progress(
                        progress_file=progress_file,
                        q_num=_q_num,
                        total_q=total_q,
                        conv_idx=conv_idx,
                        total_convs=len(conversations),
                        cid=cid,
                        ability_type=ability_type,
                        question_text=question_text,
                        score=score,
                        latency_ms=elapsed,
                        results=results,
                        per_type=per_type,
                    )
    finally:
        read_conn.close()

    overall_accuracy = sum(r["score"] for r in results) / len(results) if results else 0
    type_accuracy = {
        t: sum(scores) / len(scores) if scores else 0
        for t, scores in per_type.items()
    }

    print(f"\n{'='*60}")
    print(f"BEAM Real Data Results ({len(results)} questions)")
    print(f"{'='*60}")
    print(f"Overall accuracy: {overall_accuracy:.4f}")
    print("\nPer-type accuracy:")
    for t, acc in sorted(type_accuracy.items()):
        n = len(per_type[t])
        print(f"  {t}: {acc:.4f} ({n} questions)")

    out_file = output_path or RESULTS_PATH
    out_file.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "benchmark": "BEAM-Real",
        "version": "1.0",
        "n_conversations": len(conversations),
        "n_questions": len(results),
        "overall_accuracy": round(overall_accuracy, 4),
        "per_type_accuracy": {k: round(v, 4) for k, v in type_accuracy.items()},
        "per_type_counts": {k: len(v) for k, v in per_type.items()},
        "results": results,
    }

    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nResults saved to {out_file}")

    if cleanup_tmp and db_path.exists():
        try:
            db_path.unlink()
        except OSError:
            pass

    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="BEAM Real Data Benchmark")
    parser.add_argument("--max-conversations", type=int, default=None,
                        help="Max conversations to evaluate (default: all)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output JSON path")
    parser.add_argument("--rebuild", action="store_true",
                        help="Force rebuild DB")
    parser.add_argument("--no-cache", action="store_true",
                        help="Do not use cached DB")
    parser.add_argument("--light", action="store_true",
                        help="Run in light search mode (fast evaluation)")
    args = parser.parse_args()

    out_p = Path(args.output) if args.output else None
    run_beam_real_eval(
        max_conversations=args.max_conversations,
        output_path=out_p,
        use_cache_db=not args.no_cache,
        rebuild=args.rebuild,
        light=args.light,
    )
