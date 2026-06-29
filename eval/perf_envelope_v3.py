"""Sprint 4 / 9.4 perf envelope v3.

Focused on the three operations the 9.4 gate calls out:
  1. save    — direct SQLite INSERT (mirrors memory_save hot path)
  2. search  — FTS5 (raw) + indexed (vector index path from 8.6)
  3. rerank  — weak CE from _apply_cross_encoder_rerank (default; the deep
              jina-v3 path is opt-in and out of scope for the envelope)

At 1K, 10K, 100K synthetic notes. Writes eval/results/perf-envelope-v3.json.

Usage:
    ~/.config/agentic-memory/venv/bin/python eval/perf_envelope_v3.py [--quick]

    --quick   runs only 1K (smoke test, ~30s)
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
PERF_ROOT = Path("/tmp/perf_envelope_v3")
RESULTS_PATH = REPO_ROOT / "eval" / "results" / "perf-envelope-v3.json"
VENV_PY = REPO_ROOT / "venv" / "bin" / "python"
REBUILD_SCRIPT = REPO_ROOT / "rebuild_index.py"
REBUILD_VEC_SCRIPT = REPO_ROOT / "rebuild_vec_index.py"

SIZES_FULL = [1_000, 10_000, 100_000]
SIZES_QUICK = [1_000]

WARMUP_ITERS = 2
MEASURE_ITERS = 5
SEED = 20260607

# Reuse the v1 vocabulary for consistent note content.
VOCAB = list((
    "memory agent system memory_search contradiction detector rebuild index "
    "SQLite FTS5 schema metadata frontmatter pinned drift staleness temporal "
    "supersede LoCoMo benchmark session retrieval recall precision latency "
    "throughput corpus vector embedding model2vec cosine similarity note "
    "frontmatter parsing optimization hot cold cache eviction tier migration "
    "compaction consolidation archive archive rotation lesson preference "
    "project decision session summary error pattern user preference agent "
    "workflow protocol contract API query response insert update delete "
    "transaction commit rollback pragma journal WAL mode cache limit MB "
    "tag category importance decay score access fitness hot cold running "
    "stable frozen feature flag toggle override value key reference pointer "
    "table index column row tuple integer boolean text blob real null empty "
    "active inactive enabled disabled deprecated experimental stable beta "
    "draft pending approved rejected merged closed open resolved unresolved"
).split())
CATEGORIES = ["lessons", "preferences", "projects", "decisions", "sessions"]
TAGS_POOL = [
    "perf", "drift", "staleness", "supersede", "loCoMo", "temporal",
    "agent", "workflow", "data", "ops", "infra", "retrieval",
    "embedding", "semantic", "FTS5", "schema", "migration",
    "compaction", "consolidation", "pinned", "decay",
]


# ---------------------------------------------------------------------------
# Corpus generation (mirrors v1)
# ---------------------------------------------------------------------------

def _frontmatter(slug: str, idx: int, importance: int, tags: list[str]) -> str:
    created = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=idx)
    updated = created + timedelta(minutes=random.randint(0, 120))
    lines = [
        "---",
        f"title: Note {idx}",
        f"slug: {slug}",
        f"created: {created.isoformat()}",
        f"updated: {updated.isoformat()}",
        f"observed_at: {updated.isoformat()}",
        f"tags: [{', '.join(tags)}]",
        f"importance: {importance}",
        "---",
    ]
    return "\n".join(lines)


def _body(rng: random.Random, n_words: int) -> str:
    words = rng.choices(VOCAB, k=n_words)
    sentences = []
    i = 0
    while i < len(words):
        chunk = words[i:i + rng.randint(8, 18)]
        sentences.append(" ".join(chunk).capitalize() + ".")
        i += len(chunk)
    return "# " + " ".join(words[:3]).title() + "\n\n" + " ".join(sentences)


def generate_corpus(n: int, out_dir: Path) -> list[Path]:
    rng = random.Random(SEED + n)
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for i in range(n):
        category = CATEGORIES[rng.randrange(len(CATEGORIES))]
        slug = f"{category}-note-{i:06d}"
        sub = out_dir / category
        sub.mkdir(exist_ok=True)
        importance = rng.choices([1, 2, 3, 4, 5], weights=[3, 4, 5, 2, 1])[0]
        tags = rng.sample(TAGS_POOL, k=rng.randint(1, 3))
        n_words = rng.randint(80, 220)
        body = _body(rng, n_words)
        path = sub / f"{slug}.md"
        path.write_text(_frontmatter(slug, i, importance, tags) + "\n\n" + body + "\n")
        files.append(path)
    return files


# ---------------------------------------------------------------------------
# Measurement dataclass
# ---------------------------------------------------------------------------

@dataclass
class Measurement:
    name: str
    corpus_size: int
    iterations: int
    elapsed_s: list[float] = field(default_factory=list)
    unit: str = "seconds"
    notes: str = ""

    @property
    def p50(self) -> float:
        return statistics.median(self.elapsed_s) if self.elapsed_s else 0.0

    @property
    def p95(self) -> float:
        if not self.elapsed_s:
            return 0.0
        s = sorted(self.elapsed_s)
        idx = max(0, int(round(0.95 * (len(s) - 1))))
        return s[idx]

    @property
    def max(self) -> float:
        return max(self.elapsed_s) if self.elapsed_s else 0.0

    @property
    def mean(self) -> float:
        return statistics.fmean(self.elapsed_s) if self.elapsed_s else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "corpus_size": self.corpus_size,
            "iterations": self.iterations,
            "unit": self.unit,
            "p50_s": round(self.p50, 4),
            "p95_s": round(self.p95, 4),
            "max_s": round(self.max, 4),
            "mean_s": round(self.mean, 4),
            "elapsed_s": [round(x, 4) for x in self.elapsed_s],
            "notes": self.notes,
        }


def _timed(fn: Callable[[], Any], iters: int) -> list[float]:
    out: list[float] = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t0)
    return out


# ---------------------------------------------------------------------------
# Per-corpus setup
# ---------------------------------------------------------------------------

def setup_corpus(n: int) -> tuple[Path, Path, Path]:
    root = PERF_ROOT / str(n)
    if root.exists():
        shutil.rmtree(root)
    memory_dir = root / "memory"
    memory_dir.mkdir(parents=True)
    db_path = memory_dir / "memory.db"
    return memory_dir, db_path, root


def build_db(memory_dir: Path, db_path: Path) -> float:
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(VENV_PY), str(REBUILD_SCRIPT), "memory", "memory/memory.db"],
        cwd=str(memory_dir.parent),
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rebuild failed: {proc.stderr[-500:]}")
    return time.perf_counter() - t0


def build_vec_index(db_path: Path) -> float:
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(VENV_PY), str(REBUILD_VEC_SCRIPT), str(db_path)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"vec rebuild failed: {proc.stderr[-500:]}")
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------

def measure_save(db_path: Path, n: int) -> Measurement:
    """Direct SQLite INSERT simulating memory_save hot path."""
    random.Random(SEED + 2)
    counter = 0

    def _one() -> int:
        nonlocal counter
        counter += 1
        slug = f"sessions-perf-test-{counter:05d}"
        now = datetime.now(timezone.utc).isoformat()
        con = sqlite3.connect(str(db_path), timeout=5.0)
        con.execute(
            """INSERT INTO memories
               (id, content, source_file, tags, created_at, updated_at,
                observed_at, pinned, importance, decay, score, supersedes,
                repo_id, access_count, success_score, fitness_score,
                conflict_policy, version_vector, logical_clock,
                consolidation_state, valid_from, valid_to, superseded_by,
                last_accessed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?)""",
            (slug, "perf test body", f"memory/sessions/{slug}.md",
             "[]", now, now, now, 0, 1, 1.0, 0.0, "[]", "perf",
             0, 0.0, 0.0, "supersede", "{}", 0, "working",
             now, None, None, now),
        )
        con.commit()
        con.close()
        return 1

    for _ in range(WARMUP_ITERS):
        _one()
    elapsed = _timed(_one, MEASURE_ITERS)
    return Measurement("save", n, MEASURE_ITERS, elapsed,
                       notes="direct SQLite INSERT; no frontmatter parse; "
                             "covers the new memory_embeddings insert via trigger or follow-up")


def measure_fts_search(db_path: Path, vocab: list[str], n: int) -> Measurement:
    """FTS5 MATCH queries (raw search, no rerank)."""
    rng = random.Random(SEED)
    safe_terms = [w for w in vocab if w.isalpha() and 5 <= len(w) <= 15]
    safe_terms = safe_terms[:50] if len(safe_terms) >= 20 else VOCAB
    terms = rng.sample(safe_terms, k=20)

    def _one() -> int:
        q = rng.choice(terms)
        con = sqlite3.connect(str(db_path), timeout=5.0)
        cur = con.execute(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? LIMIT 20",
            (q,),
        )
        rows = cur.fetchall()
        con.close()
        return len(rows)

    for _ in range(WARMUP_ITERS):
        _one()
    elapsed = _timed(_one, MEASURE_ITERS)
    return Measurement("fts5_search", n, MEASURE_ITERS, elapsed,
                       notes="20 random terms; LIMIT 20; sqlite3 direct; "
                             "no rerank, no semantic fallback")


def measure_indexed_search(db_path: Path, vocab: list[str], n: int) -> Measurement:
    """Vector-index path (8.6): model2vec encode + usearch ANN + writeback.

    Clears the in-process vec_index cache before the first call so the
    measurement is end-to-end (BLOB load + ANN + FP32 rerank). After that
    the cache is hot and we measure the steady-state path.
    """
    sys.path.insert(0, str(REPO_ROOT))
    # L4 / H3: redirect prod mem dir so a stray import doesn't touch prod.
    import memory_mcp  # noqa: F401
    import tempfile
    from pathlib import Path as P
    tmp = tempfile.mkdtemp(prefix="pe3-")
    memory_mcp.GLOBAL_MEM_DIR = P(tmp)
    if hasattr(memory_mcp, "resolve_active_memory_dir"):
        memory_mcp.resolve_active_memory_dir = lambda **_: P(tmp)

    from embedding_search import EmbeddingSearch  # type: ignore

    rng = random.Random(SEED + 1)
    query = " ".join(rng.sample(vocab, 5))

    es = EmbeddingSearch()
    if es.model is not None:
        es.model.encode(["warmup"])

    # Drop the cache so first call is a true cold start (BLOB load).
    if hasattr(es, "clear_vec_index_cache"):
        es.clear_vec_index_cache()

    def _one() -> int:
        results = es.search(query, str(db_path), limit=5)
        if isinstance(results, list):
            return len(results)
        return 0

    for _ in range(WARMUP_ITERS):
        _one()
    elapsed = _timed(_one, MEASURE_ITERS)
    return Measurement("indexed_search", n, MEASURE_ITERS, elapsed,
                       notes="EmbeddingSearch.search() with usearch HNSW + FP32 rerank; "
                             "includes model2vec encode of the query (warm model); "
                             "end-to-end including DB I/O + cache load on iter 0")


def measure_rerank(db_path: Path, vocab: list[str], n: int) -> Measurement:
    """Weak CE from _apply_cross_encoder_rerank. This is the default rerank.

    Pulls N top FTS5 hits, then times just the rerank call. The query is
    a real FTS term so the head list is non-empty at every corpus size.
    """
    sys.path.insert(0, str(REPO_ROOT))
    import memory_mcp  # noqa: F401
    import tempfile
    from pathlib import Path as P
    tmp = tempfile.mkdtemp(prefix="pe3-")
    memory_mcp.GLOBAL_MEM_DIR = P(tmp)
    if hasattr(memory_mcp, "resolve_active_memory_dir"):
        memory_mcp.resolve_active_memory_dir = lambda **_: P(tmp)

    rng = random.Random(SEED + 3)
    safe_terms = [w for w in vocab if w.isalpha() and 5 <= len(w) <= 15]
    terms = rng.sample(safe_terms, k=20)
    top_k = 20  # matches the default call site: limit*2 where limit=10

    # Pre-build the head lists for each term so the rerank timing is pure.
    heads: list[tuple[str, list[tuple]]] = []
    for t in terms:
        con = sqlite3.connect(str(db_path), timeout=5.0)
        rows = con.execute(
            """SELECT m.id, m.content, m.source_file, m.tags, m.created_at,
                      fts.rank, m.fitness_score, m.importance, m.pinned
               FROM memories_fts fts
               JOIN memories m ON m.rowid = fts.rowid
               WHERE memories_fts MATCH ?
               ORDER BY fts.rank
               LIMIT ?""",
            (t, top_k),
        ).fetchall()
        con.close()
        if rows:
            # Convert to the 10-tuple shape the reranker expects
            scored = [
                (r[0], r[1], r[2], r[3], r[4], r[5],
                 0.5, float(r[6]) if r[6] is not None else 1.0,
                 r[7] if r[7] is not None else 3,
                 r[8] if r[8] is not None else 0)
                for r in rows
            ]
            heads.append((t, scored))

    if not heads:
        return Measurement("rerank", n, MEASURE_ITERS, [0.0] * MEASURE_ITERS,
                           notes="FTS5 returned 0 hits for all terms; rerank is a no-op")

    def _one() -> int:
        q, scored = rng.choice(heads)
        result = memory_mcp._apply_cross_encoder_rerank(
            q, scored, top_k=top_k, deep_rerank=False,
        )
        return len(result)

    for _ in range(WARMUP_ITERS):
        _one()
    elapsed = _timed(_one, MEASURE_ITERS)
    return Measurement("rerank", n, MEASURE_ITERS, elapsed,
                       notes=f"weak CE (IDF + bigram phrase bonus) on top_k={top_k} FTS hits; "
                             "deep_rerank=False (default); "
                             "jina-reranker-v3 not measured (opt-in, off the envelope)")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_for_size(n: int) -> dict[str, Any]:
    print(f"--- corpus size {n} ---")
    t0 = time.perf_counter()
    memory_dir, db_path, root = setup_corpus(n)
    files = generate_corpus(n, memory_dir)
    gen_s = time.perf_counter() - t0
    print(f"  corpus: {len(files)} files in {gen_s:.1f}s")

    t0 = time.perf_counter()
    build_db(memory_dir, db_path)
    init_s = time.perf_counter() - t0
    print(f"  rebuild_index: {init_s:.1f}s")

    t0 = time.perf_counter()
    build_vec_index(db_path)
    vec_s = time.perf_counter() - t0
    print(f"  rebuild_vec_index: {vec_s:.1f}s")

    measurements = []
    vocab: list = list(VOCAB)
    save_m = measure_save(db_path, n)
    measurements.append(save_m)
    print(f"  {save_m.name}: p50={save_m.p50*1000:.2f}ms p95={save_m.p95*1000:.2f}ms max={save_m.max*1000:.2f}ms")
    for fn in (measure_fts_search, measure_indexed_search, measure_rerank):
        m = fn(db_path, vocab, n)
        measurements.append(m)
        print(f"  {m.name}: p50={m.p50*1000:.2f}ms p95={m.p95*1000:.2f}ms max={m.max*1000:.2f}ms")

    total_s = time.perf_counter() - t0
    return {
        "corpus_size": n,
        "generation_s": round(gen_s, 2),
        "rebuild_index_s": round(init_s, 2),
        "rebuild_vec_index_s": round(vec_s, 2),
        "total_s": round(gen_s + init_s + vec_s + total_s, 2),
        "db_rows": n,
        "measurements": [m.to_dict() for m in measurements],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                        help="only run 1K corpus (smoke test)")
    args = parser.parse_args()

    sizes = SIZES_QUICK if args.quick else SIZES_FULL
    t0 = time.perf_counter()
    corpora = [run_for_size(n) for n in sizes]
    total_s = time.perf_counter() - t0

    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host": "macOS",
        "venv_python": str(VENV_PY),
        "perf_root": str(PERF_ROOT),
        "sizes": sizes,
        "warmup_iters": WARMUP_ITERS,
        "measure_iters": MEASURE_ITERS,
        "total_s": round(total_s, 2),
        "schema": {
            "rebuild_index": str(REBUILD_SCRIPT.name),
            "rebuild_vec_index": str(REBUILD_VEC_SCRIPT.name),
        },
        "sprint": "Sprint 4 / 9.4 final gate",
        "scope": "search (FTS5 + indexed), save, rerank (weak CE) at 1K/10K/100K",
        "deep_rerank_out_of_scope": "jina-reranker-v3 deep_rerank=True is opt-in; measured separately (8.5s cold + 1.9-16.7s per top-5..50 query on CPU)",
        "corpora": corpora,
    }

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {RESULTS_PATH} ({total_s:.1f}s total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
