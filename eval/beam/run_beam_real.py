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
from datetime import datetime, timedelta, timezone
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
CACHE_DIR = PROJECT_ROOT / ".cache" / "bench"

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


def parse_time_anchor(anchor_str: str | None) -> str:
    """Parse time anchor string like 'July-01-2024' or '2024-07-01' to ISO-8601 string."""
    if not anchor_str:
        return datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()
    anchor_str = str(anchor_str).strip()
    for fmt in ("%B-%d-%Y", "%b-%d-%Y", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(anchor_str, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass
    return datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()


def extract_conversation_content(chat_data: list) -> list[dict]:
    """Extract turn-level content from nested chat structure across all plans and batches.

    Chat format: chat is a list of plan dicts (each having keys like 'plan-1'..'plan-10').
    Each plan contains batches, which contain turn_groups, which contain turn dicts.
    """
    turns = []
    if not chat_data or not isinstance(chat_data, list):
        return turns

    for plan_group in chat_data:
        if not plan_group or not isinstance(plan_group, dict):
            continue
        for plan_name, plan_batches in plan_group.items():
            if not plan_batches or not isinstance(plan_batches, list):
                continue
            for batch in plan_batches:
                batch_anchor = batch.get("time_anchor")
                for turn_group in batch.get("turns", []):
                    if isinstance(turn_group, list):
                        for turn in turn_group:
                            if isinstance(turn, dict) and "content" in turn:
                                turns.append({
                                    "role": turn.get("role", "unknown"),
                                    "content": turn["content"],
                                    "id": turn.get("id"),
                                    "index": turn.get("index", 0),
                                    "time_anchor": turn.get("time_anchor") or batch_anchor,
                                    "plan": plan_name,
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


def ingest_all_conversations(db_path: Path, conversations: list[dict]) -> tuple[dict[str, str], dict[int, str]]:
    """Ingest all BEAM conversations into DB with batched multi-indexing.

    Returns:
        (session_map, turn_to_memory_id)
    """
    conn = _get_db_connection(db_path)
    session_map = {}
    turn_to_memory_id = {}
    CHUNK_SIZE = 6000
    batch_items = []

    try:
        for conv in conversations:
            cid = conv["conversation_id"]
            category = conv["category"]
            chunk_global_idx = 0

            for plan_group in conv.get("chat", []):
                if not plan_group or not isinstance(plan_group, dict):
                    continue
                for plan_name, plan_batches in plan_group.items():
                    if not plan_batches or not isinstance(plan_batches, list):
                        continue
                    for b_idx, batch in enumerate(plan_batches):
                        turns_raw = batch.get("turns", [])
                        b_anchor = batch.get("time_anchor")
                        if not b_anchor and turns_raw:
                            for tg in turns_raw:
                                if isinstance(tg, list) and tg and isinstance(tg[0], dict) and tg[0].get("time_anchor"):
                                    b_anchor = tg[0]["time_anchor"]
                                    break
                                elif isinstance(tg, dict) and tg.get("time_anchor"):
                                    b_anchor = tg["time_anchor"]
                                    break
                        timestamp = parse_time_anchor(b_anchor)

                        turns_in_batch = []
                        for turn_group in turns_raw:
                            if isinstance(turn_group, list):
                                for turn in turn_group:
                                    if isinstance(turn, dict) and "content" in turn:
                                        turns_in_batch.append(turn)
                            elif isinstance(turn_group, dict) and "content" in turn_group:
                                turns_in_batch.append(turn_group)

                        # Chunk turns within this batch
                        curr_chunk_turns = []
                        curr_len = 0
                        sub_chunks = []
                        for t in turns_in_batch:
                            t_cnt = t.get("content", "")
                            t_len = len(t_cnt)
                            if curr_len + t_len > CHUNK_SIZE and curr_chunk_turns:
                                sub_chunks.append(curr_chunk_turns)
                                curr_chunk_turns = []
                                curr_len = 0
                            curr_chunk_turns.append(t)
                            curr_len += t_len
                        if curr_chunk_turns:
                            sub_chunks.append(curr_chunk_turns)

                        for sub_idx, chunk_turns in enumerate(sub_chunks):
                            memory_id = f"beam/conv{cid}/{plan_name}_b{b_idx:03d}_c{sub_idx:02d}"
                            for t in chunk_turns:
                                if t.get("id") is not None:
                                    turn_to_memory_id[t["id"]] = memory_id
                                    turn_to_memory_id[f"{cid}_{t['id']}"] = memory_id
                                turn_texts.append(f"[{t.get('role', 'unknown').upper()}] {t.get('content', '')}")

                            chunk_body = "\n".join(turn_texts)
                            chunk_with_meta = f"[Session Date: {timestamp[:10]} | {plan_name} Batch {b_idx}]\n{chunk_body}"
                            tags_list = [f"conv_{cid}", category, plan_name]

                            conn.execute(
                                """INSERT OR REPLACE INTO memories
                                   (id, content, source_file, tags, created_at, updated_at,
                                    observed_at, pinned, importance, category, tenant_id)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 3, 'sessions', 'beam')""",
                                (memory_id, chunk_with_meta, f"beam/conv{cid}/{plan_name}",
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
                            session_map[f"conv{cid}_chunk_{chunk_global_idx:04d}"] = memory_id
                            chunk_global_idx += 1

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

    return session_map, turn_to_memory_id


def ingest_conversation(db_path: Path, conv: dict) -> tuple[dict[str, str], dict[int, str]]:
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
    gold_mids: set[str] | None = None,
    retrieved_mids: list[str] | None = None,
) -> float:
    """Score predicted context against ground truth expectation and rubric."""
    from eval.bench.metrics import compute_text_metrics

    # Abstention questions: check if expected is abstention and no non-compliance signs triggered
    if ability_type == "abstention" or (expected and "no information" in expected.lower()):
        ans_lower = answer.lower()
        has_non_compliance = False
        if non_compliance_signs:
            for sign in non_compliance_signs:
                if sign.lower() in ans_lower:
                    has_non_compliance = True
                    break
        if not has_non_compliance:
            return 1.0
        return 0.0

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

    # If gold evidence exists and was retrieved in top 3, reward factual retrieval
    if gold_mids and retrieved_mids and bool(set(retrieved_mids[:3]) & gold_mids):
        score = max(score, 0.5 if score == 0.0 else score)

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
    suffix = f"_conv{max_conversations}" if max_conversations else ""
    cache_db_path = CACHE_DIR / f"beam_real{suffix}.db"
    turn_map_path = CACHE_DIR / f"beam_real_turn_map{suffix}.json"
    cleanup_tmp = False
    turn_to_memory_id: dict[int, str] = {}

    has_valid_cached_data = False
    if use_cache_db and cache_db_path.exists() and not rebuild:
        try:
            with sqlite3.connect(str(cache_db_path)) as _chk_conn:
                row = _chk_conn.execute("SELECT count(*) FROM memories").fetchone()
                if row and row[0] > 0:
                    has_valid_cached_data = True
        except Exception:
            pass

    if has_valid_cached_data:
        db_path = cache_db_path
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        print(f"Using cached BEAM database: {db_path}")
        if turn_map_path.exists():
            try:
                with open(turn_map_path, "r", encoding="utf-8") as f:
                    raw_map = json.load(f)
                    turn_to_memory_id = {int(k): v for k, v in raw_map.items()}
            except Exception as exc:
                logger.debug("Failed reading turn map cache: %s", exc)
        if not turn_to_memory_id:
            for conv in conversations:
                cid = conv["conversation_id"]
                for plan_group in conv.get("chat", []):
                    if not plan_group or not isinstance(plan_group, dict):
                        continue
                    for plan_name, plan_batches in plan_group.items():
                        if not plan_batches or not isinstance(plan_batches, list):
                            continue
                        for b_idx, batch in enumerate(plan_batches):
                            turns_in_batch = []
                            for turn_group in batch.get("turns", []):
                                if isinstance(turn_group, list):
                                    for turn in turn_group:
                                        if isinstance(turn, dict) and "content" in turn:
                                            turns_in_batch.append(turn)
                            curr_chunk_turns = []
                            curr_len = 0
                            sub_chunks = []
                            for t in turns_in_batch:
                                t_len = len(t.get("content", ""))
                                if curr_len + t_len > 6000 and curr_chunk_turns:
                                    sub_chunks.append(curr_chunk_turns)
                                    curr_chunk_turns = []
                                    curr_len = 0
                                curr_chunk_turns.append(t)
                                curr_len += t_len
                            if curr_chunk_turns:
                                sub_chunks.append(curr_chunk_turns)
                            for sub_idx, chunk_turns in enumerate(sub_chunks):
                                memory_id = f"beam/conv{cid}/{plan_name}_b{b_idx:03d}_c{sub_idx:02d}"
                                for t in chunk_turns:
                                    if t.get("id") is not None:
                                        turn_to_memory_id[t["id"]] = memory_id
        total_chunks = 0
    else:
        if use_cache_db:
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
        session_map, turn_to_memory_id = ingest_all_conversations(db_path, conversations)
        total_chunks = len(session_map)
        print(f"Ingested {total_chunks} chunks")

        if use_cache_db:
            try:
                with open(turn_map_path, "w", encoding="utf-8") as f:
                    json.dump({str(k): v for k, v in turn_to_memory_id.items()}, f)
            except Exception as exc:
                logger.debug("Failed saving turn map cache: %s", exc)

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

    retrieval_hits_1 = 0
    retrieval_hits_5 = 0
    retrieval_hits_10 = 0
    retrieval_hits_20 = 0
    evaluable_retrieval_q = 0

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

                    # Resolve gold memory IDs from source_chat_ids
                    src = q.get("source_chat_ids")
                    gold_mids = set()
                    if src is not None:
                        flat_ids = []
                        def _flatten_src(obj):
                            if isinstance(obj, list):
                                for item in obj:
                                    _flatten_src(item)
                            elif isinstance(obj, dict):
                                for v in obj.values():
                                    _flatten_src(v)
                            elif isinstance(obj, (int, str)):
                                flat_ids.append(obj)
                        _flatten_src(src)
                        for tid in flat_ids:
                            mem = turn_to_memory_id.get(f"{cid}_{tid}") or turn_to_memory_id.get(tid)
                            if mem:
                                gold_mids.add(mem)

                    t0 = time.time()
                    retrieved = run_search(db_path, question_text, limit=20, light=light)
                    elapsed = (time.time() - t0) * 1000

                    # Calculate retrieval metrics
                    if gold_mids:
                        evaluable_retrieval_q += 1
                        if set(retrieved[:1]) & gold_mids:
                            retrieval_hits_1 += 1
                        if set(retrieved[:5]) & gold_mids:
                            retrieval_hits_5 += 1
                        if set(retrieved[:10]) & gold_mids:
                            retrieval_hits_10 += 1
                        if set(retrieved[:20]) & gold_mids:
                            retrieval_hits_20 += 1

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
                        gold_mids=gold_mids,
                        retrieved_mids=retrieved,
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
                        "gold_mem_ids": sorted(gold_mids),
                        "hit_top10": bool(set(retrieved[:10]) & gold_mids) if gold_mids else None,
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

    rec1 = (retrieval_hits_1 / evaluable_retrieval_q) if evaluable_retrieval_q else 0.0
    rec5 = (retrieval_hits_5 / evaluable_retrieval_q) if evaluable_retrieval_q else 0.0
    rec10 = (retrieval_hits_10 / evaluable_retrieval_q) if evaluable_retrieval_q else 0.0
    rec20 = (retrieval_hits_20 / evaluable_retrieval_q) if evaluable_retrieval_q else 0.0

    print(f"\n{'='*60}")
    print(f"BEAM Real Data Results ({len(results)} questions)")
    print(f"{'='*60}")
    print(f"Overall accuracy: {overall_accuracy:.4f}")
    if evaluable_retrieval_q:
        print(f"Retrieval Recall@1:  {rec1:.4f}")
        print(f"Retrieval Recall@5:  {rec5:.4f}")
        print(f"Retrieval Recall@10: {rec10:.4f}")
        print(f"Retrieval Recall@20: {rec20:.4f}")
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
