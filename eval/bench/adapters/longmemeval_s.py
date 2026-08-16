"""LongMemEval-S dataset adapter."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import BaseBenchmarkAdapter
from ..protocol import BenchmarkQuestion, BenchmarkSession

logger = logging.getLogger(__name__)

BENCH_ROOT = Path(__file__).resolve().parent.parent.parent
LONGMEM_S_JSON = BENCH_ROOT / "longmemeval_s" / "longmemeval_s_cleaned.json"
LONGMEM_SYNTH_JSONL = BENCH_ROOT / "datasets" / "longmemeval_s_synth.jsonl"


def _parse_haystack_date(date_str: str) -> str:
    """Parse '2023/05/20 (Sat) 02:21' into ISO-8601 datetime string."""
    parts = date_str.split("(")
    date_part = parts[0].strip()
    time_part = "00:00"
    if len(parts) > 1:
        m = re.search(r"(\d{2}:\d{2})", parts[1])
        if m:
            time_part = m.group(1)
    iso_date = date_part.replace("/", "-")
    return f"{iso_date}T{time_part}:00Z"


def _join_turns(session_turns: list[dict]) -> str:
    parts = []
    for turn in session_turns:
        c = turn.get("content") or ""
        if c:
            parts.append(c)
    return "\n".join(parts)


class LongMemEvalSAdapter(BaseBenchmarkAdapter):
    """Adapter for the LongMemEval-S cleaned dataset."""

    name = "longmemeval_s"
    version = "1.0"
    tenant_id = "longmemeval"

    def __init__(self, dataset_path: Path | None = None) -> None:
        self.dataset_path = dataset_path or (LONGMEM_S_JSON if LONGMEM_S_JSON.exists() else LONGMEM_SYNTH_JSONL)

    def load(self, limit: int | None = None) -> tuple[list[BenchmarkSession], list[BenchmarkQuestion]]:
        sessions: list[BenchmarkSession] = []
        questions: list[BenchmarkQuestion] = []
        seen_sessions: set[str] = set()

        if self.dataset_path.suffix == ".json" and self.dataset_path.exists():
            with open(self.dataset_path, "r", encoding="utf-8") as f:
                raw_entries = json.load(f)

            evaluable = [q for q in raw_entries if not q.get("question_id", "").endswith("_abs")]
            if limit is not None:
                evaluable = evaluable[:limit]

            for q in evaluable:
                qid = q["question_id"]
                s_ids = q.get("haystack_session_ids", [])
                s_turns = q.get("haystack_sessions", [])
                s_dates = q.get("haystack_dates", [])
                gold_ids = set(q.get("answer_session_ids", []))

                for i, (sid, sess) in enumerate(zip(s_ids, s_turns)):
                    if sid not in seen_sessions:
                        seen_sessions.add(sid)
                        content = _join_turns(sess)
                        if not content.strip():
                            continue
                        observed_at = (
                            _parse_haystack_date(s_dates[i])
                            if s_dates and i < len(s_dates)
                            else datetime.now(timezone.utc).isoformat()
                        )
                        sessions.append(
                            BenchmarkSession(
                                session_id=sid,
                                content=content,
                                timestamp=observed_at,
                                category="sessions",
                                tags=[sid],
                            )
                        )

                # Parse question date if available
                qdate = q.get("question_date")
                as_of_val = None
                if qdate:
                    try:
                        dt_str = _parse_haystack_date(qdate)
                        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                        as_of_val = dt.timestamp()
                    except Exception as exc:
                        logger.debug("Failed to parse question date (non-fatal): %s", exc)

                questions.append(
                    BenchmarkQuestion(
                        question_id=qid,
                        query=q["question"],
                        expected_answer=q.get("answer", ""),
                        gold_session_ids=gold_ids,
                        category=q.get("question_type", "general"),
                        as_of=as_of_val,
                        metadata={"n_sessions": len(s_ids)},
                    )
                )
        else:
            # Fallback to synth jsonl
            if LONGMEM_SYNTH_JSONL.exists():
                with open(LONGMEM_SYNTH_JSONL, "r", encoding="utf-8") as f:
                    for line_idx, line in enumerate(f):
                        if not line.strip():
                            continue
                        entry = json.loads(line)
                        qid = entry.get("question_id", f"synth_{line_idx}")
                        sess_texts = entry.get("sessions", [])
                        gold_ids = set()
                        for s_idx, stext in enumerate(sess_texts):
                            sid = f"{qid}_sess_{s_idx}"
                            gold_ids.add(sid)
                            if sid not in seen_sessions:
                                seen_sessions.add(sid)
                                sessions.append(
                                    BenchmarkSession(
                                        session_id=sid,
                                        content=stext,
                                        timestamp=datetime.now(timezone.utc).isoformat(),
                                        category="sessions",
                                    )
                                )
                        questions.append(
                            BenchmarkQuestion(
                                question_id=qid,
                                query=entry.get("query", entry.get("question", "")),
                                expected_answer=entry.get("answer", ""),
                                gold_session_ids=gold_ids,
                                category="synthetic",
                            )
                        )
                        if limit and len(questions) >= limit:
                            break

        return sessions, questions
