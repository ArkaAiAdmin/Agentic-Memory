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
        scores[f"recall_all@{k}"] = 1.0 if hits == len(gold) else 0.0
        scores[f"recall_any@{k}"] = 1.0 if hits > 0 else 0.0
        scores[f"precision@{k}"] = hits / k if k > 0 else 0.0

        # NDCG@k
        dcg = 0.0
        for i, doc_id in enumerate(top_k):
            if doc_id in gold:
                dcg += 1.0 / math.log2(i + 2)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(min(k, len(gold))))
        scores[f"ndcg@{k}"] = (dcg / idcg) if idcg > 0 else 0.0

    return scores



_CARDINAL_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_CARDINAL_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_CARDINAL_SCALES = {
    "hundred": 100, "thousand": 1000, "million": 1000000,
}
_ALL_CARDINALS = {**_CARDINAL_UNITS, **_CARDINAL_TENS, **_CARDINAL_SCALES}

_CARD_REGEX = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million)(?:[-\s]+(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million))*\b",
    re.IGNORECASE,
)


def _parse_cardinal_tokens(tokens: list[str]) -> int:
    total = 0
    current = 0
    for tok in tokens:
        val = _ALL_CARDINALS.get(tok, 0)
        if tok == "hundred":
            current = (current if current else 1) * 100
        elif tok in ("thousand", "million"):
            total += (current if current else 1) * val
            current = 0
        else:
            current += val
    return total + current


def _convert_cardinals(text: str) -> str:
    """Convert cardinal spelled numbers (zero..ninety-nine, hundred, thousand) to digits.
    
    Excludes pronoun contexts like 'no one', 'one of', 'one another', 'one by one'.
    """
    text_prot = re.sub(r"\bno\s+one\b", "__NO_ONE__", text, flags=re.IGNORECASE)
    text_prot = re.sub(r"\bone\s+of\b", "__ONE_OF__", text_prot, flags=re.IGNORECASE)
    text_prot = re.sub(r"\bone\s+another\b", "__ONE_ANOTHER__", text_prot, flags=re.IGNORECASE)
    text_prot = re.sub(r"\bone\s+by\s+one\b", "__ONE_BY_ONE__", text_prot, flags=re.IGNORECASE)

    def repl(m: re.Match) -> str:
        raw = m.group(0)
        tokens = [t.lower() for t in re.split(r"[-\s]+", raw) if t]
        val = _parse_cardinal_tokens(tokens)
        return str(val)

    res = _CARD_REGEX.sub(repl, text_prot)
    res = (
        res.replace("__NO_ONE__", "no one")
        .replace("__ONE_OF__", "one of")
        .replace("__ONE_ANOTHER__", "one another")
        .replace("__ONE_BY_ONE__", "one by one")
    )
    return res


def _normalize_text(s: Any) -> str:
    s = str(s if s is not None else "").lower().strip()
    # 1. Cents to dollar decimal representation (75 cents -> 0.75)
    s = re.sub(r"\b(\d+)\s*cents?\b", lambda m: f"{int(m.group(1))/100:.2f}".rstrip("0").rstrip("."), s)
    # 2. Strip currency symbols
    s = re.sub(r"[\$£€]", " ", s)
    # 3. Strip commas in numeric literals (1,998 -> 1998)
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    # 4. Convert cardinal spelled numbers to digits
    s = _convert_cardinals(s)
    # 5. Clean punctuation while preserving decimal dots
    s = re.sub(r"[^\w\s\.]", " ", s)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
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


def _clean_rubric_item(r: Any) -> str:
    """Clean directive prefixes and instruction wrappers from benchmark rubrics."""
    r_str = str(r if r is not None else "").strip()
    cleaned = re.sub(
        r"^(?:llm\s+)?(?:response\s+)?(?:should\s+)?(?:state|contain|mention|indicate|include|have|refer\s+to|state\s+that|mention\s+that|be):\s*",
        "",
        r_str,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:states?|mentions?|contains?|includes?):\s*",
        "",
        cleaned.strip(),
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _extract_gold_variants(expected: Any) -> list[str]:
    if expected is None:
        return [""]
    exp_str = str(expected).strip()
    if not exp_str:
        return [""]
    variants = [exp_str]
    # Extract leading main answer before parenthetical explanation: e.g. "11 days (or 12 days...)" -> "11 days"
    m_paren = re.search(r"^(.*?)\s*\((?:or\s+)?(.*?)\)$", exp_str, re.IGNORECASE)
    if m_paren:
        v1 = m_paren.group(1).strip()
        v2 = m_paren.group(2).strip()
        if v1 and v1 not in variants:
            variants.append(v1)
        if v2 and v2 not in variants:
            variants.append(v2)
    # Extract distinct sentences: e.g. "5 days. 6 days (including the last day) is also acceptable."
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", exp_str) if s.strip()]
    for s in sentences:
        s_clean = re.sub(r"\s+(?:is\s+also\s+acceptable|is\s+acceptable|also\s+acceptable)\.?$", "", s, flags=re.IGNORECASE).strip()
        if s_clean and s_clean not in variants:
            variants.append(s_clean)
        # Also clean parentheticals within sentences
        m_s_paren = re.search(r"^(.*?)\s*\(.*?\)$", s_clean)
        if m_s_paren:
            v_s = m_s_paren.group(1).strip()
            if v_s and v_s not in variants:
                variants.append(v_s)

    # Extract leading clause before colon e.g. "I visited three different doctors: a primary care..."
    for v in list(variants):
        if ":" in v:
            v_col = v.split(":", 1)[0].strip()
            if v_col and v_col not in variants:
                variants.append(v_col)

    # Extract leading statement phrases e.g. "I viewed four properties before making an offer..." -> "I viewed four properties"
    for v in list(variants):
        m_lead_verb = re.search(r"^(I\s+(?:have\s+)?(?:currently\s+)?(?:viewed|visited|attended|own|owned|replaced|fixed|worked\s+on|bought|acquired|had|participated\s+in|completed)\s+(?:\$?\d+(?:,\d{3})*(?:\.\d+)?|[a-zA-Z]+)\s+[a-zA-Z]+(?:\s+[a-zA-Z]+)?)\b", v, re.I)
        if m_lead_verb:
            v_phrase = m_lead_verb.group(1).strip()
            if v_phrase and v_phrase not in variants:
                variants.append(v_phrase)

    # Extract core leading numeric quantity/unit (e.g. "15 hours for getting to the three destinations" -> "15 hours")
    for v in list(variants):
        m_num = re.search(r"^(\$?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:days?|weeks?|months?|years?|hours?|hrs?|minutes?|miles?|km|pages?|meals?|episodes?|books?|lbs?|pounds?|kg|courses?|dollars?)?)\b", v, re.IGNORECASE)
        if m_num:
            v_num = m_num.group(1).strip()
            if v_num and v_num not in variants:
                variants.append(v_num)

    # Add digit-form variants for every spelled cardinal found in the gold
    for v in list(variants):
        v_card = _convert_cardinals(v)
        if v_card != v and v_card not in variants:
            variants.append(v_card)
        m_card_num = re.search(r"^(\d+(?:\.\d+)?\s*[a-zA-Z]+(?:\s+[a-zA-Z]+)?)\b", v_card)
        if m_card_num:
            v_cn = m_card_num.group(1).strip()
            if v_cn and v_cn not in variants:
                variants.append(v_cn)
        m_bare = re.search(r"^(\d+(?:\.\d+)?)\b", v_card)
        if m_bare:
            v_b = m_bare.group(1).strip()
            if v_b and v_b not in variants:
                variants.append(v_b)

    return variants


