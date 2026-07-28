"""Tests for the temporal_ssm training pipeline + gated reranker wiring.

Run:  venv/bin/python -m pytest eval/test_temporal_ssm_training.py -q
(Repo uses a subprocess-per-file runner; this file is self-contained.)
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import config as config_module
from search.scoring import TemporalAttentionModel, reset_ssm_model, _apply_temporal_ssm_rerank
import cron.cron_train_temporal_ssm as ssm_cron


class _FakeConfig:
    temporal_ssm_enabled = False
    temporal_ssm_weights = ""
    db_path = ""
    _config_path = ""


def _make_fake_config(enabled=False, weights="", db_path="", config_path=""):
    fake = _FakeConfig()
    fake.temporal_ssm_enabled = enabled
    fake.temporal_ssm_weights = weights
    fake.db_path = db_path
    fake._config_path = config_path
    return fake


def _scratch_db(path: Path):
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.execute("CREATE TABLE memories (id TEXT, content TEXT, fitness_score REAL, "
                 "importance INTEGER, access_count INTEGER, last_accessed TEXT)")
    conn.execute("CREATE TABLE memory_ctr_feedback (note_id TEXT, clicked_at TEXT, "
                 "dismissed_at TEXT, returned_at TEXT)")
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, tool_name TEXT, params TEXT)")
    now = __import__("time").time()
    iso = __import__("datetime").datetime.fromtimestamp(now).isoformat()
    for i in range(12):
        clicked = (i % 2 == 0)
        conn.execute("INSERT INTO memories VALUES (?,?,?,?,?,?)",
                     (f"n{i}", f"alpha beta note {i}", 0.7, 4, 5, iso))
        conn.execute("INSERT INTO memory_ctr_feedback VALUES (?,?,?,?)",
                     (f"n{i}", iso if clicked else None, None if clicked else iso, iso))
    conn.execute("INSERT INTO audit_log VALUES (1,'memory_search',?)",
                 ('{"query":"alpha beta"}',))
    conn.commit()
    return conn


class TestSSMFromConfig(unittest.TestCase):
    def tearDown(self):
        reset_ssm_model()

    def test_raises_when_flag_off(self):
        with mock.patch.object(config_module, "get_config", lambda: _make_fake_config(enabled=False)):
            with self.assertRaises(RuntimeError):
                TemporalAttentionModel.from_config()

    def test_loads_weights_when_flag_on(self):
        w = np.random.uniform(-1, 1, 58)
        s = ",".join(f"{v:.6f}" for v in w)
        with mock.patch.object(config_module, "get_config", lambda: _make_fake_config(enabled=True, weights=s)):
            model = TemporalAttentionModel.from_config()
        self.assertTrue(model.has_learned_weights)
        self.assertEqual(model.W_readout.shape, (8,))
        self.assertEqual(model.W_input.shape, (8, 6))

    def test_neutral_without_weights(self):
        # Flag on but no weights string -> zero-weight model, score() is 0.5.
        with mock.patch.object(config_module, "get_config", lambda: _make_fake_config(enabled=True, weights="")):
            model = TemporalAttentionModel.from_config()
        self.assertFalse(model.has_learned_weights)
        self.assertEqual(model.score("x"), 0.5)


class TestSSMRerankGated(unittest.TestCase):
    def tearDown(self):
        reset_ssm_model()

    def test_noop_when_flag_off(self):
        # Force the flag off deterministically (don't touch global os.environ,
        # which would clobber sibling test files that enable it).
        with mock.patch.object(config_module, "get_config", lambda: _make_fake_config(enabled=False)):
            results = [("n1", "content one", 0.0, 0.0, 0.0, 0.0, 0.9, 0.7, 3, None, None, None, 5)]
            out = _apply_temporal_ssm_rerank("query", results)
        # Flag off -> model is None -> results returned unchanged.
        self.assertEqual(out, results)
        self.assertEqual(out[0][6], 0.9)


class TestSSMTrainingCron(unittest.TestCase):
    def test_sgd_train_produces_58_weights_and_learns(self):
        rng = np.random.default_rng(0)
        X = rng.random((200, 6))
        # Label = 1 when first feature high, else 0.
        y = (X[:, 0] > 0.5).astype(float)
        weights = ssm_cron._sgd_train(X, y, epochs=120, lr=0.1)
        self.assertEqual(weights.size, 58)

        # Verify the trained forward pass separates the classes.
        W_readout, b_readout, W_input, b_input = ssm_cron._unpack(weights)
        inner = X @ W_input.T + b_input
        h = np.tanh(inner)
        raw = h @ W_readout + b_readout
        pred = np.tanh(raw) * 0.5 + 0.5
        # Accuracy should beat random.
        acc = float(np.mean((pred > 0.5).astype(float) == y))
        self.assertGreater(acc, 0.8)

    def test_main_writes_weights_to_toml(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "mem.db"
            conn = _scratch_db(db_path)
            conn.close()
            toml_path = Path(td) / "memory.toml"
            toml_path.write_text("[features]\ntemporal_ssm_enabled = false\n", encoding="utf-8")

            fake_cfg = _make_fake_config(enabled=False)
            fake_cfg.db_path = str(db_path)
            fake_cfg._config_path = str(toml_path)

            with mock.patch.object(ssm_cron, "_get_config", lambda: fake_cfg):
                rc = ssm_cron.main()
            self.assertEqual(rc, 0)
            text = toml_path.read_text(encoding="utf-8")
            self.assertIn("temporal_ssm_weights", text)
            # Extract weights and confirm 58 floats.
            line = [l for l in text.splitlines() if l.strip().startswith("temporal_ssm_weights")][0]
            val = line.split('"')[1]
            self.assertEqual(len(val.split(",")), 58)

    def test_main_skips_on_cold_start(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "mem.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE memories (id TEXT, content TEXT, fitness_score REAL, "
                         "importance INTEGER, access_count INTEGER, last_accessed TEXT)")
            conn.execute("CREATE TABLE memory_ctr_feedback (note_id TEXT, clicked_at TEXT, "
                         "dismissed_at TEXT, returned_at TEXT)")
            conn.commit()
            conn.close()
            toml_path = Path(td) / "memory.toml"
            toml_path.write_text("[features]\ntemporal_ssm_enabled = false\n", encoding="utf-8")

            fake_cfg = _make_fake_config(enabled=False)
            fake_cfg.db_path = str(db_path)
            fake_cfg._config_path = str(toml_path)

            with mock.patch.object(ssm_cron, "_get_config", lambda: fake_cfg):
                rc = ssm_cron.main()
            self.assertEqual(rc, 0)
            self.assertNotIn("temporal_ssm_weights", toml_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
