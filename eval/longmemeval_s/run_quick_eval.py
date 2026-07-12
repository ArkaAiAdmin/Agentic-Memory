"""Quick eval: FTS-only, no hybrid, no rerank — fast diagnostic.

Measures pure FTS recall to isolate the impact of OR queries + BM25 normalization.
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


def is_evaluable(entry: dict) -> bool:
    return not entry["question_id"].endswith("_abs")


def _join_turns(session_turns: list[dict]) -> str:
    parts = []
    for turn in session_turns:
        c = turn.get("content") or ""
        if c:
            parts.append(c)
    return "\n".join(parts)


def _parse_haystack_date(date_str: str) -> str:
    import re as _re
    parts = date_str.split("(")
    date_part = parts[0].strip()
    time_part = "00:00"
    if len(parts) > 1:
        m = _re.search(r"(\d{2}:\d{2})", parts[1])
        if m:
            time_part = m.group(1)
    iso_date = date_part.replace("/", "-")
    return f"{iso_date} {time_part}:00"


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


def _fts_only_search(db_path, query, limit=50, tenant_id="longmemeval"):
    """Pure FTS5 search with no embedding/reranking overhead."""
    from search.query_parser import _parse_search_query
    import re
    from infra.memory_common import connection_pool

    normalized_query, fts_query, bare_text, _ = _parse_search_query(query, db_path)
    terms = re.findall(r"[\w@#\.+\-]+", fts_query, flags=re.UNICODE)
    if not terms:
        return []

    db = connection_pool.get(str(db_path), timeout=10.0, tenant_id=tenant_id)
    try:
        # Category filter for sessions
        rows = db.execute(
            """SELECT m.id, fts.rank FROM memories_fts fts
               JOIN tenant_memories m ON m.id = (SELECT id FROM memories WHERE rowid = fts.rowid)
               WHERE memories_fts MATCH ? AND m.deleted_at IS NULL
                 AND m.category = 'sessions'
               ORDER BY fts.rank LIMIT ?""",
            (fts_query, limit),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        from infra.memory_common import safe_close_db
        safe_close_db(db)


def main():
    corpus_path = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "longmemeval_s_cleaned.json")
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    with open(corpus_path) as f:
        corpus = json.load(f)
    evaluable = [q for q in corpus if is_evaluable(q)][:limit]

    # Create temp DB
    tmpdir = tempfile.mkdtemp(prefix="quick_eval_")
    db_path = Path(tmpdir) / "memory.db"
    bootstrap_temp_db_clean(db_path)

    # Collect all sessions
    all_ids, all_sessions, all_dates = set(), {}, {}
    for q in evaluable:
        for sid in q.get("haystack_session_ids", []):
            all_ids.add(sid)
        for sid, sess, d in zip(
            q.get("haystack_session_ids", []),
            q.get("haystack_sessions", []),
            q.get("haystack_dates", []),
        ):
            all_sessions[sid] = sess
            all_dates[sid] = d

    sorted_ids = sorted(all_ids)
    sorted_sessions = [all_sessions[sid] for sid in sorted_ids]
    sorted_dates = [all_dates.get(s, "") for s in sorted_ids]
    print(f"Seeding {len(sorted_ids)} sessions...")
    _seed_sessions(db_path, sorted_sessions, sorted_ids, sorted_dates)
    print(f"DB ready. Starting {len(evaluable)} questions...")

    per_q = []
    per_metric = defaultdict(list)
    t0 = time.perf_counter()

    for idx, q in enumerate(evaluable):
        qid = q["question_id"]
        gold = set(q["answer_session_ids"])
        question = q["question"]

        ranked = _fts_only_search(db_path, question, limit=50)
        scores = compute_all_k(ranked, gold, ks=KS)
        per_q.append({"question_id": qid, "question_type": q["question_type"], "scores": scores})
        for k, v in scores.items():
            per_metric[k].append(v)

        if (idx + 1) % 10 == 0 or idx == 0:
            elapsed = time.perf_counter() - t0
            rate = (idx + 1) / elapsed
            print(f"  [{idx + 1}/{len(evaluable)}] {qid} ({q['question_type']}) "
                  f"recall@10={scores['recall_any@10']:.2f} rate={rate:.1f}/s")

    wall = time.perf_counter() - t0
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
    print(f"FTS-only eval: {len(evaluable)} questions, {wall:.1f}s ({wall/len(evaluable):.2f}s/q)")
    print(f"\nMacro metrics:")
    for k, v in sorted(agg.items()):
        print(f"  {k}: {v:.4f}")
    print(f"\nPer-type breakdown:")
    for qt in sorted(by_type):
        r10 = mean(by_type[qt]["recall_any@10"])
        r50 = mean(by_type[qt]["recall_any@50"])
        n = len(by_type[qt]["recall_any@10"])
        print(f"  {qt} (n={n}): recall@10={r10:.4f} recall@50={r50:.4f}")

    # Failed questions
    failed = [pq for pq in per_q if pq["scores"]["recall_any@10"] == 0]
    print(f"\nFailed (recall@10=0): {len(failed)}/{len(evaluable)}")
    for pq in failed[:10]:
        print(f"  {pq['question_id']} ({pq['question_type']})")

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
