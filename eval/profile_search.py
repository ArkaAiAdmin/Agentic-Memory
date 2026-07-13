#!/usr/bin/env python3
"""Profile the 12-phase search pipeline to find bottleneck phases.

Usage:
    venv/bin/python eval/profile_search.py [query] [limit]
"""
import json, sys, time, sqlite3, tempfile, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# Monkey-patch to capture phase latencies
import search.orchestrator as orch

_original_record = orch._record_phase_latency
_phase_times: list[tuple[str, float]] = []

def _patched_record(name, t0):
    elapsed = (time.time() - t0) * 1000
    _phase_times.append((name, elapsed))
    _original_record(name, t0)

orch._record_phase_latency = _patched_record

from search.orchestrator import search_memories
from eval._fixtures import bootstrap_temp_db_clean

def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "Admon Sunday rotation shift"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    print(f"Query: {query}")
    print(f"Limit: {limit}")
    print()

    # Bootstrap a temp DB with LongMemEval data
    tmpdir = tempfile.mkdtemp(prefix="profile_search_")
    db_path = Path(tmpdir) / "memory.db"
    bootstrap_temp_db_clean(db_path)
    print(f"DB: {db_path}")

    # Seed sessions from LongMemEval dataset
    print("Seeding sessions...")
    with open(str(HERE / "longmemeval_s" / "longmemeval_s_cleaned.json")) as f:
        corpus = json.load(f)
    all_session_ids = set()
    session_dates = {}
    for q in corpus:
        for sid in q.get("haystack_session_ids", []):
            all_session_ids.add(sid)
        if "haystack_sessions" in q:
            for s in q["haystack_sessions"]:
                if isinstance(s, dict):
                    sid = s.get("id", "")
                    all_session_ids.add(sid)
                    if "date" in s:
                        session_dates[sid] = s["date"]
    print(f"  Found {len(all_session_ids)} unique session IDs")

    # Load actual session content from the oracle
    with open(str(HERE / "longmemeval_s" / "longmemeval_oracle.json")) as f:
        oracle = json.load(f)
    session_content = {}
    for q in oracle:
        if "haystack_sessions" in q:
            for s in q["haystack_sessions"]:
                if isinstance(s, list):
                    # s is a list of turns: [{"role": ..., "content": ..., "has_answer": ...}]
                    content = "\n".join(t.get("content", "") for t in s if isinstance(t, dict))
                    # Use first turn's has_answer to identify gold sessions
                    session_id = None
                    for t in s:
                        if isinstance(t, dict) and t.get("has_answer"):
                            # Try to find a session ID from the turns
                            break
                    # We need a stable ID - use the session index within this question
                    # Actually, we need to match by haystack_session_ids
                elif isinstance(s, dict):
                    sid = s.get("id", "")
                    content = s.get("content", "")
                    session_content[sid] = content
    print(f"  Loaded content for {len(session_content)} sessions from oracle")

    # Also load from cleaned.json which has haystack_session_ids
    # Build a mapping from session_id to content
    # The oracle haystack_sessions are indexed by question, not by session_id
    # We need to match them up
    with open(str(HERE / "longmemeval_s" / "longmemeval_s_cleaned.json")) as f:
        cleaned = json.load(f)

    # Build session_id -> content mapping from oracle
    for q in oracle:
        if "haystack_session_ids" in q and "haystack_sessions" in q:
            sids = q["haystack_session_ids"]
            sessions = q["haystack_sessions"]
            for i, sid in enumerate(sids):
                if i < len(sessions):
                    s = sessions[i]
                    if isinstance(s, list):
                        content = "\n".join(t.get("content", "") for t in s if isinstance(t, dict))
                        session_content[sid] = content
                    elif isinstance(s, dict):
                        session_content[sid] = s.get("content", "")
    print(f"  After matching: {len(session_content)} sessions with content")

    # Insert sessions into DB
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    seeded = 0
    for sid in all_session_ids:
        content = session_content.get(sid, f"Session {sid}")
        obs = session_dates.get(sid, "2023-06-01")
        conn.execute(
            """INSERT OR IGNORE INTO memories
               (id, content, source_file, category, tags, created_at, updated_at,
                observed_at, pinned, importance, tenant_id)
               VALUES (?, ?, ?, 'sessions', '[]', datetime('now'), datetime('now'),
                       ?, 0, 3, 'longmemeval')""",
            (sid, content, f"longmemeval/{sid}", obs),
        )
        seeded += 1
    conn.commit()
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()
    print(f"  Seeded {seeded} sessions. DB size: {db_path.stat().st_size / 1024 / 1024:.1f} MB")

    # Warm up models
    print("Warming up embedding model...")
    from infra._lazy_imports import get_embedding_search
    es = get_embedding_search()
    for i in range(60):
        if es.model is not None:
            print(f"  Embedding model ready ({i}s)")
            break
        time.sleep(1)

    print("Warming up CE model...")
    from search.rerankers import _get_ce_chunk_model
    ce = _get_ce_chunk_model()
    print(f"  CE model ready")

    # Run search with profiling
    print(f"\n{'='*60}")
    print(f"Running search...")
    _phase_times.clear()

    t_start = time.time()
    result = search_memories(
        db_path,
        query,
        limit=limit,
        category="sessions",
        tenant_id="longmemeval",
        hybrid=False,
        deep_rerank=False,
        rerank=True,
        light=False,
    )
    t_total = (time.time() - t_start) * 1000

    # Print results
    print(f"\nResults: {result.get('count', 0)} items")
    for i, r in enumerate(result.get("results", [])[:5]):
        print(f"  {i+1}. [{r.get('id', '?')}] score={r.get('score', 0):.4f}")
        content = r.get("content", "")[:80]
        print(f"     {content}...")

    # Print phase latencies
    print(f"\n{'='*60}")
    print(f"Phase Latencies (total: {t_total:.1f}ms)")
    print(f"{'='*60}")

    # Aggregate by phase name
    from collections import defaultdict
    phase_agg = defaultdict(lambda: [0.0, 0])
    for name, ms in _phase_times:
        phase_agg[name][0] += ms
        phase_agg[name][1] += 1

    # Sort by total time descending
    sorted_phases = sorted(phase_agg.items(), key=lambda x: -x[1][0])

    print(f"{'Phase':<35} {'Total (ms)':>10} {'Count':>6} {'Avg (ms)':>10}")
    print(f"{'-'*35} {'-'*10} {'-'*6} {'-'*10}")
    for name, (total, count) in sorted_phases:
        avg = total / count if count else 0
        print(f"{name:<35} {total:>10.1f} {count:>6} {avg:>10.1f}")

    # Check orchestrator's own phase_latencies
    print(f"\n{'='*60}")
    print(f"Orchestrator Phase Latencies:")
    print(f"{'='*60}")
    with orch._phase_latencies_lock:
        for name, ms in sorted(orch._phase_latencies.items(), key=lambda x: -x[1]):
            print(f"  {name:<35} {ms:>10.1f}ms")

    # Cleanup
    try:
        shutil.rmtree(db_path.parent, ignore_errors=True)
    except Exception:
        pass

if __name__ == "__main__":
    main()
