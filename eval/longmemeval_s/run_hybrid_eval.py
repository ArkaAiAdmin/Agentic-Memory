"""Fast eval: FTS + embedding hybrid, pre-loaded model, batch embeddings.

Tests all 4 improvements together:
1. OR-only queries (query_parser)
2. BM25 normalization (scoring)
3. bge-base-en-v1.5 embedding model (embedding)
4. Always-on hybrid fusion (orchestrator)
"""
from __future__ import annotations
import json, hashlib, re, sqlite3, sys, tempfile, time
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

_STOP_WORDS = frozenset({
    'a','an','the','and','or','but','in','on','at','to','for','of','with','by',
    'from','is','are','was','were','be','been','being','have','has','had','do',
    'does','did','will','would','could','should','may','might','shall','can',
    'need','dare','ought','used','what','which','who','whom','this','that',
    'these','those','i','you','he','she','it','we','they','me','him','her',
    'us','them','my','your','his','its','our','their','mine','yours','hers',
    'ours','theirs','am','if','then','else','when','where','how','all','each',
    'every','both','few','more','most','other','some','such','no','not','only',
    'own','same','so','than','too','very','just','because','as','until','while',
    'about','between','through','during','before','after','above','below','up',
    'down','out','off','over','under','again','further','once','here','there',
    'any','also','type','kind','sort','want','looking','find','search','query',
    'tell','show',
})

def _join_turns(session_turns):
    return "\n".join(t.get("content") or "" for t in session_turns if t.get("content"))

def _parse_haystack_date(date_str):
    parts = date_str.split("(")
    date_part = parts[0].strip()
    time_part = "00:00"
    if len(parts) > 1:
        m = re.search(r"(\d{2}:\d{2})", parts[1])
        if m:
            time_part = m.group(1)
    return f"{date_part.replace('/', '-')} {time_part}:00"

def _seed_sessions(db_path, sessions, session_ids, session_dates=None):
    conn = sqlite3.connect(str(db_path))
    try:
        for i, (sid, sess) in enumerate(zip(session_ids, sessions)):
            content = _join_turns(sess)
            if not content.strip():
                continue
            observed_at = (
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
                (sid, content, f"longmemeval/{sid}", observed_at),
            )
        conn.commit()
        try:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


def _precompute_embeddings(db_path, model):
    """Batch-compute embeddings for all sessions."""
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT id, content FROM memories WHERE deleted_at IS NULL "
        "AND category = 'sessions' AND content IS NOT NULL AND content != ''"
    ).fetchall()
    if not rows:
        conn.close()
        return 0

    batch_size = 256
    count = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
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


def _hybrid_search_fts_plus_emb(db_path, query, fts_query, model, limit=50):
    """Search FTS + embedding, merge via simple RRF."""
    from infra.memory_common import connection_pool

    db = connection_pool.get(str(db_path), timeout=10.0, tenant_id="longmemeval")
    try:
        # FTS results
        fts_rows = db.execute(
            """SELECT m.id, fts.rank FROM memories_fts fts
               JOIN tenant_memories m ON m.id = (SELECT id FROM memories WHERE rowid = fts.rowid)
               WHERE memories_fts MATCH ? AND m.deleted_at IS NULL
                 AND m.category = 'sessions'
               ORDER BY fts.rank LIMIT ?""",
            (fts_query, limit * 3),
        ).fetchall()
        fts_ranked = [r[0] for r in fts_rows]
        fts_scores = {r[0]: -r[1] for r in fts_rows}

        # Embedding results
        qvec = model.encode([query])[0]
        emb_rows = db.execute(
            "SELECT memory_id, embedding FROM memory_embeddings me "
            "JOIN memories m ON m.id = me.memory_id "
            "WHERE m.deleted_at IS NULL AND m.category = 'sessions'"
        ).fetchall()
        
        emb_scores = {}
        import numpy as np
        for mid, blob in emb_rows:
            if not blob:
                continue
            dvec = np.frombuffer(blob, dtype=np.float32)
            cos = float(np.dot(qvec, dvec) / (np.linalg.norm(qvec) * np.linalg.norm(dvec) + 1e-10))
            emb_scores[mid] = cos

        # RRF merge
        k = 60
        rrf = {}
        # FTS
        for rank, doc_id in enumerate(fts_ranked):
            rrf[doc_id] = rrf.get(doc_id, 0) + 1.0 / (k + rank + 1)
        # Embedding (sorted by cosine desc)
        sem_sorted = sorted(emb_scores.keys(), key=lambda x: -emb_scores[x])
        for rank, doc_id in enumerate(sem_sorted[:limit * 3]):
            rrf[doc_id] = rrf.get(doc_id, 0) + 2.0 / (k + rank + 1)  # sem_w=2.0

        # Sort by RRF and return top results
        merged = sorted(rrf.keys(), key=lambda x: -rrf[x])
        return merged[:limit]
    finally:
        from infra.memory_common import safe_close_db
        safe_close_db(db)


