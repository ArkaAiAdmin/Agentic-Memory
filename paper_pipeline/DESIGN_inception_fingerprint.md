# Design Spec: Inception-Fingerprint Identity

**Status:** LOCKED  
**Date:** 2026-07-11  
**Scope:** Phase 2 of `crdt_projection.py` — entity dedup grouping key

---

## Problem

Phase 2 groups entities by `(name, entity_type)` and picks `max(entity_id)` as the winner. This identity function is too lossy — it collapses entities that are genuinely distinct:

| Case | Scenario | Current behavior | Correct behavior |
|------|----------|-----------------|-----------------|
| 1 | Two peers concurrently create the same entity | `max(entity_id)` picks winner → correct | Collapse via `max(entity_id)` |
| 2 | Same entity re-imported under different local IDs; an outsider with higher ID steals the slot | Outsider wins → wrong | Only same-fingerprint entities compete |
| 3 | "Alice" (corporate lawyer) vs "Alice" (executive chef) — homonyms | Collapsed via `(name, type)` → wrong | Different fingerprints → coexist |

## Solution

Replace the `(name, entity_type)` grouping key with a **fingerprint** computed from the distinguishing context available at entity creation time.

```python
def compute_fingerprint(name: str, entity_type: str, description: str) -> str:
    canonical = lambda s: " ".join(s.lower().strip().split())
    payload = f"{canonical(name)}|{canonical(entity_type)}|{canonical(description)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

### Why description as the discriminator

- **peer_id** would prevent cross-peer collapse — same entity created by different agents would never merge. Wrong.
- **timestamp** would make every entity unique. Wrong.
- **description** captures the distinguishing context: "corporate lawyer at Skadden" vs "executive chef at Le Bernardin".

### Immutability

Computed at inception, never recomputed. Changing description after creation is a metadata update, not an identity change.

### Backwards compatibility

When `description=""`, `fingerprint = sha256(name|type|)` — exactly the old behavior. All existing tests pass without modification.

### Strict at write path, permissive at backfill

- **New writes:** Always compute and store fingerprint at creation time.
- **Legacy entities:** Compute fingerprint retroactively from current `(name, type, description)`. This is an approximation (inception metadata ≠ current metadata), but always produces a value and is strictly better than the current approach which ignores description entirely.

---

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | `fingerprint = sha256(canonicalize(name) \| canonicalize(type) \| canonicalize(description))` | Description is the only distinguishing context available at creation time |
| D2 | Canonicalization: `lower → strip → collapse_whitespace` | Normalizes trivial differences (case, spacing) without semantic normalization |
| D3 | Fingerprint immutable at inception | Identity continuity: changing description shouldn't change who the entity is |
| D4 | `max(entity_id)` retained only for same-fingerprint races | Still the correct tiebreaker for Case 1 (true concurrent creation) |
| D5 | Empty description → `sha256(name\|type\|)` | Degrades to old behavior; no information to distinguish |
| D6 | Legacy backfill: compute from current metadata | Permissive, always produces a value, strictly better than current approach |
| D7 | No write ever fails due to fingerprint computation | Even empty description produces a valid fingerprint |
| D8 | `UNIQUE(fingerprint)` replaces `UNIQUE(name, entity_type)` on canonical table | Enforces Invariant 2 at DB level |

## What Changes and What Doesn't

| Component | Change |
|-----------|--------|
| Phase 1 (entity merge) | **Unchanged** |
| Phase 2 (entity dedup) | **Grouping key changes** from `(name, entity_type)` to `fingerprint` |
| Phase 3 (edge redirect) | **Unchanged** — consumes the redirect map; doesn't care how it was produced |
| No-orphan invariant | **Unchanged** — Phase 3 still rewrites all loser IDs |
| Idempotence & determinism | **Unchanged** — fingerprint is deterministic, grouping is deterministic |
| `kg_entities` schema | **`UNIQUE(fingerprint)` replaces `UNIQUE(name, entity_type)`** |
| `EntityOp` dataclass | **`fingerprint` field added** |
| `_ENTITY_COLS` | **`fingerprint` added** |
| `_apply_entities` | **Writes `fingerprint` column** |

## Migration

- Migration 038: recreate `kg_entities` table with `fingerprint` column and `UNIQUE(fingerprint)` constraint
- Backfill script: compute fingerprints from existing `(name, type, description)` rows
- Zero-data-loss tested up and down
