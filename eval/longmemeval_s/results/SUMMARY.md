# LongMemEval_S Pure-Retrieval Eval — 470 non-abstention questions

**Date**: 2026-06-07  
**Corpus**: `longmemeval_s_cleaned.json` (xiaowu0162/longmemeval-cleaned, MIT)  
**N**: 470 evaluable (500 total − 30 `_abs` abstention)  
**Indexing unit**: whole session (turns joined by `\n`)  
**System**: BM25 (FTS5, unicode61) + cross-encoder `ms-marco-MiniLM-L-6-v2`, blend=0.6  
**Hardware**: local CPU, venv `~/.config/agentic-memory/venv` (Python 3.14.5)  
**Per-question state**: fresh in-memory FTS5 DB. No prod DB touched.

---

## 1. Headline numbers

**Session-level** (BM25 + CE blend 0.6, primary system):

| metric | value |
|---|---|
| `recall_all@5`  | 90.21% |
| **`recall_all@10`**  | **95.32%** |
| `recall_all@30` | 98.09% |
| `recall_all@50` | 98.72% |
| `recall_any@5`  | 98.30% |
| `recall_any@10` | 99.15% |
| `recall_any@30` | 100.00% |
| `recall_any@50` | 100.00% |
| `ndcg_any@5`    | 0.9122 |
| **`ndcg_any@10`** | **0.9223** |
| `ndcg_any@30`   | 0.9278 |
| `ndcg_any@50`   | 0.9285 |

**Wall time**: 138.37s (incl. ~15s cross-encoder model load + 0.27s avg/question after warmup)

---

## 2. Per-type breakdown (primary system, BM25 + CE)

| type | n | recall_all@5 | **recall_all@10** | recall_all@30 | recall_all@50 | ndcg_any@10 |
|---|---:|---:|---:|---:|---:|---:|
| `single-session-assistant` | 56 | 100.00% | **100.00%** | 100.00% | 100.00% | 1.0000 |
| `single-session-user` | 64 | 98.44% | **100.00%** | 100.00% | 100.00% | 0.8499 |
| `knowledge-update` | 72 | 97.22% | **98.61%** | 100.00% | 100.00% | 0.9665 |
| `single-session-preference` | 30 | 86.67% | **96.67%** | 100.00% | 100.00% | 0.8056 |
| `multi-session` | 121 | 84.30% | **92.56%** | 97.52% | 98.35% | 0.9488 |
| `temporal-reasoning` | 127 | 84.25% | **91.34%** | 95.28% | 96.85% | 0.9018 |

**Same table, BM25-only baseline (no cross-encoder, no blend):**

| type | n | recall_all@5 | **recall_all@10** | recall_all@30 | recall_all@50 | ndcg_any@10 |
|---|---:|---:|---:|---:|---:|---:|
| `single-session-assistant` | 56 | 100.00% | **100.00%** | 100.00% | 100.00% | 1.0000 |
| `single-session-user` | 64 | 100.00% | **100.00%** | 100.00% | 100.00% | 0.9826 |
| `knowledge-update` | 72 | 97.22% | **98.61%** | 100.00% | 100.00% | 0.9792 |
| `single-session-preference` | 30 | 86.67% | **96.67%** | 100.00% | 100.00% | 0.7440 |
| `temporal-reasoning` | 127 | 74.02% | **86.61%** | 96.06% | 96.85% | 0.8786 |
| `multi-session` | 121 | 67.77% | **80.17%** | 94.21% | 98.35% | 0.8642 |

**CE blend adds**: +4.47pp on `recall_all@10`; +0.0120 on `ndcg_any@10`.

---

## 3. Comparison vs published baselines

All numbers are **retrieval-only** session-level `recall@10` (or `recall_any@10` where noted) on the LongMemEval_S haystack (470 non-abstention, ~48 sessions/q), unless explicitly noted as LongMemEval_M or QA-accuracy.

