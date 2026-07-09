"""Tests for infra.policy_hash_cache (fleet peer-policy cache).

Subprocess-isolated per Hard Rule 20 so each test starts from a clean
module + a fresh on-disk cache location (MEMORY_DB_PATH points the cache
file into a temp dir via resolve_active_memory_dir()).
"""
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_subprocess(code: str, env: dict) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=30,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )


class TestPolicyHashCache(unittest.TestCase):

    def _env(self, tmpdir: str) -> dict:
        # Point the active memory dir (and thus the cache file) at tmpdir.
        return {"MEMORY_DB_PATH": str(Path(tmpdir) / "memory.db")}

    def test_load_peer_cache_empty_when_no_file(self):
        code = textwrap.dedent(f"""
            import os, sys, tempfile
            sys.path.insert(0, {repr(REPO_ROOT)})
            import infra.policy_hash_cache as pc
            assert pc.load_peer_cache() == {{}}, "expected empty cache"
            print("PASS")
        """)
        import tempfile
        tmp = tempfile.mkdtemp()
        result = _run_subprocess(code, self._env(tmp))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_is_cache_fresh_true_for_recent_entry(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(REPO_ROOT)})
            import infra.policy_hash_cache as pc
            entry = {{"policy_hash": "abc", "fetched_at": time.time()}}
            assert pc.is_cache_fresh(entry, 60.0) is True
            print("PASS")
        """)
        import tempfile
        tmp = tempfile.mkdtemp()
        result = _run_subprocess(code, self._env(tmp))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_is_cache_fresh_false_after_ttl_expiry(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(REPO_ROOT)})
            import infra.policy_hash_cache as pc
            # fetched 100s ago, ttl 60s -> expired
            entry = {{"policy_hash": "abc", "fetched_at": time.time() - 100.0}}
            assert pc.is_cache_fresh(entry, 60.0) is False
            # fetched_at <= 0 is never fresh
            assert pc.is_cache_fresh({{}}, 60.0) is False
            print("PASS")
        """)
        import tempfile
        tmp = tempfile.mkdtemp()
        result = _run_subprocess(code, self._env(tmp))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_filter_stale_entries_splits_fresh_and_stale(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(REPO_ROOT)})
            import infra.policy_hash_cache as pc
            now = time.time()
            cache = {{
                "peer_a": {{"policy_hash": "a", "fetched_at": now}},            # fresh
                "peer_b": {{"policy_hash": "b", "fetched_at": now - 1000.0}},   # stale
            }}
            fresh, stale = pc.filter_stale_entries(cache, 60.0)
            assert set(fresh.keys()) == {{"peer_a"}}, fresh.keys()
            assert set(stale.keys()) == {{"peer_b"}}, stale.keys()
            print("PASS")
        """)
        import tempfile
        tmp = tempfile.mkdtemp()
        result = _run_subprocess(code, self._env(tmp))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_persist_then_load_roundtrip(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(REPO_ROOT)})
            import infra.policy_hash_cache as pc
            cache = {{"peer_a": {{"policy_hash": "a", "fetched_at": time.time()}}}}
            pc.persist_peer_cache(cache)
            loaded = pc.load_peer_cache()
            assert loaded == cache, (loaded, cache)
            print("PASS")
        """)
        import tempfile
        tmp = tempfile.mkdtemp()
        result = _run_subprocess(code, self._env(tmp))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_atomic_write_produces_valid_json(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile, json
            sys.path.insert(0, {repr(REPO_ROOT)})
            import infra.policy_hash_cache as pc
            cache = {{"peer_a": {{"policy_hash": "a", "fetched_at": time.time()}}}}
            pc.persist_peer_cache(cache)
            # The on-disk file must be valid JSON (atomic_write never leaves
            # a half-written file behind).
            raw = pc._cache_path().read_text()
            parsed = json.loads(raw)  # raises if corrupt
            assert parsed == cache, (parsed, cache)
            print("PASS")
        """)
        import tempfile
        tmp = tempfile.mkdtemp()
        result = _run_subprocess(code, self._env(tmp))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_isolation_between_two_peers(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(REPO_ROOT)})
            import infra.policy_hash_cache as pc
            # Seed only peer_a
            pc.persist_peer_cache({{"peer_a": {{"policy_hash": "a", "fetched_at": time.time()}}}})
            loaded = pc.load_peer_cache()
            assert set(loaded.keys()) == {{"peer_a"}}, loaded.keys()
            assert "peer_b" not in loaded
            # Add peer_b without touching peer_a
            merged = dict(loaded)
            merged["peer_b"] = {{"policy_hash": "b", "fetched_at": time.time()}}
            pc.persist_peer_cache(merged)
            loaded2 = pc.load_peer_cache()
            assert set(loaded2.keys()) == {{"peer_a", "peer_b"}}, loaded2.keys()
            assert loaded2["peer_a"]["policy_hash"] == "a"
            assert loaded2["peer_b"]["policy_hash"] == "b"
            print("PASS")
        """)
        import tempfile
        tmp = tempfile.mkdtemp()
        result = _run_subprocess(code, self._env(tmp))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_cache_path_under_active_memory_dir(self):
        code = textwrap.dedent(f"""
            import os, sys, tempfile
            from pathlib import Path
            sys.path.insert(0, {repr(REPO_ROOT)})
            import infra.policy_hash_cache as pc
            p = pc._cache_path()
            assert p.name == ".peer_policy_cache.json", p.name
            assert str(p.parent) == os.environ.get("MEMORY_DB_PATH")[:-len("memory.db")].rstrip(os.sep), (str(p.parent), os.environ.get("MEMORY_DB_PATH"))
            print("PASS")
        """)
        import tempfile
        tmp = tempfile.mkdtemp()
        result = _run_subprocess(code, self._env(tmp))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
