"""BEAM dataset adapter (synthetic multi-scale and real BEAM-10M)."""

from __future__ import annotations

import ast
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .base import BaseBenchmarkAdapter
from ..protocol import BenchmarkQuestion, BenchmarkSession

logger = logging.getLogger(__name__)

BENCH_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_BEAM_DIR = BENCH_ROOT / "datasets" / "beam"


class BEAMAdapter(BaseBenchmarkAdapter):
    """Adapter for BEAM benchmarks."""

    name = "beam"
    version = "1.0"
    tenant_id = "beam"

    def __init__(self, mode: str = "real", scale: str = "100K") -> None:
        self.mode = mode
        self.scale = scale

    def load(self, limit: int | None = None) -> tuple[list[BenchmarkSession], list[BenchmarkQuestion]]:
        if self.mode == "real":
            return self._load_real(limit)
        return self._load_synthetic(limit)

    def _load_real(self, limit: int | None = None) -> tuple[list[BenchmarkSession], list[BenchmarkQuestion]]:
        from eval.beam.run_beam_real import load_beam_dataset, extract_conversation_content

        conversations = load_beam_dataset()
        sessions: list[BenchmarkSession] = []
        questions: list[BenchmarkQuestion] = []
        CHUNK_SIZE = 2000

        for conv in conversations:
            cid = conv["conversation_id"]
            category = conv["category"]
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
                memory_id = f"beam/conv{cid}/chunk_{idx:04d}"
                timestamp = (datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=idx)).isoformat()
                chunk_with_meta = f"[Session Date: {timestamp[:10]}]\n{chunk}"
                sessions.append(
                    BenchmarkSession(
                        session_id=memory_id,
                        content=chunk_with_meta,
                        timestamp=timestamp,
                        category="sessions",
                        tags=[f"conv_{cid}", category],
                    )
                )

            probing = conv["probing_questions"]
            for ability_type, q_list in probing.items():
                if not isinstance(q_list, list):
                    continue
                for q_idx, q in enumerate(q_list):
                    q_text = q.get("question", "")
                    if not q_text:
                        continue
                    expected = (
                        q.get("ideal_response")
                        or q.get("ideal_answer")
                        or q.get("ideal_summary")
                        or q.get("answer")
                        or q.get("expected_compliance")
                        or ""
                    )
                    questions.append(
                        BenchmarkQuestion(
                            question_id=f"beam_{cid}_{ability_type}_{q_idx}",
                            query=q_text,
                            expected_answer=expected,
                            category=ability_type,
                            rubric=q.get("rubric"),
                            compliance_indicators=q.get("compliance_indicators"),
                            non_compliance_signs=q.get("non_compliance_signs"),
                            difficulty=q.get("difficulty", "normal"),
                            metadata={"conversation_id": cid, "category": category},
                        )
                    )

        if limit is not None:
            questions = questions[:limit]

        return sessions, questions

    def _load_synthetic(self, limit: int | None = None) -> tuple[list[BenchmarkSession], list[BenchmarkQuestion]]:
        # Load synthetic evolving facts from run_beam_eval
        from eval.beam.run_beam_eval import SCALES, generate_synthetic_sessions, PROBING_QUESTIONS

        scale_config = SCALES.get(self.scale, SCALES["100K"])
        raw_sessions, _ = generate_synthetic_sessions(scale_config)

        sessions: list[BenchmarkSession] = []
        for s in raw_sessions:
            sessions.append(
                BenchmarkSession(
                    session_id=f"beam_synth_{s['session_id']}",
                    content=s["content"],
                    timestamp=s["timestamp"],
                    category="sessions",
                    tags=s.get("tags", []),
                )
            )

        questions: list[BenchmarkQuestion] = []
        for q_idx, q in enumerate(PROBING_QUESTIONS):
            questions.append(
                BenchmarkQuestion(
                    question_id=f"beam_synth_q{q_idx}",
                    query=q["query"],
                    expected_answer=q["expected_current"],
                    category=q.get("category", "fact_tracking"),
                )
            )

        if limit is not None:
            questions = questions[:limit]

        return sessions, questions