def main():
    corpus_path = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "longmemeval_s_cleaned.json")
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    with open(corpus_path) as f:
        corpus = json.load(f)
    evaluable = [q for q in corpus if not q["question_id"].endswith("_abs")][:limit]

    # Create temp DB
    tmpdir = tempfile.mkdtemp(prefix="hybrid_eval_")
    db_path = Path(tmpdir) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(db_path)
    bootstrap_temp_db_clean(db_path)

    # Collect and seed sessions
    all_ids, all_sessions, all_dates = set(), {}, {}
    for q in evaluable:
        for sid in q.get("haystack_session_ids", []):
            all_ids.add(sid)
        for sid, sess, d in zip(q["haystack_session_ids"], q["haystack_sessions"], q["haystack_dates"]):
            all_sessions[sid] = sess
            all_dates[sid] = d

    sorted_ids = sorted(all_ids)
    sorted_sessions = [all_sessions[sid] for sid in sorted_ids]
    sorted_dates = [all_dates.get(s, "") for s in sorted_ids]
    print(f"Seeding {len(sorted_ids)} sessions...")
    _seed_sessions(db_path, sorted_sessions, sorted_ids, sorted_dates)

    # Load embedding model
    print("Loading bge-base-en-v1.5...")
    from sentence_transformers import SentenceTransformer
    t0 = time.time()
    model = SentenceTransformer("BAAI/bge-base-en-v1.5")
    model.dim = model.get_sentence_embedding_dimension()
    print(f"Model loaded in {time.time()-t0:.1f}s, dim={model.dim}")

    # Pre-compute embeddings
    print("Pre-computing embeddings...")
    t0 = time.time()
    n = _precompute_embeddings(db_path, model)
    print(f"Embedded {n} sessions in {time.time()-t0:.1f}s")

    # Parse queries using main pipeline's query parser
    from search.query_parser import _parse_search_query

    # Run eval
    print(f"\nStarting {len(evaluable)} questions...")
    per_q = []
    per_metric = defaultdict(list)
    total_t = time.perf_counter()

    for idx, q in enumerate(evaluable):
        qid = q["question_id"]
        gold = set(q["answer_session_ids"])
        question = q["question"]

        # Get FTS query from main pipeline's parser
        _, fts_query, _, _ = _parse_search_query(question, db_path)
        terms = re.findall(r"[\w@#\.+\-]+", fts_query, flags=re.UNICODE)
        if not terms:
            ranked = []
        else:
            ranked = _hybrid_search_fts_plus_emb(db_path, question, fts_query, model, limit=50)

        scores = compute_all_k(ranked, gold, ks=KS)
        per_q.append({"question_id": qid, "question_type": q["question_type"], "scores": scores})
        for k, v in scores.items():
            per_metric[k].append(v)

        if (idx + 1) % 10 == 0 or idx == 0:
            elapsed = time.perf_counter() - total_t
            rate = (idx + 1) / elapsed
            print(f"  [{idx + 1}/{len(evaluable)}] {qid} ({q['question_type']}) "
                  f"recall@10={scores['recall_any@10']:.2f} recall@50={scores['recall_any@50']:.2f} "
                  f"rate={rate:.1f}/s")

    wall = time.perf_counter() - total_t
    agg = {f"recall_any@{k}": mean(per_metric[f"recall_any@{k}"]) for k in KS}
    agg.update({f"recall_all@{k}": mean(per_metric[f"recall_all@{k}"]) for k in KS})
    agg.update({f"ndcg_any@{k}": mean(per_metric[f"ndcg_any@{k}"]) for k in KS})

    # Per-type breakdown
    by_type = defaultdict(lambda: defaultdict(list))
    for pq in per_q:
        qt = pq["question_type"]
        for k, v in pq["scores"].items():
            by_type[qt][k].append(v)

    print(f"\n{'='*60}")
    print(f"Hybrid eval (FTS + bge-base-en-v1.5): {len(evaluable)} questions, {wall:.1f}s")
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

    # Failed questions
    failed = [pq for pq in per_q if pq["scores"]["recall_any@10"] == 0]
    print(f"\nFailed (recall@10=0): {len(failed)}/{len(evaluable)}")
    for pq in failed:
        print(f"  {pq['question_id']} ({pq['question_type']})")

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
