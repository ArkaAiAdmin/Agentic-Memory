"""Auto-Consolidation for agentic-memory.

Detects near-duplicate notes, clusters related notes, and provides
merge suggestions. LLM-free: uses content similarity and tag overlap.

Opt-in via MEMORY_CONSOLIDATION=1.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import cast

__all__ = [
    "CONSOLIDATION_ENABLED",  # noqa: F822 — dynamically resolved via __getattr__
    "detect_duplicates",
    "cluster_related",
    "merge_suggestions",
    "consolidation_stats",
]

# CONSOLIDATION_ENABLED is dynamically resolved via __getattr__

# ---------------------------------------------------------------------------
# Similarity Helpers
# ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, normalize whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokenize(text: str) -> set[str]:
    """Split normalized text into word tokens."""
    return set(_normalize_text(text).split())


def _jaccard_similarity(a: set, b: set) -> float:
    """Jaccard index between two token sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _content_hash(text: str) -> str:
    """SHA-256 of normalized content for fast duplicate check."""
    import hashlib

    return hashlib.sha256(_normalize_text(text).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Near-Duplicate Detection
# ---------------------------------------------------------------------------


def detect_duplicates(
    conn: AnyConnection, threshold: float = 0.85, limit: int = 50
) -> list[dict]:
    """Find pairs of notes with Jaccard similarity above threshold.

    Returns list of {id_a, id_b, similarity, content_a_preview, content_b_preview}.
    """
    rows = conn.execute(
        "SELECT id, content FROM memories WHERE deleted_at IS NULL"
    ).fetchall()

    if len(rows) < 2:
        return []

    # Pre-compute tokens and hashes
    token_cache = {}
    hash_cache = {}
    for mid, content in rows:
        token_cache[mid] = _tokenize(content or "")
        hash_cache[mid] = _content_hash(content or "")

    # Exact hash matches first
    exact_dupes: dict[str, list[str]] = {}
    for mid, h in hash_cache.items():
        exact_dupes.setdefault(h, []).append(mid)

    duplicates = []
    seen_pairs = set()

    # Exact duplicates
    for h, mids in exact_dupes.items():
        if len(mids) > 1:
            for i in range(len(mids)):
                for j in range(i + 1, len(mids)):
                    pair = tuple(sorted([mids[i], mids[j]]))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        duplicates.append(
                            {
                                "id_a": pair[0],
                                "id_b": pair[1],
                                "similarity": 1.0,
                                "type": "exact",
                            }
                        )

    # Near-duplicates (only check if under limit)
    # Safety: cap at 500 notes to avoid O(N²) blowup on large corpora.
    if len(rows) <= min(limit * 2, 500):
        ids = list(token_cache.keys())
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pair = tuple(sorted([ids[i], ids[j]]))
                if pair in seen_pairs:
                    continue
                sim = _jaccard_similarity(token_cache[ids[i]], token_cache[ids[j]])
                if sim >= threshold:
                    seen_pairs.add(pair)
                    duplicates.append(
                        {
                            "id_a": pair[0],
                            "id_b": pair[1],
                            "similarity": round(sim, 4),
                            "type": "near",
                        }
                    )

    duplicates.sort(key=lambda x: cast(float, x["similarity"]), reverse=True)
    return duplicates[:limit]


# ---------------------------------------------------------------------------
# Related-Note Clustering
# ---------------------------------------------------------------------------


def cluster_related(
    conn: AnyConnection, tag_threshold: float = 0.3, limit: int = 50
) -> list[dict]:
    """Find clusters of related notes based on tag overlap.

    Returns list of clusters: {centroid, members: [id], shared_tags}.
    """
    rows = conn.execute(
        "SELECT id, tags FROM memories WHERE deleted_at IS NULL AND tags IS NOT NULL"
    ).fetchall()

    if not rows:
        return []

    # Parse tags for each note
    tag_sets = {}
    for mid, tags_str in rows:
        try:
            if tags_str:
                tags = json.loads(tags_str) if isinstance(tags_str, str) else tags_str
                tag_sets[mid] = set(tags) if isinstance(tags, list) else set()
            else:
                tag_sets[mid] = set()
        except (json.JSONDecodeError, TypeError):
            tag_sets[mid] = set()

    # Build clusters: group notes that share tags
    clusters = []
    assigned = set()

    for mid, tags in sorted(tag_sets.items()):
        if mid in assigned or not tags:
            continue

        # Find all notes that share at least one tag
        members = [mid]
        shared = set(tags)
        for other_mid, other_tags in tag_sets.items():
            if other_mid == mid or other_mid in assigned:
                continue
            overlap = len(tags & other_tags) / max(len(tags | other_tags), 1)
            if overlap >= tag_threshold:
                members.append(other_mid)
                shared &= other_tags
                assigned.add(other_mid)

        if len(members) > 1:
            assigned.update(members)
            clusters.append(
                {
                    "centroid": mid,
                    "members": members,
                    "shared_tags": list(shared),
                    "size": len(members),
                }
            )

    clusters.sort(key=lambda x: x["size"], reverse=True)
    return clusters[:limit]


# ---------------------------------------------------------------------------
# Merge Suggestions
# ---------------------------------------------------------------------------


def merge_suggestions(
    conn: AnyConnection, duplicate_threshold: float = 0.90, limit: int = 20
) -> list[dict]:
    """Suggest merges for near-duplicate notes.

    Returns list of {keep, merge, similarity, reason}.
    """
    dupes = detect_duplicates(conn, threshold=duplicate_threshold, limit=limit)

    suggestions = []
    for d in dupes:
        # Determine which to keep: higher access_count wins
        row_a = conn.execute(
            "SELECT access_count, pinned, tier FROM memories WHERE id = ?",
            (d["id_a"],),
        ).fetchone()
        row_b = conn.execute(
            "SELECT access_count, pinned, tier FROM memories WHERE id = ?",
            (d["id_b"],),
        ).fetchone()

        if not row_a or not row_b:
            continue

        acc_a, pinned_a, tier_a = row_a
        acc_b, pinned_b, tier_b = row_b

        # Pinned always wins
        if pinned_a and not pinned_b:
            keep, merge = d["id_a"], d["id_b"]
        elif pinned_b and not pinned_a:
            keep, merge = d["id_b"], d["id_a"]
        # Higher access wins
        elif (acc_a or 0) >= (acc_b or 0):
            keep, merge = d["id_a"], d["id_b"]
        else:
            keep, merge = d["id_b"], d["id_a"]

        suggestions.append(
            {
                "keep": keep,
                "merge": merge,
                "similarity": d["similarity"],
                "type": d["type"],
            }
        )

    return suggestions


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def consolidation_stats(conn: AnyConnection) -> dict:
    """Return consolidation-related statistics."""
    import sys

    if not sys.modules[__name__].CONSOLIDATION_ENABLED:
        return {"enabled": False}

    try:
        total_row = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()
        total = int(total_row[0]) if total_row is not None else 0

        # Content hash distribution
        rows = conn.execute(
            "SELECT content FROM memories WHERE deleted_at IS NULL"
        ).fetchall()

        hashes: dict[str, int] = {}
        for (content,) in rows:
            h = _content_hash(content or "")
            hashes.setdefault(h, 0)
            hashes[h] += 1

        exact_groups = sum(1 for cnt in hashes.values() if cnt > 1)
        exact_dupes = sum(cnt - 1 for cnt in hashes.values() if cnt > 1)

        # Tag distribution
        tag_rows = conn.execute(
            "SELECT tags FROM memories WHERE deleted_at IS NULL AND tags IS NOT NULL"
        ).fetchall()
        all_tags = set()
        for (tags_str,) in tag_rows:
            try:
                tags = json.loads(tags_str) if isinstance(tags_str, str) else []
                if isinstance(tags, list):
                    all_tags.update(tags)
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "enabled": True,
            "total_notes": total,
            "exact_duplicate_groups": exact_groups,
            "exact_duplicate_count": exact_dupes,
            "unique_tags": len(all_tags),
        }
    except sqlite3.OperationalError:
        return {"enabled": True, "error": "consolidation stats unavailable"}


from infra.memory_common import make_lazy_getattr
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


__getattr__ = make_lazy_getattr({"CONSOLIDATION_ENABLED": "consolidation"})
