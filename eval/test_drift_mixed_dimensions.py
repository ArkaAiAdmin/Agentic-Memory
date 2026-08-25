#!/usr/bin/env python3
"""Pin the mixed-dimension guard in the concept-drift alarm scorer.

2026-08-25 incident: the corpus gained embeddings of a second dimensionality
(model migration + journal replay backfill). ``_record_drift_event`` scored
every raw ``memory_embeddings`` vector against the centroid's dimension
indices and raised ``IndexError: index N out of bounds for axis 0 with size
128`` — taking down ``memory_check_concept_drift`` entirely. Vectors whose
dim differs from the centroid space are now skipped.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from search.drift import check_concept_drift_db  # noqa: E402

DIM = 128


def _make_db(path: Path, mixed_dims: bool) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE concept_drift (
            id TEXT PRIMARY KEY,
            drift_metric REAL,
            drifted_dimensions TEXT,
            triggered_at REAL
        );
        CREATE TABLE memory_embeddings (
            memory_id TEXT PRIMARY KEY,
            embedding BLOB
        );
        CREATE TABLE drift_alarms (
            memory_id TEXT,
            concept TEXT,
            drift_score REAL,
            threshold REAL,
            alarm_level TEXT,
            detected_at TEXT
        );
        """
    )
    rng = np.random.default_rng(42)
    # Majority-dim vectors (these define the centroid space).
    for i in range(4):
        vec = rng.standard_normal(DIM).astype(np.float32)
        conn.execute(
            "INSERT INTO memory_embeddings VALUES (?, ?)",
            (f"mem-full-{i}", vec.tobytes()),
        )
    if mixed_dims:
        # A legacy short vector whose dims are all below top_idxs range —
        # indexing it with centroid indices used to raise IndexError.
        short = rng.standard_normal(DIM // 2).astype(np.float32)
        conn.execute(
            "INSERT INTO memory_embeddings VALUES (?, ?)",
            ("mem-short", short.tobytes()),
        )
    conn.commit()
    conn.close()


class TestDriftMixedDimensions(unittest.TestCase):
    def _run(self, mixed_dims: bool) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "memory.db"
        _make_db(db, mixed_dims)
        result = check_concept_drift_db(str(db), threshold=0.0)
        return json.loads(result) if isinstance(result, str) else result

    def test_uniform_dims_baseline_succeeds(self):
        data = self._run(mixed_dims=False)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["drift_metric"], 0.0)
        self.assertEqual(data["n_embedded"], 4)

    def test_mixed_dims_do_not_crash_and_skip_short_vectors(self):
        # Pre-fix this raised IndexError; the guard must skip `mem-short`
        # and still write alarms for full-dim memories only.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "memory.db"
        _make_db(db, mixed_dims=True)
        result = check_concept_drift_db(str(db), threshold=0.0)
        data = json.loads(result) if isinstance(result, str) else result
        self.assertEqual(data["n_embedded"], 4)  # majority-dim filter applied
        # Prove the alarm scorer actually saw out-of-short-vector indices:
        # at least one top-drifted dimension must exceed the short vec range,
        # otherwise this test wouldn't have crashed pre-fix.
        top_indices = [d["index"] for d in data["drifted_dimensions"]]
        self.assertTrue(max(top_indices) >= DIM // 2, top_indices)
        conn = sqlite3.connect(str(db))
        try:
            alarmed = [
                r[0]
                for r in conn.execute("SELECT memory_id FROM drift_alarms").fetchall()
            ]
        finally:
            conn.close()
        self.assertTrue(alarmed)
        self.assertNotIn("mem-short", alarmed)


if __name__ == "__main__":
    unittest.main()
