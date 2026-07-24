# Audit To-Do — 2026-06-24

Source: 4-area technical audit (save / search / sync / multi-agent), 30+ findings, ~97% verified.
**Status:** Both agentic-memory and omega-memory MCP servers became unstable mid-session.
This file is the durable copy of the in-chat to-do list. Once the MCP server recovers, run
`omega-memory_omega_store` (or `agentic-memory_memory_save`) with the JSON below to persist.

## Priority: P0 — Correctness bugs (fix immediately)

- [ ] **1. UPDATE-style saga rollback leaves orphan chunks/embeddings**
  `saga.py:696` + `save/cleanup.py:90-147`
  `_cleanup_dependent_rows` covers kg_facts/kg_edges/backlinks but NOT memory_chunks/memory_embeddings. UPDATE rollback (row restored) skips FK cascade.
  Need: add `remove_chunks_and_embeddings_for_note` in save/cleanup.py, call from both branches of `_undo_upsert`.

- [ ] **2. `_sweep_orphan_rows` leaks `PRAGMA foreign_keys=OFF`**
  `auto_save.py:1994`
  Sets OFF, finally at 2010-2011 only `safe_close_db(conn)` — no restore. Connection returns to pool with FK disabled.
  Need: wrap in try/finally with `PRAGMA foreign_keys=ON` restore.

- [ ] **3. `crdt_field_save` not saga-wrapped**
  `crdt_field.py:394`
  Bare `conn.commit()` per call. Error path at L391-392 `continue`s without rollback.
  Need: wrap in single transaction, commit only after full batch.

- [ ] **4. `/sync/peers/gossip` is unauthenticated**
  `sync_server.py:302-311`
  No `_require_auth()` call (sibling `_handle_changes` at L318 has one). Metadata injection surface.
  Need: add `if not self._require_auth(): return` at top of `_handle_gossip_peers`.

- [ ] **5. Saga `_rollback` skips steps whose `do` returns `None`**
  `saga.py:295, 346`
  `r.do_result is None` check. Latent (no current step returns None) but fragile.
  Need: explicit `completed: bool` field on SagaRecord.

## Priority: P1 — High severity (this week)

- [ ] **6. `_hook_auto_backlink_with_flush` commits caller's conn**
  `post_save_hooks.py:353` — side-effect commit on connection caller owns.

- [ ] **7. tier written via separate UPDATE, not atomic**
  `save_pipeline.py:538-541` — separate UPDATE after main upsert.

- [ ] **8. RRF fusion is append-only, not merge-sorted**
  `search/orchestrator.py:913` — `list(results) + semantic_only`, RRF scores never used for sort.

- [ ] **9. Vestigial dead imports** (~200 LOC)
  `search/instrumentation.py` + 4 funcs in `orchestrator.py:75-80` + `_wrap_result_row` at 237-273. All dead.

- [ ] **10. `_apply_neural_forget_curve` potential N+1**
  `scoring.py:187` — `_galq(note_id)` per row.

- [ ] **11. `install_crontab.sh` hardcodes venv path**
  `install_crontab.sh:70` — `VENV_PY="$ROOT/venv/bin/python"`. No MEMORY_PYTHON fallback.

- [ ] **12. Blanket xfail in conftest**
  `eval/conftest.py:10-22` — H21 follow-up, ~20+ files xfailed.

## Priority: P2 — Medium (this sprint)

- [ ] **13.** `WHERE source_id = ? OR source_id = ?` blocks index. `save/cleanup.py:80` → `IN (?, ?)`.
- [ ] **14.** Orphan cleanup loop per-get. `db.py:263-275`.
- [ ] **15.** Dead threads' connections leak (depth>0). `db.py:264`.
- [ ] **16.** `_top_recent_tags` GROUP BY on JSON. `query_parser.py:289-292`.
- [ ] **17.** `_search_full_scan` is O(N). `embedding_search.py:836-850`.
- [ ] **18.** `db_write_queue.ProxyConnection` in-memory per execute. `db_write_queue.py:144`.
- [ ] **19.** `_vv_dominates` vs `dominates` inconsistency. `crdt_field.py:137-152` vs `crdt_merge.py:67-73`.
- [ ] **20.** `PRAGMA table_info` every `_write_ssm_state`. `save/indexers.py:133`.
- [ ] **21.** `cleanup_fts5_orphans` per-row DELETE. `fts.py:49-50` → batch `WHERE rowid IN (...)`.
- [ ] **22.** `_archive_one_autosave` FK/log gap. `auto_save.py:1940-1978`.
- [ ] **23.** Stale `.processing.*` files. `auto_save.py:550-559` — no startup sweep.
- [ ] **24.** `_load_circuit_state_from_audit` at import. `auto_save.py:1543-1544`.
- [ ] **25.** `_open_server_db` silent fallback. `sync_server.py:99-105`.
- [ ] **26.** `/crdt/changes` raw sqlite3. `sync_server.py:343-350`.
- [ ] **27.** mDNS port 5353 conflict. `mdns_discovery.py:90`.
- [ ] **28.** N+1 patterns (4): `reinforce_memories_db`, `_auto_fts_backlinks`, `_enrich_context`, `cleanup_fts5_orphans`.
- [ ] **29.** Doc drift check. `docs/architecture.md:102` claims 102 modules.
- [ ] **30.** *Audit was wrong*: only `test_all_regression.py` is empty (0 test fns). The other 4 "placeholder" files have 45 real tests (5+2+8+30).

## Priority: P3 — Low (backlog)

- [ ] **31.** Magic numbers. `memory_injection.py:214` denominator `4.0`.
- [ ] **32.** Stopword duplication. `rerankers.py:33` + `chunk_index.py:34`.
- [ ] **33.** N+1 in CTR click-proxy. `save/indexers.py:247-257`.
- [ ] **35.** Dead: `get_write_lock`. `db.py:401-413` — comment "used by save_memory" is false.
- [ ] **36.** No-op `_reset_save_memory_cache`. `save/__init__.py:100-109`.
- [ ] **37.** `_SQL_SAFE_FILTER_RE` theoretical injection. `search/orchestrator.py:122`.
- [ ] **38.** cron env var override missing in `install_crontab.sh`.

## Suggested first batch (verified, <30 LOC, safe)

1. **#2** — restore `PRAGMA foreign_keys=ON` (3 lines)
2. **#4** — add `_require_auth()` to gossip (3 lines)
3. **#9** — delete dead instrumentation (~200 LOC removed)
4. **#13** — `IN (?, ?)` in cleanup.py (1 line)
5. **#21** — batch DELETE in fts.py (~5 lines)
6. **#35** — delete unused `get_write_lock` (-13 LOC)

## Audit accuracy
- 25/30 confirmed exactly via direct file reads
- 3 refined (real but narrower)
- 1 wrong (P2 #30)
- 1 plausible but unchecked (P2 #29)
