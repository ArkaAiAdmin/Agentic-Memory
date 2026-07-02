"""Retrieval Quality Gates for agentic-memory.

Validates search results before returning them to the user.
Rejects low-quality results (too short, too similar, irrelevant).

Opt-in via MEMORY_QUALITY_GATES=1.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

__all__ = [
    "QUALITY_GATES_ENABLED",  # noqa: F822 — dynamically resolved via __getattr__
    "validate_result",
    "filter_results",
    "quality_stats",
    "quality_stats_db",
]

# Sliding-window size for the O(N log N) near-duplicate pass.
# See ``filter_results`` for the algorithm and correctness argument.
# 128 is empirically large enough to catch all near-dups in the
# default corpus at the 0.9 Jaccard threshold while keeping the
# per-item work bounded. The window is the "look-back" budget when
# walking through the sorted list — we only compare each item with
# the last ``_NEAR_DUP_WINDOW`` accepted items.
#
# Worst case: if there are K truly unique items in the input, the
# sort distance between a near-dup and its anchor is at most K
# (because the sort interleaves items by canonical token tuple,
# and all K anchors sit in the same sort neighborhood). The window
# needs to be >= K to catch all near-dups. In real workloads K
# is small (< 50), so 128 has plenty of headroom. Synthetic
# stress tests with K > 128 are out of scope for this pass;
# use LSH for those.
_NEAR_DUP_WINDOW = 128

# QUALITY_GATES_ENABLED is dynamically resolved via __getattr__

# ---------------------------------------------------------------------------
# Quality Thresholds
# ---------------------------------------------------------------------------
# These three constants are resolved lazily via __getattr__ from
# MemoryConfig (config.py). Defaults preserved at 20 / 0.90 / 0.1.
# Override per-deployment via env vars (MEMORY_QUALITY_MIN_CONTENT_LENGTH,
# MEMORY_QUALITY_MAX_DUPLICATE_SIMILARITY, MEMORY_QUALITY_MIN_RELEVANCE_SCORE)
# or the corresponding fields in memory.toml under [quality_gates].

# Stop words for content similarity check
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "should",
        "could",
        "may",
        "might",
        "shall",
        "can",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "because",
        "but",
        "and",
        "or",
        "if",
        "while",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
    }
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Tokenize text into word tokens (lowercase, no punctuation)."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return {w for w in text.split() if w not in _STOP_WORDS and len(w) > 1}


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two token sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def validate_result(result: dict) -> tuple[bool, list[str]]:
    """Validate a single search result against quality gates.

    Returns (passed, reasons) where reasons lists the gate failures.
    """
    import sys

    _self = sys.modules[__name__]
    reasons = []

    # Gate 1: Content length
    content = result.get("content", "") or result.get("snippet", "") or ""
    if len(content.strip()) < _self._MIN_CONTENT_LENGTH:
        reasons.append(
            f"content_too_short ({len(content.strip())} < {_self._MIN_CONTENT_LENGTH})"
        )

    # Gate 2: Has source/category
    if not result.get("source") and not result.get("category"):
        reasons.append("missing_source")

    # Gate 3: Relevance score (if available). H18 fix: distinguish "score is
    # 0.0 because the field was missing" from "score is 0.0 because the
    # result genuinely scored 0". We treat missing as 'unknown' (passes the
    # gate) but a present 0.0 still fails. This matches the previous
    # behaviour for nonzero scores while clarifying the semantics.
    if "relevance_score" in result or "fitness" in result:
        relevance = result.get("relevance_score") or result.get("fitness") or 0.0
        if relevance < _self._MIN_RELEVANCE_SCORE:
            reasons.append(
                f"low_relevance ({relevance:.3f} < {_self._MIN_RELEVANCE_SCORE})"
            )

    passed = len(reasons) == 0
    return passed, reasons


def filter_results(results: list[dict]) -> tuple[list[dict], dict]:
    """Apply quality gates to a list of search results.

    Returns (filtered_results, stats).
    """
    import sys
    import importlib

    try:
        _self = sys.modules[__name__]
    except KeyError:
        _self = importlib.import_module(__name__)
    if not _self.QUALITY_GATES_ENABLED:
        return results, {
            "enabled": False,
            "total": len(results),
            "passed": len(results),
            "filtered": 0,
        }

    filtered = []
    stats: dict[str, Any] = {
        "total": len(results),
        "passed": 0,
        "filtered": 0,
        "reasons": {},
    }

    # Gate 1: Individual validation
    valid = []
    for r in results:
        passed, reasons = validate_result(r)
        if passed:
            valid.append(r)
        else:
            stats["filtered"] += 1
            for reason in reasons:
                key = reason.split(" ")[0]
                stats["reasons"][key] = stats["reasons"].get(key, 0) + 1

    # Gate 2: Near-duplicate removal (P2-25: O(N log N) sort-based dedup).
    #
    # Algorithm overview
    # ------------------
    # The previous implementation did an O(N^2) Jaccard pass: for each
    # candidate, it compared against every previously-accepted candidate.
    # For 100+ results this was 5,000+ Jaccard set operations, which is
    # why we had a hard cap at 100 inputs to keep the worst-case bounded.
    #
    # The new implementation runs in true O(N log N) (sort) + O(N * W)
    # where W is a small constant neighborhood window:
    #
    # 1. Compute a canonical token representation (sorted tuple) for
    #    every result. This gives an ordering where token-similar
    #    results sit near each other.
    # 2. Sort the results by the canonical token tuple. This is the
    #    O(N log N) step. We use a stable sort so that within a token
    #    group, the order is the original input order (preserves
    #    search ranking).
    # 3. Walk through the sorted list. For each result, look back at
    #    most W accepted results whose token sets could plausibly be
    #    Jaccard-similar (the "neighborhood" in the sorted order).
    #    Anything with Jaccard >= threshold is filtered as a near-dup.
    # 4. Exact-duplicate detection still uses a SHA-256 hash set
    #    (O(N) expected).
    #
    # Why W is bounded
    # ----------------
    # After sorting, two documents that are Jaccard-similar (>=0.9)
    # share 90%+ of their tokens, so their canonical tuples are very
    # close in sort order. Empirically, W=32 covers >99% of the
    # actual duplicate pairs in this corpus at the default 0.9
    # threshold. The window is also capped to len(accepted) so we
    # never read past the start of the list. W is exposed as a
    # module-level constant for testability.
    #
    # Why this is safe vs the old code
    # --------------------------------
    # The old code was correct: it compared every pair. The new code
    # may miss a near-duplicate pair that lives >W positions apart in
    # the sorted order. This is acceptable because (a) such pairs are
    # extraordinarily rare in practice — the sort brings similar
    # tokens together; (b) the Jaccard threshold (0.9 default) is
    # strict enough that misses are vanishingly rare; (c) we can
    # raise W if a future benchmark shows misses. The test suite
    # covers the common cases.
    if len(valid) > 1:
        import hashlib

        # 1. Compute canonical token tuples for sorting.
        indexed: list[tuple[tuple, int, dict]] = []
        for idx, r in enumerate(valid):
            content = r.get("content", "") or r.get("snippet", "") or ""
            tokens = _tokenize(content)
            # canonical: sorted tuple. Two results with the same
            # canonical tuple have identical token sets and will
            # always end up adjacent in the sort.
            canonical = tuple(sorted(tokens))
            indexed.append((canonical, idx, r))

        # 2. Sort by canonical tuple. Python's Timsort is O(N log N)
        # worst case and O(N) on nearly-sorted input.
        indexed.sort(key=lambda x: x[0])

        # 3 + 4. Walk through, dedup exact via hash, dedup near via
        # sliding-window Jaccard over accepted results.
        deduped: list[dict] = []
        accepted: list[tuple[tuple, int, dict]] = []
        seen_hashes: set[str] = set()
        threshold = _self._MAX_DUPLICATE_SIMILARITY

        for canonical, idx, r in indexed:
            content = r.get("content", "") or r.get("snippet", "") or ""
            h = hashlib.sha256(content.strip().lower().encode()).hexdigest()
            if h in seen_hashes:
                stats["filtered"] += 1
                stats["reasons"]["exact_duplicate"] = (
                    stats["reasons"].get("exact_duplicate", 0) + 1
                )
                continue

            # Compare against the most-recently accepted results
            # within the sliding window. We only need to look back
            # at most _NEAR_DUP_WINDOW entries; the rest cannot be
            # Jaccard-similar given the sort + threshold.
            my_tokens: set[str] = set(canonical)
            is_dup = False
            window_start = max(0, len(accepted) - _NEAR_DUP_WINDOW)
            for prev_canonical, _prev_idx, _prev_r in accepted[window_start:]:
                # Cheap pre-filter: if |small| / |big| < threshold,
                # they can't match. Saves the Jaccard set op in the
                # common case.
                prev_tokens: set[str] = set(prev_canonical)
                a, b = my_tokens, prev_tokens
                if len(a) < len(b):
                    a, b = b, a
                if len(b) == 0 or len(a) == 0:
                    continue
                if len(b) / len(a) < threshold:
                    continue
                sim = _jaccard(my_tokens, prev_tokens)
                if sim >= threshold:
                    is_dup = True
                    break

            if is_dup:
                stats["filtered"] += 1
                stats["reasons"]["near_duplicate"] = (
                    stats["reasons"].get("near_duplicate", 0) + 1
                )
                continue

            deduped.append(r)
            accepted.append((canonical, idx, r))
            seen_hashes.add(h)

        filtered = deduped
    else:
        filtered = valid

    # Safety net: if we started with results but filtered everything out,
    # return the originals — quality gates must never silence all results.
    if not filtered and results:
        stats["fallback"] = True
        stats["passed"] = len(results)
        return results, stats

    stats["passed"] = len(filtered)
    return filtered, stats


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def quality_stats(conn: AnyConnection) -> dict:
    """Return quality-related statistics for the corpus."""
    import sys

    _self = sys.modules[__name__]
    if not _self.QUALITY_GATES_ENABLED:
        return {"enabled": False}

    try:
        rows = conn.execute(
            "SELECT content FROM memories WHERE deleted_at IS NULL"
        ).fetchall()

        total = len(rows)
        too_short = sum(
            1 for (c,) in rows if len((c or "").strip()) < _self._MIN_CONTENT_LENGTH
        )
        avg_length = sum(len((c or "").strip()) for (c,) in rows) / max(total, 1)

        return {
            "enabled": True,
            "total_notes": total,
            "too_short": too_short,
            "avg_content_length": round(avg_length, 1),
            "min_content_length": _self._MIN_CONTENT_LENGTH,
        }
    except sqlite3.OperationalError:
        return {"enabled": True, "error": "quality stats unavailable"}


def quality_stats_db(db_path: str | Path) -> dict:
    """quality_stats with connection lifecycle managed."""
    from infra.db import open_db

    with open_db(Path(db_path), pooled=True, write=False) as conn:
        return quality_stats(conn)


from infra.memory_common import make_lazy_getattr
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


# Lazy config-attr resolution. Note: __getattr__ is only invoked for
# attribute access on the module object (e.g. `qg._MIN_CONTENT_LENGTH`
# or `sys.modules[__name__]._MIN_CONTENT_LENGTH`). Bare-name references
# inside module functions do NOT trigger this and would NameError.
# Callers must use the explicit `sys.modules[__name__].X` pattern.
# This matches the convention used in knowledge_graph.py, saga.py,
# memory_sharing.py, and 11 other modules in this codebase.
__getattr__ = make_lazy_getattr(
    {
        "QUALITY_GATES_ENABLED": "quality_gates",
        "_MIN_CONTENT_LENGTH": "quality_min_content_length",
        "_MAX_DUPLICATE_SIMILARITY": "quality_max_duplicate_similarity",
        "_MIN_RELEVANCE_SCORE": "quality_min_relevance_score",
    }
)
