"""Eval v4: Full pipeline via search_memories with bge-base model.

Tests: OR queries + BM25 norm + bge-base embeddings + CE reranking + hybrid fusion.
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
from search.orchestrator import search_memories

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

def _seed_sessions(db_path, sessions, session_ids, session_dates=None):
    conn = sqlite3.connect(str(db_path))
    try:
        for i, (sid, sess) in enumerate(zip(session_ids, sessions)):
            content = _join_turns(sess)
            if not content.strip():
                continue
            obs = (
                _parse_haystack_date(session_dates[i])
                if session_dates and i < len(session_dates)
                else "datetime('now')"
            )
            conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, content, source_file, category, tags, created_at, updated_at,
                    observed_at, pinned, importance, tenant_id)
                   VALUES (?, ?, ?, 'sessions', '[]', datetime('now'), datetime('now'),
                           ?, 0, 3, 'longmemeval')""",
                (sid, content, f"longmemeval/{sid}", obs),
            )
        conn.commit()
        try:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.commit()
        except Exception:
            pass
        # Index chunks
        try:
            from search.chunk_index import _qw5_index_chunks_for, _qw5_ensure_schema
            _qw5_ensure_schema(conn)
            for sid, sess in zip(session_ids, sessions):
                content = _join_turns(sess)
                if content.strip():
                    _qw5_index_chunks_for(conn, sid, content)
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()

def _precompute_embeddings(db_path, model):
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT id, content FROM memories WHERE deleted_at IS NULL "
        "AND category = 'sessions' AND content IS NOT NULL AND content != ''"
    ).fetchall()
    if not rows:
        conn.close()
        return 0
    import hashlib
    batch_size = 256
    count = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        mids = [r[0] for r in batch]
        contents = [r[1] for r in batch]
        vecs = model.encode(contents, show_progress_bar=False, batch_size=batch_size)
        for mid, vec in zip(mids, vecs):
            chash = hashlib.sha256(vec.tobytes()).hexdigest()[:16]
            conn.execute(
                "INSERT OR REPLACE INTO memory_embeddings "
                "(memory_id, content_hash, embedding, model_revision, dim, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (mid, chash, vec.tobytes(), "bge-base", int(vec.shape[0]), time.time()),
            )
            count += 1
    conn.commit()
    conn.close()
    return count


def main():
    corpus_path = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "longmemeval_s_cleaned.json")
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    with open(corpus_path) as f:
        corpus = json.load(f)
    evaluable = [q for q in corpus if not q["question_id"].endswith("_abs")][:limit]

    tmpdir = tempfile.mkdtemp(prefix="full_eval_")
    db_path = Path(tmpdir) / "memory.db"
    bootstrap_temp_db_clean(db_path)

    # Collect sessions
    all_ids, all_sessions, all_dates = set(), {}, {}
    for q in evaluable:
        for sid in q.get("haystack_session_ids", []):
            all_ids.add(sid)
        for sid, sess, d in zip(
            q["haystack_session_ids"], q["haystack_sessions"], q["haystack_dates"]
        ):
            all_sessions[sid] = sess
            all_dates[sid] = d

    sorted_ids = sorted(all_ids)
    sorted_sessions = [all_sessions[sid] for sid in sorted_ids]
    sorted_dates = [all_dates.get(s, "") for s in sorted_ids]
    print(f"Seeding {len(sorted_ids)} sessions...")
    _seed_sessions(db_path, sorted_sessions, sorted_ids, sorted_dates)

    # Load and warm embedding model
    print("Loading bge-base-en-v1.5...")
    from infra._lazy_imports import get_embedding_search
    es = get_embedding_search()
    t0 = time.time()
    for i in range(120):
        if es.model is not None:
            print(f"Embedding model ready: {type(es.model).__name__} dim={getattr(es.model, 'dim', '?')} in {time.time()-t0:.1f}s")
            break
        time.sleep(1)
    else:
        print(f"WARNING: model not loaded in 120s")

    # Pre-compute embeddings
    if es.model is not None:
        print("Pre-computing embeddings...")
        t0 = time.time()
        n = _precompute_embeddings(db_path, es.model)
        print(f"Embedded {n} sessions in {time.time()-t0:.1f}s")

    # Run eval
    print(f"\nStarting {len(evaluable)} questions...")
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
                hybrid=True,
                deep_rerank=False,
                rerank=True,
                light=False,
            )
            ranked = [r["id"] for r in result.get("results", [])]
        except Exception as e:
            print(f"  Error on {qid}: {e}")
            ranked = []

        scores = compute_all_k(ranked, gold, ks=KS)
        per_q.append({"question_id": qid, "question_type": q["question_type"], "scores": scores})
        for k, v in scores.items():
            per_metric[k].append(v)

        if (idx + 1) % 10 == 0 or idx == 0:
            elapsed = time.perf_counter() - total_t
            rate = (idx + 1) / elapsed
            print(
                f"  [{idx + 1}/{len(evaluable)}] {qid} ({q['question_type']}) "
                f"recall@10={scores['recall_any@10']:.2f} recall@50={scores['recall_any@50']:.2f} "
                f"rate={rate:.1f}/s"
            )

    wall = time.perf_counter() - total_t
    agg = {f"recall_any@{k}": mean(per_metric[f"recall_any@{k}"]) for k in KS}
    agg.update({f"recall_all@{k}": mean(per_metric[f"recall_all@{k}"]) for k in KS})
    agg.update({f"ndcg_any@{k}": mean(per_metric[f"ndcg_any@{k}"]) for k in KS})

    by_type = defaultdict(lambda: defaultdict(list))
    for pq in per_q:
        qt = pq["question_type"]
        for k, v in pq["scores"].items():
            by_type[qt][k].append(v)

    print(f"\n{'=' * 60}")
    print(f"Full pipeline eval: {len(evaluable)} questions, {wall:.1f}s ({wall / len(evaluable):.2f}s/q)")
    print(f"\nMacro metrics:")
    for k, v in sorted(agg.items()):
        print(f"  {k}: {v:.4f}")
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
    out_path = HERE / "results" / "eval_full_pipeline_bge_base.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "macro_metrics": agg,
            "per_question": per_q,
            "n_questions": len(evaluable),
            "wall_time_s": round(wall, 2),
        }, f, indent=2)
    print(f"\nResults saved to {out_path}")

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
