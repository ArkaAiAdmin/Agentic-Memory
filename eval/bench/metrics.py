"""Standardized metric computation for retrieval, generation, and latency."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Sequence


def compute_retrieval_metrics(
    retrieved: Sequence[str],
    gold: set[str],
    ks: Sequence[int] = (1, 5, 10, 20, 30, 50),
) -> dict[str, float]:
    """Compute Recall@k, Precision@k, MRR, and NDCG@k for a ranked list of memory IDs."""
    if not gold:
        return {
            **{f"recall@{k}": 0.0 for k in ks},
            **{f"precision@{k}": 0.0 for k in ks},
            "mrr": 0.0,
            **{f"ndcg@{k}": 0.0 for k in ks},
        }

    scores: dict[str, float] = {}

    # MRR
    first_rank = 0
    for idx, item in enumerate(retrieved, start=1):
        if item in gold:
            first_rank = idx
            break
    scores["mrr"] = 1.0 / first_rank if first_rank > 0 else 0.0

    # Recall & Precision at k
    for k in ks:
        top_k = retrieved[:k]
        hits = len(set(top_k) & gold)
        scores[f"recall@{k}"] = hits / len(gold)
        scores[f"precision@{k}"] = hits / k if k > 0 else 0.0

        # NDCG@k
        dcg = 0.0
        for i, doc_id in enumerate(top_k):
            if doc_id in gold:
                dcg += 1.0 / math.log2(i + 2)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(min(k, len(gold))))
        scores[f"ndcg@{k}"] = (dcg / idcg) if idcg > 0 else 0.0

    return scores


def _normalize_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s\$\.,]", "", s)
    return s


def compute_token_f1(prediction: str, ground_truth: str) -> float:
    """Compute token-level multiset F1 score between prediction and ground truth."""
    pred_tokens = _normalize_text(prediction).split()
    gold_tokens = _normalize_text(ground_truth).split()

    if not pred_tokens or not gold_tokens:
        return 1.0 if pred_tokens == gold_tokens else 0.0

    pred_counts = Counter(pred_tokens)
    gold_counts = Counter(gold_tokens)
    common_counts = pred_counts & gold_counts
    overlap = sum(common_counts.values())

    if not overlap:
        return 0.0

    precision = overlap / len(pred_tokens)
    recall = overlap / len(gold_tokens)
    return 2 * (precision * recall) / (precision + recall)


def compute_text_metrics(
    prediction: str,
    expected: str,
    rubric: list[str] | None = None,
    compliance_indicators: list[str] | None = None,
) -> dict[str, float]:
    """Compute exact match, multiset token F1, substring match, and rubric compliance."""
    pred_norm = _normalize_text(prediction)
    exp_norm = _normalize_text(expected) if expected else ""

    em = 1.0 if (exp_norm and pred_norm == exp_norm) else 0.0
    sub = 1.0 if (exp_norm and exp_norm in pred_norm) else 0.0
    f1 = compute_token_f1(prediction, expected) if expected else 0.0

    # Rubric & compliance scoring
    rubric_score = 0.0
    if compliance_indicators:
        hits = 0
        for ind in compliance_indicators:
            ind_norm = _normalize_text(ind)
            if ind_norm in pred_norm:
                hits += 1
            else:
                words = [w for w in ind_norm.split() if len(w) > 3]
                if words and sum(1 for w in words if w in pred_norm) >= max(1, len(words) * 2 // 3):
                    hits += 1
        ratio = hits / len(compliance_indicators)
        rubric_score = 1.0 if ratio >= 0.5 else (ratio * 2)
    elif rubric:
        hits = sum(1 for r in rubric if _normalize_text(r) in pred_norm)
        rubric_score = hits / len(rubric)

    overall_accuracy = max(em, sub, 1.0 if f1 >= 0.6 else 0.0, rubric_score)

    return {
        "exact_match": em,
        "substring_match": sub,
        "token_f1": round(f1, 4),
        "rubric_score": round(rubric_score, 4),
        "overall_accuracy": round(overall_accuracy, 4),
    }


def compute_lafs(f1: float, latency_ms: float, tau: float = 2000.0) -> float:
    """Latency-Adjusted F1 Score (LAFS) from LongMemEval-V2.

    LAFS = F1 * exp(-latency_ms / tau).
    """
    decay = math.exp(-max(0.0, latency_ms) / max(1.0, tau))
    return round(f1 * decay, 4)


def calculate_latency_stats(latencies: list[float]) -> dict[str, float]:
    """Calculate mean, p50, p95, p99, and max latency from a list of latencies in ms."""
    if not latencies:
        return {"mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}

    s = sorted(latencies)
    n = len(s)

    p95_idx = min(n - 1, max(0, int(math.ceil(n * 0.95)) - 1))
    p99_idx = min(n - 1, max(0, int(math.ceil(n * 0.99)) - 1))

    return {
        "mean": round(sum(s) / n, 2),
        "p50": round(s[n // 2], 2),
        "p95": round(s[p95_idx], 2),
        "p99": round(s[p99_idx], 2),
        "max": round(s[-1], 2),
    }
