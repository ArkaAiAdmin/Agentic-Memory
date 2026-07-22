"""Eval v5: FTS + embedding RRF with bge-base, fast iteration."""
from __future__ import annotations
import json, hashlib, os, re, sqlite3, sys, tempfile, time
from collections import defaultdict
from pathlib import Path
from statistics import mean
import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "eval"))

from metrics import compute_all_k
from _fixtures import bootstrap_temp_db_clean
from infra.memory_common import connection_pool, safe_close_db
from search.query_parser import _parse_search_query

KS = (5, 10, 30, 50)


def _join_turns(st):
    return "\n".join(t.get("content") or "" for t in st if t.get("content"))


def run():
    with open(str(HERE / "longmemeval_s_cleaned.json")) as f:
        corpus = json.load(f)
    evaluable = [q for q in corpus if not q["question_id"].endswith("_abs")][:50]

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(db_path)
    bootstrap_temp_db_clean(db_path)

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
    conn = sqlite3.connect(str(db_path))
    for sid in sorted_ids:
        content = _join_turns(all_sessions[sid])
        if not content.strip():
            continue
        conn.execute(
            'INSERT OR REPLACE INTO memories (id,content,source_file,category,tags,created_at,updated_at,observed_at,pinned,importance,tenant_id) VALUES (?,?,?,"sessions","[]",datetime("now"),datetime("now"),datetime("now"),0,3,"longmemeval")',
            (sid, content, f"longmemeval/{sid}"),
        )
    conn.commit()
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()

    # Load model
    from sentence_transformers import SentenceTransformer

    print("Loading bge-base-en-v1.5...", flush=True)
    t0 = time.time()
    model = SentenceTransformer("BAAI/bge-base-en-v1.5")
    print(f"Model loaded in {time.time() - t0:.1f}s", flush=True)

    # Pre-compute ALL embeddings in batch
    print("Pre-computing embeddings...", flush=True)
    t0 = time.time()
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        'SELECT id, content FROM memories WHERE deleted_at IS NULL AND category="sessions" AND content IS NOT NULL AND content != ""'
    ).fetchall()
    mids = [r[0] for r in rows]
    contents = [r[1] for r in rows]
    all_vecs = model.encode(contents, show_progress_bar=False, batch_size=256)
    for mid, vec in zip(mids, all_vecs):
        chash = hashlib.sha256(vec.tobytes()).hexdigest()[:16]
        conn.execute(
            "INSERT OR REPLACE INTO memory_embeddings (memory_id,content_hash,embedding,model_revision,dim,updated_at) VALUES (?,?,?,?,?,?)",
            (mid, chash, vec.tobytes(), "bge-base", int(vec.shape[0]), 0),
        )
    conn.commit()
    conn.close()
    print(f"Embedded {len(mids)} sessions in {time.time() - t0:.1f}s", flush=True)

    # Pre-load ALL embeddings into memory for fast cosine search
    print("Loading embeddings into RAM...", flush=True)
    conn = sqlite3.connect(str(db_path))
    emb_rows = conn.execute("SELECT memory_id, embedding FROM memory_embeddings").fetchall()
    conn.close()
    emb_map = {}
    for mid, blob in emb_rows:
        if blob:
            emb_map[mid] = np.frombuffer(blob, dtype=np.float32)
    print(f"Loaded {len(emb_map)} embeddings into RAM", flush=True)

    # Pre-encode ALL session contents for embedding search
    # (already done: all_vecs)

    # FTS search function
    def fts_search(query, limit=150):
        _, fts_q, _, _ = _parse_search_query(query, db_path)
        terms = re.findall(r"[\w@#\.+\-]+", fts_q, flags=re.UNICODE)
        if not terms:
            return [], fts_q
        db = connection_pool.get(str(db_path), timeout=10.0, tenant_id="longmemeval")
        try:
            rows = db.execute(
                'SELECT m.id, fts.rank FROM memories_fts fts '
                "JOIN tenant_memories m ON m.id = (SELECT id FROM memories WHERE rowid = fts.rowid) "
                'WHERE memories_fts MATCH ? AND m.deleted_at IS NULL AND m.category = "sessions" '
                "ORDER BY fts.rank LIMIT ?",
                (fts_q, limit),
            ).fetchall()
            return [(r[0], r[1]) for r in rows], fts_q
        finally:
            safe_close_db(db)

    # Embedding search function (pre-loaded, fast)
    def emb_search(query, limit=150):
        qvec = model.encode([query])[0]
        scores = {}
        for mid, dvec in emb_map.items():
            cos = float(np.dot(qvec, dvec) / (np.linalg.norm(qvec) * np.linalg.norm(dvec) + 1e-10))
            scores[mid] = cos
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return ranked[:limit]

    # RRF merge function
    def rrf_merge(fts_ranked, sem_ranked, k=30, fts_w=1.0, sem_w=2.0):
        rrf = {}
        for rank, (doc_id, _) in enumerate(fts_ranked):
            rrf[doc_id] = rrf.get(doc_id, 0) + fts_w / (k + rank + 1)
        for rank, (doc_id, _) in enumerate(sem_ranked):
            rrf[doc_id] = rrf.get(doc_id, 0) + sem_w / (k + rank + 1)
        return sorted(rrf.keys(), key=lambda x: -rrf[x])

    # Run eval
    print(f"\nRunning {len(evaluable)} questions...", flush=True)
    per_q = []
    per_metric = defaultdict(list)
    total_t = time.perf_counter()

    # Also test FTS-only for comparison
    fts_only_metric = defaultdict(list)

    for idx, q in enumerate(evaluable):
        qid = q["question_id"]
        gold = set(q["answer_session_ids"])
        question = q["question"]

        fts_ranked, _ = fts_search(question, limit=150)
        fts_ids = [r[0] for r in fts_ranked]

        # FTS-only
        fts_scores = compute_all_k(fts_ids[:50], gold, ks=KS)
        for k, v in fts_scores.items():
            fts_only_metric[k].append(v)

        # FTS + embedding RRF
        sem_ranked = emb_search(question, limit=150)
        merged = rrf_merge(fts_ranked, sem_ranked)
        scores = compute_all_k(merged[:50], gold, ks=KS)

        per_q.append({"question_id": qid, "question_type": q["question_type"], "scores": scores})
        for k, v in scores.items():
            per_metric[k].append(v)

        if (idx + 1) % 10 == 0 or idx == 0:
            elapsed = time.perf_counter() - total_t
            rate = (idx + 1) / elapsed
            print(
                f"  [{idx + 1}/{len(evaluable)}] "
                f"recall@10={scores['recall_any@10']:.2f}(rrf) "
                f"{fts_scores['recall_any@10']:.2f}(fts) "
                f"recall@50={scores['recall_any@50']:.2f} "
                f"rate={rate:.1f}/s",
                flush=True,
            )

    wall = time.perf_counter() - total_t

    # FTS-only results
    fts_agg = {f"recall_any@{k}": mean(fts_only_metric[f"recall_any@{k}"]) for k in KS}
    fts_agg.update({f"ndcg_any@{k}": mean(fts_only_metric[f"ndcg_any@{k}"]) for k in KS})

    # RRF results
    rrf_agg = {f"recall_any@{k}": mean(per_metric[f"recall_any@{k}"]) for k in KS}
    rrf_agg.update({f"recall_all@{k}": mean(per_metric[f"recall_all@{k}"]) for k in KS})
    rrf_agg.update({f"ndcg_any@{k}": mean(per_metric[f"ndcg_any@{k}"]) for k in KS})

    print(f"\n{'=' * 60}")
    print(f"Eval: {len(evaluable)}q in {wall:.1f}s ({wall / len(evaluable):.2f}s/q)")
    print(f"\nFTS-only:")
    for k, v in sorted(fts_agg.items()):
        print(f"  {k}: {v:.4f}")
    print(f"\nFTS + embedding RRF:")
    for k, v in sorted(rrf_agg.items()):
        print(f"  {k}: {v:.4f}")

    # Delta
    print(f"\nDelta (RRF - FTS):")
    for k in sorted(rrf_agg):
        if k in fts_agg:
            delta = rrf_agg[k] - fts_agg[k]
            print(f"  {k}: {delta:+.4f}")

    # Failed questions
    failed = [pq for pq in per_q if pq["scores"]["recall_any@10"] == 0]
    print(f"\nFailed recall@10: {len(failed)}/{len(evaluable)}")
    for pq in failed:
        print(f"  {pq['question_id']} ({pq['question_type']})")

    # Save
    out_path = HERE / "results" / "eval_v5_rrf.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"fts_only": fts_agg, "rrf": rrf_agg, "per_question": per_q}, f, indent=2)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    run()
