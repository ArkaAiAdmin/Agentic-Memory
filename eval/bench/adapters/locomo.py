"""LoCoMo dataset adapter."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .base import BaseBenchmarkAdapter
from ..protocol import BenchmarkQuestion, BenchmarkSession

BENCH_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_DIR = BENCH_ROOT / "datasets"
LOCOMO_JSON = DATASET_DIR / "locomo10.json"
DOWNLOAD_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"

CATEGORY_MAP = {
    1: "single-hop",
    2: "multi-hop",
    3: "temporal",
    4: "open-domain",
    5: "adversarial",
}


class LoCoMoAdapter(BaseBenchmarkAdapter):
    """Adapter for the Snap Research LoCoMo benchmark."""

    name = "locomo"
    version = "1.0"
    tenant_id = "locomo"

    def __init__(self, dataset_path: Path | None = None) -> None:
        self.dataset_path = dataset_path or LOCOMO_JSON

    def ensure_dataset(self) -> list[dict[str, Any]]:
        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        if not self.dataset_path.exists():
            print(f"Downloading LoCoMo dataset to {self.dataset_path} ...")
            urllib.request.urlretrieve(DOWNLOAD_URL, str(self.dataset_path))
            print("  done.")
        with open(self.dataset_path, encoding="utf-8") as f:
            return json.load(f)

    def load(self, limit: int | None = None) -> tuple[list[BenchmarkSession], list[BenchmarkQuestion]]:
        raw_data = self.ensure_dataset()
        sessions: list[BenchmarkSession] = []
        questions: list[BenchmarkQuestion] = []

        base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)

        for conv_idx, sample in enumerate(raw_data):
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

            for sess_idx, sk in enumerate(session_keys):
                turns = conv[sk]
                if not isinstance(turns, list):
                    continue
                sess_id = f"locomo/{sample_id}/{sk}"
                lines = [f"[Conversation: {sample_id}, Session: {sk}]"]
                for turn in turns:
                    speaker = turn.get("speaker", "unknown")
                    text = turn.get("text", "")
                    dia_id = turn.get("dia_id", "")
                    lines.append(f"({dia_id}) {speaker}: {text}")
                content = "\n".join(lines)
                sess_time = (base_time + timedelta(days=conv_idx * 30 + sess_idx)).isoformat()

                sessions.append(
                    BenchmarkSession(
                        session_id=sess_id,
                        content=content,
                        timestamp=sess_time,
                        category="sessions",
                        tags=[sample_id, sk],
                        metadata={"sample_id": sample_id, "session_key": sk},
                    )
                )

            for q_idx, qa in enumerate(sample.get("qa", [])):
                cat_num = qa.get("category", 0)
                cat_name = CATEGORY_MAP.get(cat_num, f"cat-{cat_num}")
                evidence = qa.get("evidence", [])
                gold_ids = set()
                for dia_id in evidence:
                    sess_num = dia_id.split(":")[0].lstrip("D")
                    gold_ids.add(f"locomo/{sample_id}/session_{sess_num}")

                questions.append(
                    BenchmarkQuestion(
                        question_id=f"locomo_{sample_id}_q{q_idx}",
                        query=qa["question"],
                        expected_answer=qa.get("answer", ""),
                        gold_session_ids=gold_ids,
                        category=cat_name,
                        metadata={"sample_id": sample_id, "evidence": evidence},
                    )
                )

        if limit is not None:
            questions = questions[:limit]

        return sessions, questions
