# Conflict-Free Knowledge Graph Projection

**Paper:** `Conflict-Free Knowledge Graph Projection.pdf` (v4 — final preprint)
**Author:** Subrata Sadhu (Independent Researcher)
**Date:** 2026-07-16

## What this is

A standalone reference implementation of the three-phase CRDT projection
pipeline that produces a canonical local-first knowledge graph from
multi-agent operation logs.

[![Paper badge](https://img.shields.io/badge/paper-KG_Projection-blue)](https://github.com/ArkaAiAdmin/agentic-memory-paper)

> **Preprint, under review.** No DOI yet — cite via repository URL (below).

## Cite

If you refer to or build on this work, cite the paper using the
`CITATION.cff` in this directory, or use the following BibTeX entry:

```bibtex
@article{sadhu2026conflict,
  title={Conflict-Free Knowledge Graph Projection: A Three-Phase CRDT Pipeline for Multi-Agent Memory Systems},
  author={Sadhu, Subrata},
  year={2026},
  url={https://github.com/ArkaAiAdmin/agentic-memory-paper},
  version={4}
}
```

## Layout

- `Conflict-Free Knowledge Graph Projection.pdf`             — paper (v4, final preprint).
- `paper_version.txt`                                       — version and date.
- `CITATION.cff`                                            — machine-readable citation.
- `Conflict-Free Knowledge Graph Projection.md`             — markdown source.
- `crdt_projection.py`  — standalone reference implementation
  (no agent-specific imports; stdlib only: sqlite3, dataclasses, typing).
- `test_pipeline.py`    — pytest test suite.

Reproduces all evaluation scenarios from §7 of the paper. Run
`pytest test_pipeline.py -v`. Expected: **48 passed in <1 s**.

## Production alignment

The production CRDT implementation lives in `kg/kg_crdt.py` inside the
agentic-memory repository. The reference implementation in this directory
is kept in sync with production. Known alignment points:

- **Entity merge** (`merge_entity_ops`): uses `vv_dominates` for causal
  partial-order comparison; ties broken by timestamp then agent_id.
  Sort key uses `_serialise_vv` (JSON format) for
  deterministic tiebreaks — matches production.
- **Edge merge** (`merge_edge_ops`): uses `vv_dominates`, NOT `vv_sum`.
  `vv_sum` (summing all component clocks) was an earlier simplification
  in the paper that conflates concurrent ops with different component-wise
  clocks (e.g. `{A:3,B:0}` vs `{A:0,B:3}` both sum to 3). The paper has
  been corrected to match the production implementation.
- **Fingerprint** (`compute_fingerprint`): canonicalises name, entity_type,
  and description via `lower().strip().split()` then SHA-256 — matches
  production `kg/kg_crdt.py` and `DESIGN_inception_fingerprint.md`.
- **Redirect map** (`entity_dedup_via_crdt` + `redirect_edge_ids`):
  loser entity_ids are mapped to winner via max(entity_id) tiebreaker;
  edges are rewritten through the redirect map before applying to
  `kg_edges` — matches production.
- **Version-vector helpers**: `vv_dominates`, `vv_merge`, `vv_sum`,
  `_serialise_vv`, `_parse_vv` all present in both paper and production.

## Validation

Last verified run: 2026-07-16.

```text
$ pytest test_pipeline.py -v
============================== 48 passed in 0.09s ==============================
```

If a count changes, the paper's reproducibility statement needs an update.

## Reproducibility

All figures and tables in the paper are generated from `crdt_projection.py`
and `test_pipeline.py`. No manual data editing was performed. To regenerate:

```bash
cd paper_pipeline
python3 -m pytest test_pipeline.py -v
```

## License

- Text (paper): CC-BY-4.0 — see `LICENSE`.
- Code: Apache-2.0 — see upstream [agentic-memory/LICENSE](https://github.com/ArkaAiAdmin/agentic-memory/blob/main/LICENSE).

## AI assistance disclosure

Conceptual drafting, literature review, and typesetting assistance used
publicly available AI assistants and free-tier paid models. All technical
claims, equations, and code references were verified manually against the
codebase.

## Upstream

Code lives in agentic-memory: https://github.com/ArkaAiAdmin/agentic-memory
Paper lives here. Cross-references in the paper to the agentic-memory
system use that URL.
