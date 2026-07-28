import sqlite3
import time
import math
from cron.cron_recompute_temporal_priors import fit_half_life
from search.enrichment import _apply_post_rank_metadata

def test_fit_half_life_insufficient_data():
    # Less than 10 samples -> returns default
    assert fit_half_life("lessons", []) == 180.0
    assert fit_half_life("concepts", [("2026-01-01", 1)] * 5) == 730.0

def test_fit_half_life_regression():
    # Mock a set of note access counts.
    # Older notes have fewer accesses.
    # Let's say hl = 50 days.
    now = time.time()
    rows = []
    from datetime import datetime
    for age in range(1, 21):
        created = datetime.fromtimestamp(now - age * 86400).isoformat()
        access_count = max(1, int(round(10 * math.exp(-0.01386 * age))))
        rows.append((created, access_count))
    
    hl = fit_half_life("sessions", rows)
    # The fitted hl should be close to 50 days.
    assert 10.0 <= hl <= 100.0

def test_fit_half_life_positive_slope():
    # If slope is positive (older notes have MORE accesses), fall back to default
    now = time.time()
    rows = []
    from datetime import datetime
    for age in range(1, 21):
        created = datetime.fromtimestamp(now - age * 86400).isoformat()
        access_count = age * 2
        rows.append((created, access_count))
    
    hl = fit_half_life("sessions", rows)
    assert hl == 14.0 # default for sessions

def test_temporal_factor_per_category_enrichment(tmp_path):
    # Test _apply_post_rank_metadata with per-category decay.
    db_file = tmp_path / "test_priors.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_temporal_priors (
            category       TEXT PRIMARY KEY,
            half_life_days REAL NOT NULL,
            updated_at     REAL NOT NULL
        )
        """
    )
    # Insert custom half-lives
    conn.execute(
        "INSERT INTO memory_temporal_priors (category, half_life_days, updated_at) VALUES (?, ?, ?)",
        ("sessions", 5.0, time.time())
    )
    conn.execute(
        "INSERT INTO memory_temporal_priors (category, half_life_days, updated_at) VALUES (?, ?, ?)",
        ("concepts", 1000.0, time.time())
    )
    conn.commit()
    conn.close()

    # Enrich two items with the same age (10 days old).
    # Sessions has half-life = 5 -> decays much faster (smaller temporal_decay factor)
    # than concepts (half-life = 1000).
    now = time.time()
    from datetime import datetime
    created_str = datetime.fromtimestamp(now - 10 * 86400).isoformat()

    items = [
        {
            "id": "note1",
            "category": "sessions",
            "created": created_str,
            "last_accessed": None,
        },
        {
            "id": "note2",
            "category": "concepts",
            "created": created_str,
            "last_accessed": None,
        }
    ]

    enriched = _apply_post_rank_metadata(items, "test", db_path=db_file, as_of=now)
    decay_session = enriched[0]["temporal_decay"]
    decay_concept = enriched[1]["temporal_decay"]

    # Sessions (hl=5) decayed more than concepts (hl=1000)
    assert decay_concept > decay_session
