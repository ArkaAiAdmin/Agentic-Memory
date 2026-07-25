"""Shared version-vector primitives for the CRDT subsystem.

This is a zero-dependency leaf module — it imports nothing from the
agentic-memory codebase. All CRDT modules (crdt_field, crdt_merge,
kg_crdt) import VV logic from here to eliminate duplication and
ensure consistent semantics (especially around empty-VV handling).

Version vectors are ``dict[str, int]`` mapping agent_id → lamport clock.
The empty dict ``{}`` represents "no causal history" (a fresh entity
that has never been written to).

Correctness properties:
    * ``vv_dominates(a, b)`` is a strict partial order (irreflexive,
      transitive, antisymmetric).
    * ``vv_concurrent(a, b)`` is symmetric.
    * ``vv_join`` is commutative, associative, and idempotent (lattice join).
    * ``merge_vectors`` is pointwise-max (commutative, associative, idempotent).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "vv_dominates",
    "vv_concurrent",
    "vv_join",
    "parse_version_vector",
    "merge_vectors",
]


def vv_dominates(a: dict[str, int], b: dict[str, int]) -> bool:
    """Return True if version vector ``a`` causally dominates ``b``.

    a dominates b iff for every agent x, a[x] >= b[x], AND there
    exists at least one agent where a[x] > b[x].

    Empty-VV semantics:
        * An empty VV can never dominate anything (no causal history).
        * Any non-empty VV dominates an empty one (fresh/unknown state
          is causally before any write).
        * Two empty VVs are equal, not concurrent — neither dominates.
    """
    if not a:
        return False  # empty can never dominate
    if not b:
        return True  # any non-empty VV dominates empty (fresh/unknown state)
    keys = set(a) | set(b)
    at_least_one_greater = False
    for k in keys:
        av = a.get(k, 0)
        bv = b.get(k, 0)
        if av < bv:
            return False
        if av > bv:
            at_least_one_greater = True
    return at_least_one_greater


def vv_concurrent(a: dict[str, int], b: dict[str, int]) -> bool:
    """Return True if ``a`` and ``b`` are concurrent (neither dominates).

    Two empty VVs are equal (same state), not concurrent.
    Two equal non-empty VVs are also equal, not concurrent.
    One empty + one non-empty: the non-empty dominates, so not concurrent.
    """
    if not a and not b:
        return False  # both empty = same state
    if a == b:
        return False  # identical VVs = same causal history (equal, not concurrent)
    return not vv_dominates(a, b) and not vv_dominates(b, a)


def vv_join(*vvs: dict[str, int]) -> dict[str, int]:
    """Compute the join (element-wise max) of multiple version vectors.

    The join represents the causal history of all inputs — any replica
    that has seen any of the inputs will have at least this state.

    Commutative, associative, idempotent (lattice join).
    """
    result: dict[str, int] = {}
    for vv in vvs:
        for k, v in vv.items():
            if v > result.get(k, 0):
                result[k] = v
    return result


def parse_version_vector(raw: Optional[str]) -> dict[str, int]:
    """Parse a version_vector JSON string into a dict.

    Returns an empty dict if the value is None, empty, or unparseable.
    """
    if not raw:
        return {}
    try:
        vv = json.loads(raw)
        if isinstance(vv, dict):
            return {k: int(v) for k, v in vv.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def merge_vectors(
    agent_id: str, local: dict[str, int], remote: dict[str, int]
) -> dict[str, int]:
    """Merge two version vectors by taking the max per entry.

    Pure pointwise-max — idempotent and commutative.
    The caller is responsible for bumping the local clock BEFORE or AFTER
    calling merge_vectors, so that merge_vectors(x, x) == x.

    The ``agent_id`` parameter is accepted for API compatibility with
    callers that pass it, but is not used in the computation (the merge
    is a pure pointwise-max regardless of who is doing the merging).
    """
    merged: dict[str, int] = {}
    all_keys = set(local) | set(remote)
    for k in all_keys:
        merged[k] = max(local.get(k, 0), remote.get(k, 0))
    return merged
