Each file is an independent entry point invoked by crontab or `memory_maintenance(operation=...)`. There is no shared orchestrator inside this module — each script bootstraps itself by prepending the repo root to `sys.path`, then imports runtime helpers from sibling packages (`infra.*`, `search.*`, `background.cron_model_lock`).

- Model-training jobs (`cron_train_temporal_ssm.py`, `cron_train_forget_model.py`) pull CTR feedback from `memory_ctr_feedback` over a rolling 30-day window, build NumPy feature vectors, run hand-rolled SGD, and atomically write weights back into `memory.toml` under `[features]` via temp-file + rename.
- `cron_train_ltr.py` builds graded-label training data (clicked=2, returned=1, dismissed=0) using `search.ltr.features.extract_ltr_features`, trains a LightGBM LambdaMART model with group-based train/val split, and persists it to `models/ltr/model.txt`.
- `cron_tune_rewrites.py` fits per-query-type logistic regression on `memory_search_interaction`, normalizes weights to sum to 1.0, and writes them into `memory_query_type_stats` only when AUC > 0.5.
- `cron_recompute_temporal_priors.py` linear-regresses log(access_count) vs age_days per category to fit exponential half-lives, persisted in `memory_temporal_priors`.
- `cron_embedding_recompute.py` delegates to `background.cron_model_lock` + `infra.embedding_recompute.check_and_rebuild` to detect embedding-model drift and rebuild the vec index.
- `cron_answer_rerank.py` pre-computes cross-encoder answer-rerank scores for hot memories into a cache table, also cleaning stale entries.
- `cron_quality_filter.py` is a thin wrapper around `quality_gates.quality_stats(conn)` gated by `MEMORY_QUALITY_GATES=1`.

All scripts acquire process-level mutual exclusion via `_flock.acquire_lock_or_exit(...)` before touching the database or config, and every DB connection uses `PRAGMA busy_timeout` / `foreign_keys=ON` plus optional tenant scoping through `infra.tenant_query.install_tenant_context`.