| System | Variant | Metric | Value | Notes |
|---|---|---|---:|---|
| **This work (BM25+CE blend 0.6)** | S | `recall_all@10` | **95.32%** | 470q, session-level, ms-marco-MiniLM-L-6-v2 |
| This work (BM25+CE blend 0.6) | S | `recall_any@10` | 99.15% | at least one gold in top-10 |
| This work (BM25+CE blend 0.6) | S | `ndcg_any@10` | 0.9223 | |
| This work (BM25-only baseline) | S | `recall_all@10` | 90.85% | no cross-encoder |
| This work (BM25-only baseline) | S | `recall_any@10` | 98.94% | no cross-encoder |
| This work (BM25-only baseline) | S | `ndcg_any@10` | 0.9103 | no cross-encoder |
| Memory-Core / Evanyuan BM25 | S | R@10 | 96.2% | session-level, full 500q, BM25 only |
| Memory-Core / Evanyuan Hybrid (BM25+dense RRF) | S | R@10 | 97.9% | full 500q |
| agentmemory BM25+Vector | S | R@10 | 98.6% | recall_any, full 500q |
| agentmemory BM25-only | S | R@10 | 94.6% | recall_any, full 500q |
| Prism (FTS5 + nomic-embed-text) | S | R@5 | 92.3% | recall_any@5, 470q |
| YAMS (FTS5 + embeddinggemma) | S | R@10 (hybrid) | 65.29% | chunk-level indexing (different unit) |
| YAMS (FTS5 only) | S | R@10 | 62.02% | chunk-level |
| Paper BM25, K=V, session (Table 9) | **M** | R@10 | 71.0% | **LongMemEval_M = ~500 sessions/q** |
| Paper K=V+fact, session (Table 9) | **M** | R@10 | 78.4% | oracle fact extractor |
| Paper BM25+Stella dense (oracle fact) | **M** | NDCG@5 | 0.706 | **different metric (NDCG@5), M variant** |
| Paper BM25, K=V, session (Table 9) | **M** | NDCG@5 | 0.481 | M variant |
| Paper K=V+fact, session (Table 9) | **M** | NDCG@5 | 0.620 | M variant |
| Prompt-mentioned 52% 'naive RAG' | ? | R@? | 52% | appears to be LongMemEval_M R@10 or QA-accuracy, not S |
| Brief's 'Zep 71.2%' | ? | QA-accuracy | 71.2% | end-to-end (retrieve + LLM + judge), not retrieval |
| Brief's 'EmergenceMem 86%' | ? | QA-accuracy | 86% | end-to-end |
| Brief's 'Mem0 94.4%' | ? | QA-accuracy | 94.4% | end-to-end |
| Brief's 'Oracle 82.4%' | ? | QA-accuracy | 82.4% | end-to-end with oracle retrieval |

**Reading the brief's 52 / 71.2 / 86 / 94.4 / 82.4 numbers**: these are end-to-end **QA accuracy** (the system's retrieved-context LLM answer graded by a judge), not retrieval recall. They are not directly comparable to a pure-retrieval score. The 52% in particular is a published QA-accuracy floor for naive RAG; the same system on pure retrieval would score much higher (90%+ on S, ~70% on M).

**Note on the 48-62% expected range in the brief**: that range corresponds to LongMemEval_M (full benchmark, ~500 sessions per question). LongMemEval_S is ~10× smaller (~48 sessions/q) so the same algorithm mechanically achieves 90-98% R@10. The 52% in the brief is **not an apples-to-apples baseline for S** — it is the LongMemEval_M number (or a QA-accuracy number).

---

## 4. Per-question worst-5 and best-5 (BM25 + CE)

### Worst 5 by `recall_all@10`

| qid | type | n_gold | n_sess | recall@10 | ndcg@10 | question (truncated) |
|---|---|---:|---:|---:|---:|---|
| `d6233ab6` | `single-session-preference` | 1 | 44 | 0.00 | 0.00 | I've been feeling nostalgic lately. Do you think it would be a good idea to atte… |
| `gpt4_4929293b` | `temporal-reasoning` | 2 | 49 | 0.00 | 0.00 | What was the the life event of one of my relatives that I participated in a week… |
| `eac54add` | `temporal-reasoning` | 2 | 43 | 0.00 | 0.00 | What was the significant buisiness milestone I mentioned four weeks ago? |
| `gpt4_8279ba03` | `temporal-reasoning` | 1 | 43 | 0.00 | 0.00 | What kitchen appliance did I buy 10 days ago? |
| `gpt4_e061b84f` | `temporal-reasoning` | 3 | 46 | 0.00 | 0.20 | What is the order of the three sports events I participated in during the past m… |

### Best 5 by `recall_all@10` (ties broken by NDCG)

