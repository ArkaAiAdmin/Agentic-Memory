# Known Issues

> Current limitations and open problems. Each entry includes severity,
> impact, and planned mitigation path.

## CRDT Push Not Field-Aware

**Severity**: Medium  
**Impact**: Multi-agent sync loses granular field-level merges when using the legacy `crdt_save` path.

The field-level CRDT (`crdt_field.py`, migration 013) correctly merges per-field updates, but the `crdt_push` / `crdt_save` sync path still operates at the note level for pre-v13 peers. When two agents edit different fields of the same note and one peer is pre-v13, the entire note wins via LWW instead of per-field merge.

**Mitigation**: Upgrade all peers to v13+ schema. The field-level path is the default for v13+; the legacy path is a backward-compatibility fallback only.

## Saga Doesn't Cover Post-Save Hooks

**Severity**: Low  
**Impact**: Post-save operations (fitness recalc, tier update, audit flush) are not rolled back on saga failure.

The saga wraps the DB upsert and file write, but post-save hooks (e.g., `_recalculate_fitness_scores`, `_record_last_accessed`) run after the saga commits. If a post-save hook fails, the memory is already persisted but derived state may be inconsistent.

**Mitigation**: The hooks are idempotent and re-run on the next save or cron cycle. The `memory_maintenance(operation="rebuild")` command can fully recompute derived state.

## Neural Forget Curve Is Actually Jaccard

**Severity**: Low  
**Impact**: Naming confusion. The "neural forget curve" phase (Phase 7) uses a Jaccard-based surprise metric, not a neural network.

The forget curve implementation in `search/scoring.py` computes surprise as `1 - Jaccard(query_tokens, memory_tokens)` rather than using a trained neural model. The name is historical and somewhat misleading.

**Mitigation**: Documentation update. The scoring formula is deterministic and requires zero model loading, which is a feature for local-first deployments.

## SQLite Limits Horizontal Scaling

**Severity**: Medium  
**Impact**: Single-writer constraint limits write throughput; no built-in horizontal scaling.

SQLite's WAL mode allows concurrent reads but serializes writes. For high-throughput multi-agent deployments, the write journal (`journal.db`) serializes writes through a single background worker. Horizontal scaling requires external coordination (e.g., multiple DB instances with CRDT sync).

**Mitigation**: The CRDT sync layer enables multi-instance deployments. Each instance maintains its own SQLite DB and syncs via the field-level CRDT protocol. Write throughput scales linearly with instance count.

## No Managed Offering Yet

**Severity**: Info  
**Impact**: Users must self-host. No cloud/SaaS option available.

Agentic Memory is designed for local-first deployment. There is no hosted cloud service. Users run their own SQLite instances and manage their own infrastructure.

**Mitigation**: The REST API and Docker deployment patterns make self-hosting straightforward. A managed offering is on the roadmap but not yet planned.

## No Published Benchmarks Yet

**Severity**: Info  
**Impact**: No standardized performance comparison with alternatives.

Internal benchmarks exist in `eval/benchmarks/` (save ~1.2ms, search ~15ms on typical workloads), but there is no published, reproducible benchmark suite comparing against Mem0, Zep, or Letta.

**Mitigation**: Benchmark scripts are available for self-evaluation. A formal benchmark publication is planned as part of the research paper pipeline.