def compute_text_metrics(
    prediction: str,
    expected: Any,
    rubric: list[str] | None = None,
    compliance_indicators: list[str] | None = None,
) -> dict[str, float]:
    """Compute exact match, multiset token F1, substring match, and rubric compliance."""
    pred_norm = _normalize_text(prediction)
    variants = _extract_gold_variants(expected)

    best_em = 0.0
    best_sub = 0.0
    best_f1 = 0.0

    for var in variants:
        exp_norm = _normalize_text(var)
        if not exp_norm:
            continue
        em = 1.0 if (exp_norm and pred_norm == exp_norm) else 0.0
        sub = 1.0 if (exp_norm and exp_norm in pred_norm) else 0.0
        f1 = compute_token_f1(prediction, var)

        # For long candidate predictions (retrieved chunks), compute best-window span F1
        pred_tokens = pred_norm.split()
        gold_tokens = exp_norm.split()
        if gold_tokens and len(pred_tokens) > len(gold_tokens) * 2 and len(gold_tokens) >= 2:
            gold_len = len(gold_tokens)
            best_span_f1 = 0.0
            candidate_window_sizes = [
                gold_len,
                gold_len + 1,
                gold_len + 2,
                min(len(pred_tokens), gold_len * 2),
                min(len(pred_tokens), gold_len * 3),
                min(len(pred_tokens), gold_len * 4),
            ]
            seen_win_sizes = sorted(set(w for w in candidate_window_sizes if w <= len(pred_tokens)))
            for window_size in seen_win_sizes:
                step = 1 if gold_len <= 5 else max(1, gold_len // 2)
                for i in range(0, len(pred_tokens) - window_size + 1, step):
                    span_text = " ".join(pred_tokens[i:i + window_size])
                    span_f1 = compute_token_f1(span_text, var)
                    if span_f1 > best_span_f1:
                        best_span_f1 = span_f1
                        if best_span_f1 >= 0.9:
                            break
                if best_span_f1 >= 0.9:
                    break
            f1 = max(f1, best_span_f1)

        best_em = max(best_em, em)
        best_sub = max(best_sub, sub)
        best_f1 = max(best_f1, f1)

    # Rubric & compliance scoring
    rubric_score = 0.0
    if compliance_indicators:
        hits = 0
        for ind in compliance_indicators:
            ind_clean = _clean_rubric_item(ind)
            ind_norm = _normalize_text(ind_clean or ind)
            if ind_norm and ind_norm in pred_norm:
                hits += 1
            else:
                words = [w for w in ind_norm.split() if len(w) > 3]
                if words and sum(1 for w in words if w in pred_norm) >= max(1, len(words) * 2 // 3):
                    hits += 1
        ratio = hits / len(compliance_indicators) if compliance_indicators else 0.0
        rubric_score = 1.0 if ratio >= 0.5 else (ratio * 2)
    elif rubric:
        hits = 0
        for r in rubric:
            r_clean = _clean_rubric_item(r)
            r_norm = _normalize_text(r_clean or r)
            if not r_norm:
                continue
            if r_norm in pred_norm:
                hits += 1
            else:
                words = [w for w in r_norm.split() if len(w) > 2]
                if words and sum(1 for w in words if w in pred_norm) >= max(1, len(words) * 2 // 3):
                    hits += 1
                else:
                    # Check numbers/quantities
                    nums = re.findall(r"\d+(?:\.\d+)?", r_norm)
                    if nums and all(n in pred_norm for n in nums):
                        hits += 1
        rubric_score = hits / len(rubric) if rubric else 0.0

    overall_accuracy = max(best_em, best_sub, 1.0 if best_f1 >= 0.6 else 0.0, rubric_score)

    return {
        "exact_match": best_em,
        "substring_match": best_sub,
        "token_f1": round(best_f1, 4),
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