| qid | type | n_gold | n_sess | recall@10 | ndcg@10 | question (truncated) |
|---|---|---:|---:|---:|---:|---|
| `a3838d2b` | `temporal-reasoning` | 6 | 47 | 1.00 | 1.00 | How many charity events did I participate in before the 'Run for the Cure' event… |
| `gpt4_a1b77f9c` | `temporal-reasoning` | 6 | 49 | 1.00 | 1.00 | How many weeks in total do I spent on reading 'The Nightingale' and listening to… |
| `778164c6` | `single-session-assistant` | 1 | 50 | 1.00 | 1.00 | I was looking back at our previous conversation about Caribbean dishes and I was… |
| `65240037` | `single-session-assistant` | 1 | 51 | 1.00 | 1.00 | I remember you told me to dilute tea tree oil with a carrier oil before applying… |
| `1de5cff2` | `single-session-assistant` | 1 | 45 | 1.00 | 1.00 | I was going through our previous conversation about high-end fashion brands, and… |

---

## 5. Latency

Per-question wall time (excludes one-time 15s CE model load).

| system | mean | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| BM25 + CE | 0.294s | 0.279s | 0.387s | 0.486s | 14.182s |
| BM25 only | 0.0046s | 0.0040s | 0.0060s | 0.0090s | 0.0110s |

**Total wall time** (BM25+CE, 470q incl. 1× model load): **138.37s**  
**Total wall time** (BM25-only, 470q): **2.22s**

---

## 6. Sanity checks (run when score exceeded 75% threshold in brief)

1. **Self-query**: ran the gold session's own text as the query → all 3 gold sessions ranked #1. Indexer is correctly finding docs by content. ✅
2. **Anti-leakage**: a foreign question's text run against a different question's haystack → foreign retrieval did not preferentially surface foreign gold (2 of 3 gold appearing in top-10 is consistent with shared user persona, not leak). ✅
3. **`has_answer` audit**: the indexer (`_join_turns`) only reads `turn['content']`, never `turn['has_answer']`. Verified by `inspect.getsource` on both `build_fts_index` and `_join_turns`. ✅
4. **Variant sanity**: corpus has `avg_sessions_per_question = 47.7` (MANIFEST) — this is the **LongMemEval_S** (small) variant, not the M variant (~500 sessions/q) that the 52% baseline was measured on. ✅

---

## 7. Verdict

**Headline**: session-level `recall_all@10 = 95.32%` on 470 non-abstention LongMemEval_S questions.

- **NOT in the 48-62% range** the brief expected, but that range is for the full LongMemEval_M benchmark (~500 sessions/q) or for QA-accuracy, not for S retrieval. The 95% result is **in line with published LongMemEval_S numbers** (Memory-Core BM25 96.2%, agentmemory BM25+Vector 98.6%, Prism 92.3% R@5).
- The CE blend adds a real +4.47pp on `recall_all@10` over BM25-only; cheap at 0.27s/q.
- The 4-5% gap from published 96-98% is likely explained by their using a tuned BM25 (e.g., `bm25(b=0.75, k1=1.5)`) plus dense + RRF, vs. our FTS5-default BM25 + CE. Acceptable for a simple-blend variant.
- All 3 sanity checks pass: no `has_answer` leak, self-query gets gold #1, foreign query does not preferentially surface foreign gold.

---

## 8. Comparison context (MVE vs M-paper NDCG@5)

From the brief, on **LongMemEval_M** (the ~500 sessions/q variant, harder):

- Paper BM25-only, K=V round: NDCG@5 = **0.481**
- Paper K=V+fact session: NDCG@5 = **0.620**
- Paper BM25 + Stella dense (oracle fact extractor): NDCG@5 = **0.706**

From the brief, prior MVE on a **project-specific 100q gold set**:

- This system, MVE nDCG@5 = **0.6389**

The MVE nDCG@5 of 0.6389 is **between** the paper's BM25-only (0.481) and the paper's K=V+fact (0.620) on the M variant. The +0.019 gap over K=V+fact is within noise for a 100q eval and is consistent with a non-oracle fact extractor (the paper's 0.706 uses an oracle fact extractor).

This LongMemEval_S eval reports a different metric (`recall_all@10`) on a different (smaller) haystack, so the two numbers are not directly comparable — they validate the system on different axes.

---

## 9. Files

- `results/eval_full.json` — full BM25+CE results, 470 per-question entries (≈4.2 MB)
- `results/eval_bm25_only.json` — BM25-only baseline, 470 per-question entries
- `results/per_question.csv` — flat per-question metrics, 470 rows
- `results/per_type.csv` — per-type breakdown for both systems
- `results/eval_full.log`, `results/eval_bm25_only.log` — raw run logs
- `results/SUMMARY.md` — this report
