#!/usr/bin/env python3
"""eval/embedding_benchmark.py

Measures embedding search performance and writes results to
eval/results/bench-embeddings.json in the format consumed by the
Streamlit dashboard.

Metrics:
  - embedding_speed: vec/s for batch encode
  - memory_index_build_s: usearch HNSW build time for memory-level vectors
  - chunk_index_build_s: usearch HNSW build time for chunk-level vectors
  - search_recall: ANN top-k recall vs brute-force exact cosine
  - search_latency_ms: p50/p95/max for ANN vs full-scan across corpus sizes
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = REPO_ROOT / "eval" / "results" / "bench-embeddings.json"
sys.path.insert(0, str(REPO_ROOT))

from infra.embedding_search import (
    get_embedding_search,
    _cache_text,
    _content_hash,
    chunk_memory,
)


def _rebuild_vec_index(db_path: str, force: bool = False):
    import importlib.util

    rebuild_path = REPO_ROOT / "rebuild_vec_index.py"
    if not rebuild_path.exists():
        raise FileNotFoundError(f"rebuild_vec_index.py not found at {rebuild_path}")
    spec = importlib.util.spec_from_file_location(
        "rebuild_vec_index", str(rebuild_path)
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load spec for {rebuild_path}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as e:
        raise RuntimeError(f"Failed to exec rebuild_vec_index module: {e}") from e
    return mod.rebuild_vec_index(db_path, force=force)


def _rebuild_chunk_vec_index(db_path: str, force: bool = False):
    import importlib.util

    rebuild_path = REPO_ROOT / "rebuild_vec_index.py"
    if not rebuild_path.exists():
        raise FileNotFoundError(f"rebuild_vec_index.py not found at {rebuild_path}")
    spec = importlib.util.spec_from_file_location(
        "rebuild_vec_index", str(rebuild_path)
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load spec for {rebuild_path}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as e:
        raise RuntimeError(f"Failed to exec rebuild_vec_index module: {e}") from e
    return mod.rebuild_chunk_vec_index(db_path, force=force)


def _build_corpus(size: int) -> list[dict]:
    tokens = [
        "python", "memory", "embeddings", "vector", "search", "database",
        "usearch", "sqlite", "chunk", "index", "neural", "retrieval",
        "semantic", "cosine", "hnsw", "model2vec", "context", "knowledge",
    ]
    return [
        {
            "id": f"note-{i}",
            "content": (
                f"---\ncategory: lessons\ntitle_slug: bench-{i}\n---\n\n"
                f"# Benchmark Note {i}\n\n"
                + " ".join(tokens[i % len(tokens)] for _ in range(30))
                + f"\n\n## Section {i}\n\n"
                + " ".join(tokens[(i + 3) % len(tokens)] for _ in range(30))
            ),
        }
        for i in range(size)
    ]


def _populate_db(db_path: str, corpus: list[dict]):
    import sqlite3
    from infra.db_migrations import run_schema_setup
    from search_pipeline import _qw5_index_chunks_for

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    run_schema_setup(conn)
    conn.commit()

    now = time.time()
    rows = []
    chunk_rows = []
    for note in corpus:
        rows.append((
            note["id"], note["content"], "bench.md", "[]",
            now, now, now, 3, 1.0, "{}", "lessons"
        ))
        chunks = chunk_memory(note["content"])
        for j, ch in enumerate(chunks):
            chunk_rows.append((note["id"], j, ch["start_offset"], ch["end_offset"], ch["content"]))
    conn.executemany(
        "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, "
        "observed_at, importance, score, metadata, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) VALUES (?, ?, ?, ?, ?)",
        chunk_rows,
    )
    conn.commit()
    conn.close()


def _stats(values: list[float]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    return {
        "p50_ms": s[len(s) // 2],
        "p95_ms": s[int(len(s) * 0.95)],
        "max_ms": s[-1],
        "mean_ms": round(statistics.mean(values), 3),
        "samples": len(values),
    }


def _measure_recall_and_latency(es, db_path: str, memory_rows: list, ann_k: int = 20) -> dict:
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    dim = int(es.model.dim)

    # normalise to list of (mid, content) tuples
    if memory_rows and isinstance(memory_rows[0], dict):
        memory_rows = [(r["id"], r["content"]) for r in memory_rows]

    # Load cached embeddings
    cached = {}
    try:
        for mid, chash, emb_blob in conn.execute(
            "SELECT memory_id, content_hash, embedding FROM memory_embeddings"
        ).fetchall():
            cached[mid] = (chash, emb_blob)
    except Exception:
        pass

    # Build ground-truth vectors for all memories
    gt_vecs = []
    gt_mids = []
    for mid, content in memory_rows:
        text = _cache_text(content)
        chash = _content_hash(text)
        entry = cached.get(mid)
        if entry and entry[0] == chash:
            try:
                v = es.np.frombuffer(entry[1], dtype=es.np.float32)
                if v.size == dim:
                    gt_vecs.append(v)
                    gt_mids.append(mid)
                    continue
            except Exception:
                pass
        gt_vecs.append(es.model.encode([text])[0])
        gt_mids.append(mid)

    # Pick query from middle of corpus
    query_mid, query_content = memory_rows[len(memory_rows) // 2]
    query_vec = es.model.encode([_cache_text(query_content)])[0]

    # Full-scan ground truth
    mat = es.np.stack(gt_vecs) if gt_vecs else es.np.empty((0, dim), dtype=es.np.float32)
    sims = mat @ query_vec
    gt_top = es.np.argsort(sims)[::-1][:ann_k]
    gt_ids = {gt_mids[i] for i in gt_top}

    # ANN search via index
    idx, meta = es._load_vec_index(db_path, conn)
    ann_ids = set()
    ann_latencies = []
    if idx is not None and meta and meta.get("n_vectors", 0) > 0:
        for _ in range(5):
            t0 = time.time()
            matches = idx.search(query_vec, ann_k)
            ann_latencies.append((time.time() - t0) * 1000.0)
        key_map = {}
        try:
            candidate_keys = [int(k) for k in matches.keys.tolist()]
            placeholders = ",".join("?" for _ in candidate_keys)
            key_map = {int(k): mid for k, mid in conn.execute(
                f"SELECT key, memory_id FROM memory_vec_keys WHERE key IN ({placeholders})",
                candidate_keys,
            ).fetchall()}
        except Exception:
            pass
        ann_ids = {key_map[int(k)] for k in candidate_keys if int(k) in key_map}

    # Full-scan latency
    fs_latencies = []
    for _ in range(5):
        t0 = time.time()
        try:
            es._search_full_scan(conn, _cache_text(query_content), ann_k)
        except Exception:
            pass
        fs_latencies.append((time.time() - t0) * 1000.0)

    recall = len(ann_ids & gt_ids) / len(gt_ids) if gt_ids else None
    conn.close()
    return {
        "recall_at_20": round(recall, 4) if recall is not None else None,
        "ann_latency_ms": _stats(ann_latencies),
        "full_scan_latency_ms": _stats(fs_latencies),
    }


def run_bench(quick: bool = False) -> dict:
    sizes = [50] if quick else [100, 500, 1000]
    corpora = {}
    tmpdirs = {}

    try:
        for size in sizes:
            tmpdir = Path(tempfile.mkdtemp(prefix=f"bench-emb-{size}-"))
            tmpdirs[size] = tmpdir
            db_path = str(tmpdir / "memory.db")
            corpus = _build_corpus(size)
            corpora[size] = corpus
            _populate_db(db_path, corpus)

        es = get_embedding_search()
        if es.model is None:
            raise RuntimeError("Embedding model unavailable")

        # Measure embedding speed
        texts = [_cache_text(corpora[sizes[0]][i]["content"]) for i in range(min(50, len(corpora[sizes[0]])))]
        t0 = time.time()
        batch = es.model.encode(texts)
        batch_s = time.time() - t0
        embed_speed = {
            "batch_vec_per_s": round(len(texts) / batch_s, 2) if batch_s > 0 else 0,
            "texts_encoded": len(texts),
            "dim": int(es.model.dim),
        }

        # Build ANN index for each corpus size
        corpus_results = []
        for size in sizes:
            db_path = str(tmpdirs[size] / "memory.db")
            memory_rows = corpora[size]

            # Rebuild memory-level ANN index
            t0 = time.time()
            try:
                _rebuild_vec_index(db_path, force=True)
            except Exception as e:
                print(f"Warning: rebuild_vec_index failed for size={size}: {e}", file=sys.stderr)
            mem_idx_s = time.time() - t0

            # Rebuild chunk-level ANN index
            t0 = time.time()
            try:
                _rebuild_chunk_vec_index(db_path, force=True)
            except Exception as e:
                print(f"Warning: rebuild_chunk_vec_index failed for size={size}: {e}", file=sys.stderr)
            chunk_idx_s = time.time() - t0

            recall_lat = _measure_recall_and_latency(es, db_path, memory_rows, ann_k=20)

            corpus_results.append({
                "corpus_size": size,
                "db_rows": size,
                "memory_index_build_s": round(mem_idx_s, 3),
                "chunk_index_build_s": round(chunk_idx_s, 3),
                "recall_at_20": recall_lat["recall_at_20"],
                "ann_latency_ms": recall_lat["ann_latency_ms"],
                "full_scan_latency_ms": recall_lat["full_scan_latency_ms"],
            })

        return {
            "benchmark": "embeddings",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host": os.uname().nodename,
            "venv_python": sys.executable,
            "model_id": "BAAI/bge-base-en-v1.5",
            "embedding_speed": embed_speed,
            "corpora": corpus_results,
        }
    finally:
        for d in tmpdirs.values():
            import shutil
            shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Embedding performance benchmark")
    parser.add_argument("--quick", action="store_true", help="50 notes only")
    parser.add_argument("--output", type=str, default=str(RESULTS_PATH))
    args = parser.parse_args()

    data = run_bench(quick=args.quick)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2))
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
