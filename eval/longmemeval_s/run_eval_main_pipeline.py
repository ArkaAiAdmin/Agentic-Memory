"""
LongMemEval_S evaluation using the MAIN search pipeline.

Instead of the custom BM25+CE retrieval in retrieval.py, this uses
search.orchestrator.search_memories — the same pipeline used in production.

This means improvements to the main search pipeline automatically
improve benchmark scores.

Usage:
  venv/bin/python eval/longmemeval_s/run_eval_main_pipeline.py \\
      --input eval/longmemeval_s/longmemeval_s_cleaned.json \\
      --output eval/longmemeval_s/results/eval_main_pipeline.json \\
      --limit 50
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import logging

logger = logging.getLogger(__name__)
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
EVAL_DIR = REPO_ROOT / "eval"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(EVAL_DIR))

from metrics import compute_all_k
from search.orchestrator import search_memories

# Bootstrap a temp DB with the full schema
from _fixtures import bootstrap_temp_db_clean, populate_eval_memory_indexes, set_benchmark_env

set_benchmark_env()


KS = (5, 10, 30, 50)


def is_evaluable(entry: dict) -> bool:
    return not entry["question_id"].endswith("_abs")


def load_corpus(path: str, limit: int | None = None) -> list[dict]:
    import json
    if limit is None:
        with open(path) as f:
            return json.load(f)

    evaluable_questions = []
    current_lines = []
    
    with open(path, "r", encoding="utf-8") as f:
        f.readline()  # skip [
        for line in f:
            if line.startswith("    {"):
                current_lines = [line]
            elif line.startswith("    },") or line.startswith("    }"):
                current_lines.append(line)
                obj_str = "".join(current_lines).rstrip().rstrip(",")
                try:
                    q = json.loads(obj_str)
                    if not q["question_id"].endswith("_abs"):
                        evaluable_questions.append(q)
                        if len(evaluable_questions) >= limit:
                            break
                except Exception as exc:
                    logger.debug("Failed to parse JSON question object (non-fatal): %s", exc)
            else:
                current_lines.append(line)
                
    return evaluable_questions


def _join_turns(session_turns: list[dict]) -> str:
    """Concatenate turn contents with newline separator."""
    parts = []
    for turn in session_turns:
        c = turn.get("content") or ""
        if c:
            parts.append(c)
    return "\n".join(parts)


def _parse_haystack_date(date_str: str) -> str:
    """Parse '2023/05/20 (Sat) 02:21' into ISO-8601 datetime string."""
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


def _seed_sessions(db_path: Path, sessions: list[list[dict]], session_ids: list[str],
                   session_dates: list[str] | None = None) -> None:
    """Insert all sessions into the memories table for search_memories to find."""
    import json as _json

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
        # Rebuild FTS index
        try:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.commit()
        except Exception as exc:
            logger.debug("FTS rebuild skipped (non-fatal): %s", exc)
        # Index chunks for every seeded session so the chunk-level
        # hybrid search path (wired into search_memories) has data to
        # retrieve. Long sessions are split into turns; the most
        # relevant turn surfaces even when the rest of the session is noise.
        try:
            for sid, sess in zip(session_ids, sessions):
                content = _join_turns(sess)
                if content.strip():
                    populate_eval_memory_indexes(conn, sid, content, category="sessions")
            conn.commit()
        except Exception as _ck_e:
            print(f"  indexing skipped: {_ck_e}")

    finally:
        conn.close()


def run(corpus: list[dict], limit: int | None = None, db_path: Path | None = None) -> dict:
    evaluable = [q for q in corpus if is_evaluable(q)]
    if limit is not None:
        evaluable = evaluable[:limit]

    # Create a temp DB for this evaluation run
    if db_path is None:
        tmpdir = tempfile.mkdtemp(prefix="longmemeval_main_")
        db_path = Path(tmpdir) / "memory.db"
        bootstrap_temp_db_clean(db_path)
        cleanup = True
    else:
        cleanup = False

    # Warm up embedding model synchronously so it's ready before queries start
    try:
        from sentence_transformers import SentenceTransformer
        _emb_model = SentenceTransformer("BAAI/bge-base-en-v1.5")
        print(f"Embedding model loaded: {type(_emb_model).__name__}")
        # Inject into the lazy loader so search_memories finds it
        from infra._lazy_imports import get_embedding_search
        es = get_embedding_search()
        es.model = _emb_model
    except Exception as e:
        print(f"WARNING: Embedding model warm-up failed: {e}")
        _emb_model = None

    def _embed_and_store_batch(conn, sessions: list[tuple[str, str]], model) -> int:
        """Batch-compute and store embeddings for sessions."""
        import hashlib, time as _t
        if model is None:
            return 0
        model_name = getattr(model, 'name', 'bge-large')
        count = 0
        # Batch encode for speed (100 at a time)
        batch_size = 100
        for i in range(0, len(sessions), batch_size):
            batch = sessions[i:i + batch_size]
            contents = [c for _, c in batch]
            try:
                vecs = model.encode(contents, show_progress_bar=False, batch_size=batch_size)
            except Exception as exc:
                logger.debug("Batch encode failed, falling back to individual: %s", exc)
                vecs = [model.encode([c])[0] for c in contents]
            for (mid, content), vec in zip(batch, vecs):
                chash = hashlib.sha256(content.encode()).hexdigest()[:16]
                conn.execute(
                    "INSERT OR REPLACE INTO memory_embeddings "
                    "(memory_id, content_hash, embedding, model_revision, dim, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mid, chash, vec.tobytes(), model_name, int(getattr(model, 'dim', vec.shape[0])), _t.time()),
                )
                count += 1
        return count

    # Collect and seed sessions grouped by question tenant_id
    print(f"Seeding sessions for {len(evaluable)} questions into DB...")
    conn = sqlite3.connect(str(db_path))
    try:
        for q in evaluable:
            qid = q["question_id"]
            t_id = f"longmem_{qid}"
            s_ids = q.get("haystack_session_ids", [])
            sessions = q.get("haystack_sessions", [])
            dates = q.get("haystack_dates", [])
            for i, (sid, sess) in enumerate(zip(s_ids, sessions)):
                content = _join_turns(sess)
                if not content.strip():
                    continue
                observed_at = (
                    _parse_haystack_date(dates[i])
                    if dates and i < len(dates)
                    else "datetime('now')"
                )
                conn.execute(
                    """INSERT OR REPLACE INTO memories
                       (id, content, source_file, category, tags, created_at, updated_at,
                        observed_at, pinned, importance, tenant_id)
                       VALUES (?, ?, ?, 'sessions', '[]', datetime('now'), datetime('now'),
                               ?, 0, 3, ?)""",
                    (sid, content, f"longmemeval/{sid}", observed_at, t_id),
                )
        conn.commit()
        try:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.commit()
        except Exception as exc:
            logger.debug("FTS rebuild skipped for seeded sessions (non-fatal): %s", exc)
    finally:
        conn.close()
    print(f"Done seeding. DB size: {db_path.stat().st_size / 1024:.1f} KB")

    # Pre-compute embeddings AFTER seeding
    try:
        conn = sqlite3.connect(str(db_path))
        has_emb = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
        if has_emb == 0:
            print("Pre-computing embeddings for all sessions...")
            from infra._lazy_imports import get_embedding_search
            es = get_embedding_search()
            model = es.model
            if model is None:
                print("WARNING: Embedding model not loaded, skipping pre-compute")
            else:
                rows = conn.execute(
                    "SELECT id, content FROM memories WHERE deleted_at IS NULL "
                    "AND category = 'sessions' AND content IS NOT NULL AND content != ''"
                ).fetchall()
                sessions_to_embed = [(r[0], r[1]) for r in rows if r[1]]
                n = _embed_and_store_batch(conn, sessions_to_embed, model)
                conn.commit()
                print(f"Embedded {n} sessions")
        else:
            print(f"Found {has_emb} existing embeddings, skipping pre-compute")
        conn.close()
    except Exception as e:
        print(f"Embedding pre-compute failed: {e}")

    print(f"Starting evaluation of {len(evaluable)} questions...")

    per_q = []
    per_metric = defaultdict(list)
    total_t = time.perf_counter()

    for idx, q in enumerate(evaluable):
        qid = q["question_id"]
        gold = set(q["answer_session_ids"])
        question = q["question"]

        # Use the main search pipeline — FTS + weak CE reranking, no hybrid
        try:
            qdate = q.get("question_date")
            as_of_val = None
            if qdate:
                try:
                    dt_str = _parse_haystack_date(qdate)
                    from datetime import datetime
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    as_of_val = dt.timestamp()
                except Exception as exc:
                    logger.debug("Failed to parse question date for temporal filter (non-fatal): %s", exc)

            tenant_id = f"longmem_{qid}"
            result = search_memories(
                db_path,
                question,
                limit=50,
                category="sessions",
                tenant_id=tenant_id,
                hybrid=True,
                deep_rerank=False,
                rerank=True,
                light=False,
                as_of=as_of_val,
            )
            ranked = [r["id"] for r in result.get("results", [])]
        except Exception as e:
            print(f"  Error on {qid}: {e}")
            ranked = []

        scores = compute_all_k(ranked, gold, ks=KS)
        per_q.append({
            "question_id": qid,
            "question_type": q["question_type"],
            "n_sessions": len(q.get("haystack_session_ids", [])),
            "scores": scores,
        })
        for k, v in scores.items():
            per_metric[k].append(v)
        # Progress reporting every 25 questions
        if (idx + 1) % 25 == 0 or idx == 0:
            elapsed = time.perf_counter() - total_t
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            eta = (len(evaluable) - idx - 1) / rate if rate > 0 else 0
            print(
                f"  [{idx + 1}/{len(evaluable)}] {qid} ({q['question_type']}) "
                f"recall@10={scores['recall_any@10']:.2f} "
                f"rate={rate:.1f}/s ETA={eta:.0f}s",
                flush=True
            )

        if (idx + 1) % 25 == 0 or idx == 0 or idx == len(evaluable) - 1:
            print(
                f"  [{idx + 1}/{len(evaluable)}] {qid} ({q['question_type']}) "
                f"recall@10={scores['recall_any@10']:.2f} "
                f"latency={(time.perf_counter() - total_t) / (idx + 1):.3f}s"
            )

    wall = time.perf_counter() - total_t
    agg = {f"recall_any@{k}": mean(per_metric[f"recall_any@{k}"]) for k in KS}
    agg.update({f"recall_all@{k}": mean(per_metric[f"recall_all@{k}"]) for k in KS})
    agg.update({f"ndcg_any@{k}": mean(per_metric[f"ndcg_any@{k}"]) for k in KS})

    report = {
        "n_questions": len(evaluable),
        "wall_time_s": round(wall, 2),
        "mean_latency_ms": round(wall / len(evaluable) * 1000, 1) if evaluable else 0,
        "macro_metrics": agg,
        "per_question": per_q,
    }

    if cleanup:
        shutil.rmtree(db_path.parent, ignore_errors=True)

    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    corpus = load_corpus(args.input, limit=args.limit)
    report = run(corpus, limit=args.limit)

    with open(args.output, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n{'=' * 60}")
    print("Macro-averaged metrics:")
    for k, v in sorted(report["macro_metrics"].items()):
        print(f"  {k}: {v:.4f}")
    print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()
