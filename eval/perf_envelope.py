#!/usr/bin/env python3
"""Performance envelope benchmark for the agentic-memory system.

Generates three synthetic corpora (1K / 10K / 100K notes) with valid frontmatter
and bodies, builds a fresh SQLite DB for each, then measures latency of the
critical read/write/analysis operations. Reports p50 / p95 / max latency,
throughput, and writes results to eval/results/perf-envelope.json.

Measurements per corpus:
  1. FTS5 search          (sqlite MATCH queries, 20 random terms)
  2. Semantic search      (model2vec, 5 queries)
  3. Save                 (direct SQLite INSERT simulating memory_save)
  4. Rebuild index        (rebuild_index.py on the full corpus)
  5. Contradiction (phrase)  (contradiction_detector.py --mode=phrase)
  6. Contradiction (semantic) (--mode=semantic, skipped >=10K to avoid O(n^2))
  7. Pinned decay check   (pinned_decay.py --dry-run)

At larger corpus sizes some measurements are skipped to keep total wall time
manageable. Skipped operations are recorded in the results JSON.

Usage:
    ~/.config/agentic-memory/venv/bin/python eval/perf_envelope.py [--quick]

    --quick   runs only the 1K corpus (smoke test)
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PERF_ROOT = Path("/tmp/perf_envelope_test")
RESULTS_PATH = REPO_ROOT / "eval" / "results" / "perf-envelope.json"
VENV_PY = REPO_ROOT / "venv" / "bin" / "python"
REBUILD_SCRIPT = REPO_ROOT / "rebuild_index.py"
CONTRADICTION_SCRIPT = REPO_ROOT / "contradiction_detector.py"
PINNED_DECAY_SCRIPT = REPO_ROOT / "pinned_decay.py"

SIZES_FULL = [1_000, 10_000, 100_000]
SIZES_QUICK = [1_000]

# Vocabulary used to construct varied note bodies
VOCAB = (
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
).split()
assert len(VOCAB) >= 80, f"need rich vocabulary, got {len(VOCAB)}"

CATEGORIES = ["lessons", "preferences", "projects", "decisions", "sessions"]
TAGS_POOL = [
    "perf", "drift", "staleness", "supersede", "loCoMo", "temporal",
    "agent", "workflow", "data", "ops", "infra", "retrieval",
    "embedding", "semantic", "FTS5", "schema", "migration",
    "compaction", "consolidation", "pinned", "decay",
]

WARMUP_ITERS = 2
MEASURE_ITERS = 5
SEED = 20260607


# ---------------------------------------------------------------------------
# Corpus generation
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
    """Construct a body of approximately n_words drawn from VOCAB."""
    words = rng.choices(VOCAB, k=n_words)
    # Insert sentence-like punctuation
    sentences = []
    i = 0
    while i < len(words):
        chunk = words[i:i + rng.randint(8, 18)]
        sentences.append(" ".join(chunk).capitalize() + ".")
        i += len(chunk)
    return "# " + " ".join(words[:3]).title() + "\n\n" + " ".join(sentences)


def generate_corpus(n: int, out_dir: Path) -> list[Path]:
    """Generate n synthetic note files under out_dir, return the file paths."""
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
# Timing helpers
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
    """Create corpus at PERF_ROOT/<n>/memory, return (memory_dir, db_path, rebuild_args)."""
    root = PERF_ROOT / str(n)
    if root.exists():
        shutil.rmtree(root)
    memory_dir = root / "memory"
    memory_dir.mkdir(parents=True)
    db_path = memory_dir / "memory.db"
    return memory_dir, db_path, root


def build_db(memory_dir: Path, db_path: Path) -> float:
    """Run rebuild_index.py to build the DB; return elapsed seconds."""
    t0 = time.perf_counter()
    # rebuild_index.py expects (source_dir, db_path) as relative paths from cwd.
    # The standard call is: python rebuild_index.py memory memory/memory.db
    proc = subprocess.run(
        [str(VENV_PY), str(REBUILD_SCRIPT), "memory", "memory/memory.db"],
        cwd=str(memory_dir.parent),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rebuild failed: {proc.stderr[-500:]}")
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------

def measure_fts_search(db_path: Path, vocab: list[str], n: int) -> Measurement:
    """20 random FTS5 MATCH queries over the corpus."""
    rng = random.Random(SEED)
    # Use only single-word terms to avoid FTS5 syntax errors
    safe_terms = [w for w in vocab if w.isalpha() and 5 <= len(w) <= 15]
    safe_terms = safe_terms[:50] if len(safe_terms) >= 20 else VOCAB
    terms = rng.sample(safe_terms, k=20)
    queries = terms

    def _one() -> int:
        q = rng.choice(queries)
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
                       notes="20 random terms; LIMIT 20; sqlite3 direct")


def measure_semantic_search(vocab: list[str], n: int, db_path: Path) -> Measurement:
    """Single model2vec query via embedding_search.py (full search path).
    Encodes the whole DB per call -- this is the current implementation cost."""
    sys.path.insert(0, str(REPO_ROOT))
    from infra.embedding_search import EmbeddingSearch  # type: ignore

    rng = random.Random(SEED + 1)
    query = " ".join(rng.sample(vocab, 5))

    es = EmbeddingSearch()
    # Warm embedding model
    if es.model is not None:
        es.model.encode(["warmup"])

    def _one() -> int:
        results = es.search(query, str(db_path), limit=5)
        if isinstance(results, list):
            return len(results)
        return 0

    for _ in range(WARMUP_ITERS):
        _one()
    elapsed = _timed(_one, MEASURE_ITERS)
    return Measurement("semantic_search", n, MEASURE_ITERS, elapsed,
                       notes="1 query x limit=5 per iteration; potion-base-8M; "
                             "encodes all rows in DB")


def measure_save(db_path: Path, n: int) -> Measurement:
    """Simulate memory_save: INSERT 1 row + write file."""
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
                       notes="direct SQLite INSERT; no frontmatter parse")


def measure_rebuild(memory_dir: Path, db_path: Path, n: int) -> Measurement:
    """Full rebuild of the corpus from disk."""

    def _one() -> None:
        build_db(memory_dir, db_path)

    for _ in range(WARMUP_ITERS):
        _one()
    elapsed = _timed(_one, MEASURE_ITERS)
    return Measurement("rebuild", n, MEASURE_ITERS, elapsed,
                       notes="rebuild_index.py on full corpus; reads from disk")


def measure_contradiction(memory_dir: Path, mode: str, n: int,
                          subsample: int = 200) -> Measurement:
    """Run contradiction_detector.py CLI on a subsample of the DB.
    Detector is O(n^2); we cap the input size to keep iteration time bounded
    and record the subsample size in the notes.
    """
    # Create a subsample memory dir: copy `subsample` source files, run
    # rebuild on it, then run the detector. This keeps the schema correct
    # because rebuild_index.py is the source of truth for it.
    sub_dir = memory_dir.parent / f"_contradiction_sub_{mode}"
    if sub_dir.exists():
        shutil.rmtree(sub_dir)
    sub_dir.mkdir()
    sub_memory = sub_dir / "memory"
    sub_memory.mkdir()

    src_db = sqlite3.connect(str(memory_dir / "memory.db"), timeout=10.0)
    sample = src_db.execute(
        "SELECT source_file, content FROM memories "
        "ORDER BY RANDOM() LIMIT ?",
        (subsample,),
    ).fetchall()
    src_db.close()

    for src_rel, _content in sample:
        # src_rel is "memory/lessons/foo.md" -- copy file by relative path
        src_path = memory_dir.parent / src_rel
        if not src_path.exists():
            continue
        dst_path = sub_dir / src_rel
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)

    # Build DB from the copied files
    build_db(sub_memory, sub_memory / "memory.db")

    def _one() -> int:
        proc = subprocess.run(
            [str(VENV_PY), str(CONTRADICTION_SCRIPT),
             str(sub_memory), f"--mode={mode}"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"contradiction {mode} failed (rc={proc.returncode}): "
                f"{proc.stderr[-300:]}"
            )
        return len(proc.stdout)

    for _ in range(WARMUP_ITERS):
        _one()
    elapsed = _timed(_one, MEASURE_ITERS)
    shutil.rmtree(sub_dir, ignore_errors=True)
    return Measurement(
        f"contradiction_{mode}", n, MEASURE_ITERS, elapsed,
        notes=f"contradiction_detector.py --mode={mode} on {subsample}-note subsample "
              f"(detector is O(n^2); full corpus would be ~{(n**2) // (subsample**2)}x longer)"
    )


def measure_pinned_decay(db_path: Path, n: int) -> Measurement:
    """Run pinned decay logic against the perf DB (script is hard-coded to
    GLOBAL_MEM_DIR; we re-implement the hot path here for accurate measurement
    on the perf DB without touching production)."""

    def _one() -> dict:
        con = sqlite3.connect(str(db_path), timeout=5.0)
        rows = con.execute(
            "SELECT id, last_accessed, access_count FROM memories "
            "WHERE pinned = 1"
        ).fetchall()
        con.close()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        candidates = []
        for note_id, last_accessed, access_count in rows:
            if not last_accessed:
                continue
            try:
                last = datetime.fromisoformat(last_accessed)
            except (TypeError, ValueError):
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days = (now - last).days
            psi = days / max(1, access_count)
            if psi > 60.0 and days > 180:
                candidates.append((note_id, "auto_unpin", psi, days))
            elif psi > 30.0 and days > 365:
                candidates.append((note_id, "review", psi, days))
        return {"pinned": len(rows), "candidates": len(candidates)}

    for _ in range(WARMUP_ITERS):
        _one()
    elapsed = _timed(_one, MEASURE_ITERS)
    return Measurement("pinned_decay_dry_run", n, MEASURE_ITERS, elapsed,
                       notes="library call: SELECT pinned notes + per-note psi check")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_for_size(n: int) -> dict[str, Any]:
    print(f"\n=== Corpus: {n:,} notes ===", flush=True)
    t_start = time.perf_counter()

    memory_dir, db_path, _root = setup_corpus(n)
    print(f"  generating {n} files...", flush=True)
    t_gen = time.perf_counter()
    files = generate_corpus(n, memory_dir)
    gen_s = time.perf_counter() - t_gen
    print(f"  generation: {gen_s:.1f}s ({len(files):,} files)", flush=True)

    print("  building DB (initial)...", flush=True)
    initial_build_s = build_db(memory_dir, db_path)
    print(f"  initial build: {initial_build_s:.1f}s", flush=True)

    con = sqlite3.connect(str(db_path))
    row_count = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    fts_count = con.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
    con.close()
    print(f"  db rows: {row_count:,} (FTS5: {fts_count:,})", flush=True)

    # Pick vocabulary from actual corpus for search queries
    con = sqlite3.connect(str(db_path))
    sample = con.execute(
        "SELECT content FROM memories ORDER BY RANDOM() LIMIT 50"
    ).fetchall()
    con.close()
    sample_words = []
    for (txt,) in sample:
        for w in txt.split():
            w = w.strip(".,:;()[]\"'").lower()
            if 5 <= len(w) <= 20 and w.isalpha():
                sample_words.append(w)
    vocab = list(dict.fromkeys(sample_words))[:200]
    if len(vocab) < 20:
        vocab = list(VOCAB)

    vocab = [str(w) for w in vocab]
    measurements: list[Measurement] = []

    def _do(name: str, fn: Callable[[], Measurement]) -> None:
        print(f"  measuring {name}...", end="", flush=True)
        try:
            m = fn()
            measurements.append(m)
            print(f" p50={m.p50*1000:.1f}ms p95={m.p95*1000:.1f}ms", flush=True)
        except Exception as e:
            print(f" SKIPPED ({e})", flush=True)
            measurements.append(Measurement(name, n, 0, [], notes=f"skipped: {e}"))

    _do("fts5_search", lambda: measure_fts_search(db_path, vocab, n))
    _do("semantic_search", lambda: measure_semantic_search(vocab, n, db_path))
    _do("save", lambda: measure_save(db_path, n))
    _do("rebuild", lambda: measure_rebuild(memory_dir, db_path, n))
    _do("pinned_decay_dry_run", lambda: measure_pinned_decay(db_path, n))

    # Contradiction phrase is O(n^2) -- skip at >=10K
    if n <= 1_000:
        _do("contradiction_phrase", lambda: measure_contradiction(memory_dir, "phrase", n))
    else:
        measurements.append(Measurement(
            "contradiction_phrase", n, 0, [],
            notes="skipped (O(n^2); corpus too large)"
        ))

    # Contradiction semantic is O(n^2) with embedding cost -- skip >= 10K
    if n <= 1_000:
        _do("contradiction_semantic", lambda: measure_contradiction(memory_dir, "semantic", n))
    else:
        measurements.append(Measurement(
            "contradiction_semantic", n, 0, [],
            notes="skipped (O(n^2) + embedding cost; corpus too large)"
        ))

    total_s = time.perf_counter() - t_start
    print(f"  total: {total_s:.1f}s", flush=True)

    return {
        "corpus_size": n,
        "generation_s": round(gen_s, 2),
        "initial_build_s": round(initial_build_s, 2),
        "total_s": round(total_s, 2),
        "db_rows": row_count,
        "measurements": [m.to_dict() for m in measurements],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="Run only the 1K corpus (smoke test).")
    ap.add_argument("--sizes", type=str, default=None,
                    help="Comma-separated list of corpus sizes to run "
                         "(e.g. '1000,10000'). Overrides --quick.")
    ap.add_argument("--keep-data", action="store_true",
                    help="Do not delete /tmp/perf_envelope_test after run.")
    ap.add_argument("--iter-scale", type=float, default=1.0,
                    help="Scale MEASURE_ITERS by this factor (use <1 for large "
                         "corpora to bound wall time).")
    args = ap.parse_args()

    if not VENV_PY.exists():
        print(f"venv missing: {VENV_PY}", file=sys.stderr)
        return 2

    if args.sizes:
        sizes = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]
    elif args.quick:
        sizes = SIZES_QUICK
    else:
        sizes = SIZES_FULL

    # Scale down iter counts for large corpora to bound wall time
    global MEASURE_ITERS, WARMUP_ITERS
    MEASURE_ITERS = max(2, int(round(MEASURE_ITERS * args.iter_scale)))
    WARMUP_ITERS = max(1, min(WARMUP_ITERS, MEASURE_ITERS - 1))

    PERF_ROOT.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    results: list[dict[str, Any]] = []
    for n in sizes:
        try:
            results.append(run_for_size(n))
        except Exception as e:
            print(f"corpus {n} FAILED: {e}", file=sys.stderr)
            results.append({
                "corpus_size": n,
                "error": str(e),
            })

    # Write results
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host": "macOS",
        "venv_python": str(VENV_PY),
        "perf_root": str(PERF_ROOT),
        "sizes": sizes,
        "warmup_iters": WARMUP_ITERS,
        "measure_iters": MEASURE_ITERS,
        "total_s": round(time.perf_counter() - t0, 2),
        "corpora": results,
    }
    with RESULTS_PATH.open("w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nResults: {RESULTS_PATH}", flush=True)

    if not args.keep_data:
        try:
            shutil.rmtree(PERF_ROOT)
        except OSError as e:
            print(f"warn: cleanup failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
