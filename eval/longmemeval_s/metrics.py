"""
LongMemEval_S retrieval metrics.

All metrics operate on a single question's retrieved ranking:
  - retrieved_ids: list[str], doc_ids in ranked order (best first)
  - gold_ids:     set/list[str] of gold session ids

Per-question scores are macro-averaged across the eval set.
"""

from __future__ import annotations

import math
from typing import Iterable


def _top_k(retrieved_ids: list[str], k: int) -> list[str]:
    return retrieved_ids[:k]


def recall_all_at_k(retrieved_ids: list[str], gold_ids: Iterable[str], k: int) -> float:
    gold = set(gold_ids)
    if not gold:
        return 1.0
    top = _top_k(retrieved_ids, k)
    return 1.0 if all(g in top for g in gold) else 0.0


def recall_any_at_k(retrieved_ids: list[str], gold_ids: Iterable[str], k: int) -> float:
    gold = set(gold_ids)
    if not gold:
        return 1.0
    top = _top_k(retrieved_ids, k)
    return 1.0 if any(g in top for g in gold) else 0.0


def ndcg_any_at_k(retrieved_ids: list[str], gold_ids: Iterable[str], k: int) -> float:
    """
    NDCG with binary relevance: rel=1 if doc_id in gold_ids, else 0.

    IDCG uses min(|gold|, k) ideal docs, each with rel=1.
    If gold is empty, returns 1.0 (vacuously satisfied).
    """
    gold = set(gold_ids)
    if not gold:
        return 1.0
    top = _top_k(retrieved_ids, k)
    dcg = 0.0
    for i, doc in enumerate(top):
        rel = 1.0 if doc in gold else 0.0
        if rel > 0.0:
            dcg += rel / math.log2(i + 2)
    ideal = min(len(gold), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def compute_all_k(retrieved_ids: list[str], gold_ids: list[str], ks=(5, 10, 30, 50)) -> dict:
    return {
        f"recall_all@{k}": recall_all_at_k(retrieved_ids, gold_ids, k) for k in ks
    } | {
        f"recall_any@{k}": recall_any_at_k(retrieved_ids, gold_ids, k) for k in ks
    } | {
        f"ndcg_any@{k}": ndcg_any_at_k(retrieved_ids, gold_ids, k) for k in ks
    }
