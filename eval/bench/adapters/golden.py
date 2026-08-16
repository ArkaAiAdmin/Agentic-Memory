"""Golden retrieval quality dataset adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import BaseBenchmarkAdapter
from ..protocol import BenchmarkQuestion, BenchmarkSession

BENCH_ROOT = Path(__file__).resolve().parent.parent.parent
GOLDEN_DATASET_JSON = BENCH_ROOT / "real_memory_golden_v2.json"


class GoldenAdapter(BaseBenchmarkAdapter):
    """Adapter for hand-curated production retrieval quality regressions."""

    name = "golden"
    version = "3.0"
    tenant_id = "golden"

    def __init__(self, dataset_path: Path | None = None) -> None:
        self.dataset_path = dataset_path or GOLDEN_DATASET_JSON

    def load(self, limit: int | None = None) -> tuple[list[BenchmarkSession], list[BenchmarkQuestion]]:
        sessions: list[BenchmarkSession] = []
        questions: list[BenchmarkQuestion] = []

        if not self.dataset_path.exists():
            return sessions, questions

        with open(self.dataset_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        for s in data.get("memories", []):
            note_id = s.get("note_id", s.get("id", ""))
            sessions.append(
                BenchmarkSession(
                    session_id=note_id,
                    content=s["content"],
                    timestamp="2025-01-01T00:00:00Z",
                    category=s.get("category", "lessons"),
                    tags=s.get("tags", []),
                )
            )

        test_cases = data.get("test_cases", [])
        if limit is not None:
            test_cases = test_cases[:limit]

        for q_idx, q in enumerate(test_cases):
            expected = q.get("expected", [])
            gold_ids = set(expected) if isinstance(expected, list) else {expected}
            questions.append(
                BenchmarkQuestion(
                    question_id=f"golden_q{q_idx}",
                    query=q["query"],
                    expected_answer=", ".join(gold_ids),
                    gold_session_ids=gold_ids,
                    category=q.get("type", q.get("category", "general")),
                    metadata=q,
                )
            )

        return sessions, questions
