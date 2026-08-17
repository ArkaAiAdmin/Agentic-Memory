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
from datetime import datetime, timezone

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
from _fixtures import (
    bootstrap_temp_db_clean,
    format_query_progress,
    init_benchmark_stdout,
    populate_eval_memory_indexes,
    print_stage_banner,
    print_summary_report,
    set_benchmark_env,
    write_live_progress,
)

init_benchmark_stdout()
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

    conn = sqlite3.connect(str(db_path))
    try:
        for i, (sid, sess) in enumerate(zip(session_ids, sessions)):
            content = _join_turns(sess)
            if not content.strip():
                continue
            observed_at = (
                _parse_haystack_date(session_dates[i])
                if session_dates and i < len(session_dates)
                else datetime.now(timezone.utc).isoformat()
            )
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, content, source_file, category, tags, created_at, updated_at,
                   observed_at, pinned, importance, tenant_id)
                   VALUES (?, ?, ?, 'sessions', '[]', ?, ?,
                           ?, 0, 3, 'longmemeval')""",
                (sid, content, f"longmemeval/{sid}", observed_at, now, now, observed_at),
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


def run(
    corpus: list[dict],
    limit: int | None = None,
    db_path: Path | None = None,
    output_file: str | None = None,
    resume: bool = False,
    force_reseed: bool = False,
) -> dict:
    import gc
    from infra.cache import clear_all_caches

    # Clear all in-memory search caches prior to evaluation
    clear_all_caches()

    evaluable = [q for q in corpus if is_evaluable(q)]
    if limit is not None:
        evaluable = evaluable[:limit]

    # Check for existing checkpoint/output when resume is enabled
    completed_qids = set()
    per_q = []
    per_metric = defaultdict(list)

    checkpoint_path = Path(output_file + ".checkpoint") if output_file else None
    target_out_path = Path(output_file) if output_file else None

    if resume and (target_out_path and target_out_path.exists() or checkpoint_path and checkpoint_path.exists()):
        read_path = target_out_path if target_out_path and target_out_path.exists() else checkpoint_path
        assert read_path is not None
        try:
            with open(read_path, "r", encoding="utf-8") as f:
                prev_report = json.load(f)
                prev_per_q = prev_report.get("per_question", [])
                for item in prev_per_q:
                    qid = item["question_id"]
                    completed_qids.add(qid)
                    per_q.append(item)
                    for k, v in item.get("scores", {}).items():
                        per_metric[k].append(v)
            print(f"Resuming run: loaded {len(completed_qids)} completed questions from {read_path}")
        except Exception as exc:
            print(f"WARNING: Failed to read checkpoint {read_path} ({exc}), starting from scratch")
            completed_qids.clear()
            per_q.clear()
            per_metric.clear()

    # Create a temp DB for this evaluation run
    if db_path is None:
        tmpdir = tempfile.mkdtemp(prefix="longmemeval_main_")
        db_path = Path(tmpdir) / "memory.db"
        bootstrap_temp_db_clean(db_path)
        cleanup = True
    else:
        cleanup = False
    os.environ["MEMORY_DB_PATH"] = str(db_path)

    # Check dependency health
    try:
        import sentence_transformers
        print(f"Using sentence_transformers ({sentence_transformers.__file__})")
    except ImportError:
        print("\n" + "=" * 60)
        print("WARNING: sentence-transformers is NOT installed in this Python environment.")
        print(f"Executing binary: {sys.executable}")
        print("Search will SILENTLY degrade to FTS-only BM25 search path.")
        print("For full 14-phase hybrid/CE evaluation, run with venv/bin/python.")
        print("=" * 60 + "\n")

    # Warm up embedding model synchronously so it's ready before queries start
    try:
        from sentence_transformers import SentenceTransformer
        _emb_model = SentenceTransformer("BAAI/bge-m3")
        print(f"Embedding model loaded: {type(_emb_model).__name__}")
        from infra._lazy_imports import get_embedding_search
        es = get_embedding_search()
        es.model = _emb_model
    except Exception as e:
        print(f"WARNING: Embedding model warm-up failed: {e}")
        _emb_model = None

    def _embed_and_store_batch(conn, sessions: list[tuple[str, str]], model) -> int:
        """Batch-compute and store embeddings for sessions."""
        import hashlib
        import time as _t
        if model is None:
            return 0
        model_name = getattr(model, 'name', 'bge-large')
        count = 0
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

    # Phase 2: Seeding & Pre-computing
    print_stage_banner(2, "Database Ingestion & Multi-Index Building", f"db={db_path.name}")
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
                    else datetime.now(timezone.utc).isoformat()
                )
                conn.execute(
                    """INSERT OR REPLACE INTO memories
                       (id, content, source_file, category, tags, created_at, updated_at,
                        observed_at, pinned, importance, tenant_id)
                       VALUES (?, ?, ?, 'sessions', '[]', datetime('now'), datetime('now'),
                               ?, 0, 3, ?)""",
                    (sid, content, f"longmemeval/{sid}", observed_at, t_id),
                )
                try:
                    from search.chunk_index import _qw5_ensure_schema, _qw5_index_chunks_for
                    _qw5_ensure_schema(conn)
                    _qw5_index_chunks_for(conn, sid, content)
                except Exception:
                    pass

        conn.commit()
        try:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.commit()
        except Exception as exc:
            logger.debug("FTS rebuild skipped for seeded sessions (non-fatal): %s", exc)
    finally:
        conn.close()
    print(f"✓ Done seeding. DB size: {db_path.stat().st_size / 1024:.1f} KB")

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
                print(f"✓ Embedded {n} sessions")
        else:
            print(f"✓ Found {has_emb} existing embeddings, skipping pre-compute")
        conn.close()
    except Exception as e:
        print(f"Embedding pre-compute failed: {e}")

    try:
        from rebuild_vec_index import rebuild_vec_index
        stats = rebuild_vec_index(str(db_path))
        print(
            f"✓ Vector index built: {stats.get('n_indexed')} items ({stats.get('serialized_bytes')} bytes) in {stats.get('elapsed_s', 0.0):.2f}s",
            flush=True,
        )
    except Exception as exc:
        logger.warning("vec index build failed (non-fatal): %s", exc)

    # Phase 3: Warmup
    print_stage_banner(3, "Search Pipeline Warmup", "Pre-warming dense vectors & cross-encoders")
    try:
        _ = search_memories(
            query="warmup query",
            db_path=db_path,
            limit=1,
            category="sessions",
            tenant_id="longmem_warmup",
            hybrid=True,
            deep_rerank=False,
            rerank=True,
        )
        print("✓ Encoders pre-warmed successfully.", flush=True)
    except Exception as exc:
        print(f"  ⚠ Warmup non-fatal notice: {exc}", flush=True)

    # Phase 4: Evaluation Execution
    print_stage_banner(4, "Evaluation Execution", f"{len(evaluable)} questions against 14-phase search pipeline")
    total_t = time.perf_counter()
    progress_file = EVAL_DIR / "results" / ".progress.json"
    suite_progress_file = EVAL_DIR / "results" / ".progress_longmemeval_s.py.json"
    per_type_scores: dict[str, list[float]] = {}
    latencies: list[float] = []

    def _save_checkpoint():
        if not output_file or not per_q:
            return
        wall_so_far = time.perf_counter() - total_t
        m_agg = {f"recall_any@{k}": mean(per_metric[f"recall_any@{k}"]) for k in KS if per_metric[f"recall_any@{k}"]}
        m_agg.update({f"recall_all@{k}": mean(per_metric[f"recall_all@{k}"]) for k in KS if per_metric[f"recall_all@{k}"]})
        m_agg.update({f"ndcg_any@{k}": mean(per_metric[f"ndcg_any@{k}"]) for k in KS if per_metric[f"ndcg_any@{k}"]})
        ckpt_report = {
            "n_questions": len(per_q),
            "total_questions": len(evaluable),
            "wall_time_s": round(wall_so_far, 2),
            "mean_latency_ms": round(wall_so_far / len(per_q) * 1000, 1) if per_q else 0,
            "macro_metrics": m_agg,
            "per_question": per_q,
        }
        tmp_ckpt = Path(output_file + ".tmp")
        try:
            with open(tmp_ckpt, "w", encoding="utf-8") as f:
                json.dump(ckpt_report, f, indent=2)
            os.replace(tmp_ckpt, checkpoint_path or Path(output_file))
        except Exception as exc:
            logger.debug("Checkpoint save failed (non-fatal): %s", exc)

    for idx, q in enumerate(evaluable, start=1):
        qid = q["question_id"]
        if qid in completed_qids:
            continue

        gold = set(q["answer_session_ids"])
        question = q["question"]
        qtype = q.get("question_type", "general")

        t_q0 = time.perf_counter()
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
                query=question,
                db_path=db_path,
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
        dt_ms = (time.perf_counter() - t_q0) * 1000.0
        latencies.append(dt_ms)

        scores = compute_all_k(ranked, gold, ks=KS)
        score_10 = scores.get("recall_any@10", 0.0)

        per_q.append({
            "question_id": qid,
            "question_type": qtype,
            "n_sessions": len(q.get("haystack_session_ids", [])),
            "scores": scores,
            "latency_ms": round(dt_ms, 2),
        })
        completed_qids.add(qid)
        for k, v in scores.items():
            per_metric[k].append(v)
        per_type_scores.setdefault(qtype, []).append(score_10)

        running_rec10 = mean(per_metric["recall_any@10"]) if per_metric.get("recall_any@10") else 0.0
        running_per_type = {
            t: mean(scs) for t, scs in per_type_scores.items() if scs
        }

        # Single-line query progress
        line_msg = format_query_progress(
            q_num=len(per_q),
            total_q=len(evaluable),
            score=score_10,
            latency_ms=dt_ms,
            running_acc=running_rec10,
            category=qtype,
            query_text=question,
            extra_metric_label="Rec@10",
        )
        print(line_msg, flush=True)

        # Atomic live progress writer
        for p_file in (progress_file, suite_progress_file):
            write_live_progress(
                progress_file=p_file,
                q_num=len(per_q),
                total_q=len(evaluable),
                category=qtype,
                question_text=question,
                score=score_10,
                latency_ms=dt_ms,
                running_overall=running_rec10,
                running_per_type=running_per_type,
                extra_fields={"benchmark": "LongMemEval-S", "question_id": qid},
            )

        # Periodically trigger garbage collection to control memory growth
        if (idx) % 25 == 0:
            gc.collect()

        # Incremental checkpointing every 5 questions or on first/last
        if (len(per_q) % 5 == 0) or idx == 1 or idx == len(evaluable):
            _save_checkpoint()

    wall = time.perf_counter() - total_t
    agg = {f"recall_any@{k}": mean(per_metric[f"recall_any@{k}"]) for k in KS} if per_metric else {}
    agg.update({f"recall_all@{k}": mean(per_metric[f"recall_all@{k}"]) for k in KS} if per_metric else {})
    agg.update({f"ndcg_any@{k}": mean(per_metric[f"ndcg_any@{k}"]) for k in KS} if per_metric else {})

    # Phase 5: Results Aggregation
    print_stage_banner(5, "Results Aggregation & Verification", f"{len(evaluable)} questions analyzed")

    from eval.bench.metrics import calculate_latency_stats
    lat_stats = calculate_latency_stats(latencies)

    report = {
        "benchmark": "LongMemEval-S-Main-Pipeline",
        "n_questions": len(evaluable),
        "wall_time_s": round(wall, 2),
        "mean_latency_ms": round(lat_stats.get("mean", 0.0), 1),
        "latency_ms": lat_stats,
        "macro_metrics": agg,
        "per_type_recall_at_10": {t: round(mean(scs), 4) for t, scs in per_type_scores.items() if scs},
        "per_type_counts": {t: len(scs) for t, scs in per_type_scores.items()},
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
    parser.add_argument("--resume", action="store_true", help="Resume from existing output or checkpoint file")
    parser.add_argument("--force-reseed", action="store_true", help="Force re-seeding DB")
    args = parser.parse_args()

    print(f"\n{'='*80}", flush=True)
    print("BENCHMARK SUITE: LONGMEMEVAL-S (Main Search Pipeline)", flush=True)
    print(f"{'='*80}", flush=True)

    print_stage_banner(1, "Dataset Loading", f"Path={args.input}")
    corpus = load_corpus(args.input, limit=args.limit)
    print(f"✓ Loaded {len(corpus)} corpus items", flush=True)

    report = run(
        corpus,
        limit=args.limit,
        output_file=args.output,
        resume=args.resume,
        force_reseed=args.force_reseed,
    )

    out_p = Path(args.output)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(out_p, "w") as f:
        json.dump(report, f, indent=2)

    # Clean up temporary checkpoint file on successful completion
    ckpt_file = Path(args.output + ".checkpoint")
    if ckpt_file.exists():
        try:
            ckpt_file.unlink()
        except OSError:
            pass

    overall_r10 = report.get("macro_metrics", {}).get("recall_any@10", 0.0)
    retrieval_recalls = {
        k: v for k, v in report.get("macro_metrics", {}).items() if "recall" in k
    }

    print_summary_report(
        benchmark_name="LongMemEval-S",
        total_q=report.get("n_questions", 0),
        wall_time_s=report.get("wall_time_s", 0.0),
        overall_metric=overall_r10,
        metric_name="RecallAny@10 (Macro)",
        category_scores=report.get("per_type_recall_at_10", {}),
        category_counts=report.get("per_type_counts", {}),
        latency_stats=report.get("latency_ms", {}),
        retrieval_recalls=retrieval_recalls,
        output_path=out_p,
    )



if __name__ == "__main__":
    main()

