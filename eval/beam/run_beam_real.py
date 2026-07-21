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
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

EVAL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_ROOT.parent.parent
RESULTS_DIR = EVAL_ROOT / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_PATH = RESULTS_DIR / "beam-real-run.json"

sys.path.insert(0, str(PROJECT_ROOT))

import memory_mcp  # noqa: E402
if not hasattr(memory_mcp, "safety_wiring"):
    setattr(memory_mcp, "safety_wiring", False)


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

BEAM_DATA_DIR = Path.home() / "Downloads"
PART_FILES = [
    "10M-00000-of-00002.parquet",
    "10M-00001-of-00002.parquet",
]


def load_beam_dataset() -> list[dict]:
    """Load BEAM conversations and probing questions from parquet files."""
    import pyarrow.parquet as pq

    conversations = []
    for part_file in PART_FILES:
        path = BEAM_DATA_DIR / part_file
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
                except Exception:
                    pass

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

def ingest_conversation(db_path: Path, conv: dict) -> dict[str, str]:
    """Ingest a BEAM conversation as memory notes.

    Splits into chunks of ~2000 chars to fit within search context windows.
    Returns mapping: chunk_id → memory_id.
    """
    turns = extract_conversation_content(conv["chat"])
    if not turns:
        return {}

    # Join all turns into a single conversation text
    full_text = "\n".join(
        f"[{t['role'].upper()}] {t['content']}" for t in turns
    )

    # Split into chunks of ~2000 chars (respecting turn boundaries)
    CHUNK_SIZE = 2000
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

    # Ingest chunks as memory notes
    conn = sqlite3.connect(str(db_path))
    session_map = {}
    try:
        for idx, chunk in enumerate(chunks):
            memory_id = f"beam/conv{conv['conversation_id']}/chunk_{idx:04d}"
            timestamp = (datetime(2024, 1, 1) + timedelta(days=idx)).isoformat()
            conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, content, source_file, tags, created_at, updated_at,
                    observed_at, pinned, importance, category, tenant_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 3, 'sessions', 'beam')""",
                (memory_id, chunk, f"beam/conv{conv['conversation_id']}",
                 json.dumps([f"conv_{conv['conversation_id']}", conv["category"]]),
                 timestamp, timestamp, timestamp),
            )
            # FTS index
            rowid = conn.execute(
                "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if rowid:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO memories_fts (rowid, content) VALUES (?, ?)",
                        (rowid[0], chunk),
                    )
                except Exception:
                    pass
            session_map[f"chunk_{idx:04d}"] = memory_id
        conn.commit()
    finally:
        conn.close()

    return session_map


# ---------------------------------------------------------------------------
# Search and scoring
# ---------------------------------------------------------------------------

def run_search(db_path: Path, query: str, limit: int = 20) -> list[str]:
    """Run hybrid search and return list of memory IDs in rank order."""
    from search.orchestrator import search_memories

    result = search_memories(
        db_path,
        query,
        limit=limit,
        include_global=True,
        rerank=True,
        include_facts=False,
        safety_wiring=False,
        tenant_id="beam",
        category="sessions",
    )
    return [r["id"] for r in result.get("results", [])]


def score_answer(answer_text: str, expected: str, rubric: list[str] = None) -> float:
    """Score an answer using token overlap and rubric matching."""
    if not expected:
        return 0.0

    answer_lower = answer_text.lower().strip()
    expected_lower = expected.lower().strip()

    # Exact match
    if answer_lower == expected_lower:
        return 1.0

    # Substring match
    if expected_lower in answer_lower:
        return 1.0

    # Token overlap
    answer_tokens = set(answer_lower.split())
    expected_tokens = set(expected_lower.split())
    overlap = answer_tokens & expected_tokens
    if expected_tokens and len(overlap) / len(expected_tokens) >= 0.6:
        return 1.0

    # Rubric-based scoring (if available)
    if rubric:
        rubric_hits = sum(1 for r in rubric if r.lower() in answer_lower)
        if rubric_hits >= len(rubric) * 0.5:
            return 1.0

    # Partial match
    if expected_tokens:
        return len(overlap) / len(expected_tokens)
    return 0.0


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def run_beam_real_eval(max_conversations: int = None) -> dict:
    """Run BEAM evaluation on real data."""
    from eval._fixtures import bootstrap_temp_db_clean

    print("Loading BEAM dataset...")
    conversations = load_beam_dataset()
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

    # Set up DB
    db_path = RESULTS_DIR / "beam_real.db"
    if db_path.exists():
        db_path.unlink()
    bootstrap_temp_db_clean(db_path)

    # Ingest all conversations
    print("\nIngesting conversations...")
    total_chunks = 0
    for conv in conversations:
        session_map = ingest_conversation(db_path, conv)
        total_chunks += len(session_map)
    print(f"Ingested {total_chunks} chunks")

    # Run evaluation
    print("\nRunning evaluation...")
    results = []
    per_type = {}

    for conv in conversations:
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

                # Get expected answer (varies by type)
                expected = (
                    q.get("ideal_response")
                    or q.get("ideal_answer")
                    or q.get("answer")
                    or q.get("expected_compliance")
                    or ""
                )
                rubric = q.get("rubric", [])
                difficulty = q.get("difficulty", "unknown")

                # Search
                t0 = time.time()
                retrieved = run_search(db_path, question_text, limit=20)
                elapsed = (time.time() - t0) * 1000

                # Get content of retrieved sessions
                retrieved_content = []
                if retrieved:
                    conn = sqlite3.connect(str(db_path))
                    for mid in retrieved[:5]:
                        row = conn.execute(
                            "SELECT content FROM memories WHERE id = ?", (mid,)
                        ).fetchone()
                        if row:
                            retrieved_content.append(row[0])
                    conn.close()

                # Score against expected answer
                combined_content = " ".join(retrieved_content)
                score = score_answer(combined_content, expected, rubric)

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

    # Calculate metrics
    overall_accuracy = sum(r["score"] for r in results) / len(results) if results else 0
    type_accuracy = {
        t: sum(scores) / len(scores) if scores else 0
        for t, scores in per_type.items()
    }

    # Report
    print(f"\n{'='*60}")
    print(f"BEAM Real Data Results ({len(results)} questions)")
    print(f"{'='*60}")
    print(f"Overall accuracy: {overall_accuracy:.4f}")
    print(f"\nPer-type accuracy:")
    for t, acc in sorted(type_accuracy.items()):
        n = len(per_type[t])
        print(f"  {t}: {acc:.4f} ({n} questions)")

    # Save
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

    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nResults saved to {RESULTS_PATH}")

    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="BEAM Real Data Benchmark")
    parser.add_argument("--max-conversations", type=int, default=None,
                        help="Max conversations to evaluate (default: all)")
    args = parser.parse_args()
    run_beam_real_eval(max_conversations=args.max_conversations)
