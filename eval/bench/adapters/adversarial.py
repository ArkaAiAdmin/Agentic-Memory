"""Adversarial & Multi-Hop Benchmark Adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseBenchmarkAdapter
from ..protocol import BenchmarkQuestion, BenchmarkSession


class AdversarialAdapter(BaseBenchmarkAdapter):
    """Adapter for state collision, 4-hop graph inference, and abstention stress tests."""

    name = "adversarial"
    version = "1.0"
    tenant_id = "adv_tenant"

    def load(self, limit: int | None = None) -> tuple[list[BenchmarkSession], list[BenchmarkQuestion]]:
        from eval.adversarial_memory_eval import generate_adversarial_dataset

        raw_dataset = generate_adversarial_dataset()
        sessions: list[BenchmarkSession] = []
        questions: list[BenchmarkQuestion] = []
        seen_sessions: set[str] = set()

        for g_idx, group in enumerate(raw_dataset):
            for s_idx, (s_date, s_text) in enumerate(group.get("sessions", [])):
                sid = f"adv_g{g_idx}_sess_{s_idx:03d}" if len(raw_dataset) > 1 else f"adv_sess_{s_idx:03d}"
                if sid not in seen_sessions:
                    seen_sessions.add(sid)
                    content_str = f"[Session Date: {s_date}]\n{s_text}"
                    sessions.append(
                        BenchmarkSession(
                            session_id=sid,
                            content=content_str,
                            timestamp=f"{s_date}T00:00:00Z",
                            category="sessions",
                        )
                    )

            for q in group.get("questions", []):
                questions.append(
                    BenchmarkQuestion(
                        question_id=q["id"],
                        query=q["question"],
                        expected_answer=q["expected"],
                        category=q["category"],
                        metadata=q.get("metadata", {}),
                    )
                )

        if limit is not None:
            questions = questions[:limit]

        return sessions, questions
