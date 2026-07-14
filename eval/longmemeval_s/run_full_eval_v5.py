"""Full LongMemEval eval — memory-efficient, uses full search_memories pipeline.

Key optimization: stream sessions into DB in batches, don't hold all in memory.
Uses the full 14-phase pipeline: FTS + CE chunk reranking + late interaction + etc.
"""
from __future__ import annotations
import json, sqlite3, sys, tempfile, time
from collections import defaultdict
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "eval"))

from metrics import compute_all_k
from eval._fixtures import bootstrap_temp_db_clean

KS = (5, 10, 30, 50)


def _join_turns(st):
    return "\n".join(t.get("content") or "" for t in st if t.get("content"))


def _parse_haystack_date(ds):
    import re
    parts = ds.split("(")
    dp = parts[0].strip()
    tp = "00:00"
    if len(parts) > 1:
        m = re.search(r"(\d{2}:\d{2})", parts[1])
        if m:
            tp = m.group(1)
    return f"{dp.replace('/', '-')} {tp}:00"


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 500

    # Load questions — keep only IDs and metadata, not session content
    with open(str(HERE / "longmemeval_s_cleaned.json")) as f:
        corpus = json.load(f)
    evaluable = [q for q in corpus if not q["question_id"].endswith("_abs")][:limit]

    # Collect unique session IDs and dates (lightweight)
    all_session_ids = set()
    session_dates = {}
    for q in evaluable:
        for sid in q.get("haystack_session_ids", []):
            all_session_ids.add(sid)
        for sid, d in zip(q.get("haystack_session_ids", []), q.get("haystack_dates", [])):
            session_dates[sid] = d

    # Create DB and seed sessions in batches (memory-efficient)
    tmpdir = tempfile.mkdtemp(prefix="longmemeval_full_")
    db_path = Path(tmpdir) / "memory.db"
    bootstrap_temp_db_clean(db_path)

    print(f"Seeding {len(all_session_ids)} sessions in batches...", flush=True)
    conn = sqlite3.connect(str(db_path))
    batch_size = 500
    seeded = 0
    for q in evaluable:
        for sid, sess, d in zip(
            q.get("haystack_session_ids", []),
            q.get("haystack_sessions", []),
            q.get("haystack_dates", []),
        ):
            if seeded >= len(all_session_ids):
                break
            content = _join_turns(sess)
            if not content.strip():
                continue
            obs = _parse_haystack_date(d) if d else "datetime('now')"
            conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, content, source_file, category, tags, created_at, updated_at,
                    observed_at, pinned, importance, tenant_id)
                   VALUES (?, ?, ?, 'sessions', '[]', datetime('now'), datetime('now'),
                           ?, 0, 3, 'longmemeval')""",
                (sid, content, f"longmemeval/{sid}", obs),
            )
            seeded += 1
            if seeded % batch_size == 0:
                conn.commit()
                print(f"  Seeded {seeded}/{len(all_session_ids)}...", flush=True)
    conn.commit()
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()
    print(f"Seeded {seeded} sessions. DB size: {db_path.stat().st_size / 1024 / 1024:.1f} MB", flush=True)

    # Pre-warm models
    print("Pre-warming models...", flush=True)
    from infra._lazy_imports import get_embedding_search
    es = get_embedding_search()
    for i in range(120):
        if es.model is not None:
            print(f"  Embedding model ready ({i}s)", flush=True)
            break
        time.sleep(1)
    from search.rerankers import _get_ce_chunk_model
    ce = _get_ce_chunk_model()
    print(f"  CE model ready", flush=True)

    # Run eval — process questions one at a time, no session content in memory
    from search.orchestrator import search_memories

    print(f"\nRunning {len(evaluable)} questions...", flush=True)
    per_q = []
    per_metric = defaultdict(list)
    total_t = time.perf_counter()

    for idx, q in enumerate(evaluable):
        qid = q["question_id"]
        gold = set(q["answer_session_ids"])
        question = q["question"]

        try:
            result = search_memories(
                db_path,
                question,
                limit=50,
                category="sessions",
                tenant_id="longmemeval",
                hybrid=False,  # Embedding search wastes 1GB MPS memory and hurts recall
                deep_rerank=False,
                rerank=True,
                light=False,
            )
            ranked = [r["id"] for r in result.get("results", [])]
        except Exception as e:
            print(f"  Error on {qid}: {e}", flush=True)
            ranked = []

        scores = compute_all_k(ranked, gold, ks=KS)
        per_q.append({
            "question_id": qid,
            "question_type": q.get("question_type", ""),
            "scores": scores,
        })
        for k, v in scores.items():
            per_metric[k].append(v)

        if (idx + 1) % 25 == 0 or idx == 0:
            elapsed = time.perf_counter() - total_t
            rate = (idx + 1) / elapsed
            eta = (len(evaluable) - idx - 1) / rate / 60 if rate > 0 else 0
            print(
                f"  [{idx + 1}/{len(evaluable)}] {qid} ({q.get('question_type', '')}) "
                f"recall@10={scores['recall_any@10']:.2f} "
                f"rate={rate:.2f}/s ETA={eta:.0f}min",
                flush=True,
            )

    wall = time.perf_counter() - total_t
    agg = {f"recall_any@{k}": mean(per_metric[f"recall_any@{k}"]) for k in KS}
    agg.update({f"recall_all@{k}": mean(per_metric[f"recall_all@{k}"]) for k in KS})
    agg.update({f"ndcg_any@{k}": mean(per_metric[f"ndcg_any@{k}"]) for k in KS})

    print(f"\n{'='*60}")
    print(f"Full LongMemEval: {len(evaluable)} questions, {wall:.0f}s ({wall/len(evaluable):.1f}s/q)")
    print(f"\nMacro-averaged metrics:")
    for k, v in sorted(agg.items()):
        print(f"  {k}: {v:.4f}")

    by_type = defaultdict(lambda: defaultdict(list))
    for pq in per_q:
        qt = pq["question_type"]
        for k, v in pq["scores"].items():
            by_type[qt][k].append(v)

    print(f"\nPer-type breakdown:")
    for qt in sorted(by_type):
        r10 = mean(by_type[qt]["recall_any@10"])
        r50 = mean(by_type[qt]["recall_any@50"])
        ndcg10 = mean(by_type[qt]["ndcg_any@10"])
        n = len(by_type[qt]["recall_any@10"])
        print(f"  {qt} (n={n}): recall@10={r10:.4f} recall@50={r50:.4f} ndcg@10={ndcg10:.4f}")

    failed = [pq for pq in per_q if pq["scores"]["recall_any@10"] == 0]
    print(f"\nFailed (recall@10=0): {len(failed)}/{len(evaluable)}")
    for pq in failed:
        print(f"  {pq['question_id']} ({pq['question_type']})")

    # Save results
    out_path = HERE / "results" / "eval_full_pipeline_v5.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "macro_metrics": agg,
            "per_question": per_q,
            "n_questions": len(evaluable),
            "wall_time_s": round(wall, 2),
            "mean_latency_ms": round(wall / len(evaluable) * 1000, 1) if evaluable else 0,
        }, f, indent=2)
    print(f"\nResults saved to {out_path}")

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
