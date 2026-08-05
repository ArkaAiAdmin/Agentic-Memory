-- Migration 077: Index saga_log for crash-recovery scan.
--
-- recover_incomplete_sagas (infra/saga.py:151) runs a correlated
-- NOT EXISTS query over saga_log on every open_db (see infra/db.py).
-- saga_log had zero indexes and ~23k rows, so the query took ~3s per
-- call. With cron cron_resolve_contradictions opening one session per
-- contradiction pair, 50 pairs * ~3s of the recovered-on-each-open
-- scan blew through the 300s background-worker timeout and wedged the
-- worker in a respawn loop at ~99% CPU.
--
-- These two indexes turn the orphan scan into an index lookup:
--   * (saga_id, step_idx) serves the correlated NOT EXISTS inner query
--     (t.saga_id = s.saga_id AND t.step_idx = s.step_idx)
--   * (status) serves the outer orphan filter (s.status = 'intent')
--     and the completed-steps scan (status='done' ORDER BY step_idx).

CREATE INDEX IF NOT EXISTS idx_saga_log_saga_step ON saga_log(saga_id, step_idx);
CREATE INDEX IF NOT EXISTS idx_saga_log_status ON saga_log(status);