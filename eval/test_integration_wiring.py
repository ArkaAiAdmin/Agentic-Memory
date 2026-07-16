"""Subprocess-isolated wiring / integration tests for Q2 + Q4 features.

Per Hard Rule 20 each test runs in its own Python process so module
singletons (``_FLAG_TIERS``, the crontab parse, the fleet-status JSON, …)
start from a clean state.  The tests PROVE the features are actually wired
into the running system — not merely importable.

Covered:
  1. Crontab wires tier-patching (``--apply-tier-patches`` + ``--reload-policy``).
  2. Crontab wires the hourly fleet ``policy_hash_status`` job.
  3. ``--apply-tier-patches`` changes tiers end-to-end (cross-process proof
     via the ``tier_patch_applied`` audit event written by the real cron).
  4. ``cron_policy_hash_status.py`` runs and reports with zero peers.
  5. ``MEMORY_TOML_HOT_RELOAD`` is documented as opt-in / OFF by default.
"""
import os
import subprocess
import sys
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_subprocess(code: str, env: dict) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=90,
        cwd=REPO_ROOT,
    )


# ---------------------------------------------------------------------------
# 1. Crontab wires tier-patching
# ---------------------------------------------------------------------------

class TestCrontabWiresTierPatching(unittest.TestCase):
    def test_crontab_contains_tier_patch_flags(self):
        code = textwrap.dedent(f"""
            import json
            import sys
            sys.path.insert(0, {repr(os.path.join(REPO_ROOT))})
            from cron.jobs import JOBS
            drift_job = JOBS.get("config_drift", {{}})
            args = drift_job.get("args", [])
            # Flags may be in top-level args or nested in --payload JSON
            all_text = " ".join(str(a) for a in args)
            assert "--apply-tier-patches" in all_text, (
                f"config_drift job missing --apply-tier-patches: {{args}}"
            )
            assert "--reload-policy" in all_text, (
                f"config_drift job missing --reload-policy: {{args}}"
            )
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


# ---------------------------------------------------------------------------
# 2. Crontab wires the hourly fleet job
# ---------------------------------------------------------------------------

class TestCrontabWiresFleetJob(unittest.TestCase):
    def test_crontab_invokes_fleet_policy_hash_status(self):
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {repr(os.path.join(REPO_ROOT))})
            from cron.jobs import JOBS
            assert "policy_hash_status" in JOBS, (
                f"policy_hash_status not in JOBS: {{list(JOBS.keys())}}"
            )
            job = JOBS["policy_hash_status"]
            assert job.get("freq") == "1h", (
                f"policy_hash_status should be hourly, got: {{job.get('freq')}}"
            )
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


# ---------------------------------------------------------------------------
# 3. --apply-tier-patches actually changes tiers end-to-end
# ---------------------------------------------------------------------------

class TestApplyTierPatchesEndToEnd(unittest.TestCase):
    def test_cron_applies_tier_patch_cross_process(self):
        code = textwrap.dedent(f"""
            import sys, os, json, tempfile, subprocess
            from pathlib import Path
            sys.path.insert(0, {repr(REPO_ROOT)})

            tmpdir = tempfile.mkdtemp()
            toml = Path(tmpdir) / "memory.toml"
            # Override a built-in flag to a NON-default tier so a real patch
            # (prev != tier) occurs and a tier_patch_applied audit event fires.
            toml.write_text(
                "[drift]\\n"
                'default_mode = "warn"\\n\\n'
                "[drift_tiers]\\n"
                'MEMORY_DB_FLOCK = "integrity"\\n'
            )
            audit = Path(tmpdir) / "audit.jsonl"

            env = dict(os.environ)
            env = {{k: v for k, v in env.items() if not k.startswith("MEMORY_")}}
            env["MEMORY_CONFIG_PATH"] = str(toml)
            env["MEMORY_DRIFT_AUDIT_PATH"] = str(audit)

            r = subprocess.run(
                [sys.executable, "cron/cron_check_config_drift.py",
                 "--severity-floor", "stability",
                 "--reload-policy", "--apply-tier-patches", "--dry-run"],
                capture_output=True, text=True, cwd={repr(REPO_ROOT)},
                env=env, timeout=60,
            )
            assert r.returncode == 0, r.stderr

            assert audit.exists(), f"audit file not written: {{r.stderr}}"
            events_text = audit.read_text().strip()
            assert events_text, f"no audit events written: {{r.stderr}}"
            events = [json.loads(ln) for ln in events_text.splitlines() if ln.strip()]
            applied = [e for e in events if e.get("decision") == "tier_patch_applied"]
            assert applied, f"no tier_patch_applied event: {{events_text}}"
            patched = applied[0]["extra"]["patched"]
            assert ["MEMORY_DB_FLOCK", "integrity"] in patched, (
                f"MEMORY_DB_FLOCK not patched to integrity: {{patched}}"
            )

            # Re-derive in THIS process the exact way the cron does (reset ->
            # apply toml overrides) and assert the override took effect.
            import infra.config as cfg
            cfg._TOML_PATH = toml
            from infra.config_drift import _FLAG_TIERS, _HARDCODE_DEFAULTS
            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml
            from infra.config import _read_toml
            _FLAG_TIERS.clear(); _FLAG_TIERS.update(_HARDCODE_DEFAULTS)
            apply_tier_overrides_from_toml(_read_toml(toml))
            got = _FLAG_TIERS.get("MEMORY_DB_FLOCK")
            assert got is not None and got.value == "integrity", (
                f"MEMORY_DB_FLOCK tier = {{got}}, expected integrity"
            )
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


# ---------------------------------------------------------------------------
# 4. cron_policy_hash_status.py runs and reports (zero peers)
# ---------------------------------------------------------------------------

class TestFleetPolicyHashStatusRuns(unittest.TestCase):
    def test_runs_with_zero_peers(self):
        code = textwrap.dedent(f"""
            import sys, os, subprocess
            sys.path.insert(0, {repr(REPO_ROOT)})

            env = dict(os.environ)
            env = {{k: v for k, v in env.items() if not k.startswith("MEMORY_")}}
            # Ensure no sync peers are configured.
            env.pop("MEMORY_SYNC_PEERS", None)
            env.pop("MEMORY_SYNC_TOKEN", None)

            r = subprocess.run(
                [sys.executable, "cron/cron_policy_hash_status.py", "--alert-stdout"],
                capture_output=True, text=True, cwd={repr(REPO_ROOT)},
                env=env, timeout=60,
            )
            assert r.returncode == 0, r.stderr

            # Must emit the one-line divergence summary.
            out = r.stdout
            assert "FLEET-POLICY-STATUS:" in out, f"no status line: {{out!r}}"
            assert "aligned=" in out and "divergent=" in out, out
            assert "unreachable=" in out and "pending=" in out, out

            # Zero peers => no divergence alert, all counts zero.
            assert "FLEET-DRIFT-ALERT" not in out, f"unexpected alert: {{out!r}}"
            for key in ("aligned=0", "divergent=0", "unreachable=0", "pending=0"):
                assert key in out, f"missing {{key}} in {{out!r}}"
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


# ---------------------------------------------------------------------------
# 5. MEMORY_TOML_HOT_RELOAD documented as opt-in / OFF by default
# ---------------------------------------------------------------------------

class TestHotReloadDefaultDocumented(unittest.TestCase):
    def test_hot_reload_documented_off_by_default(self):
        code = textwrap.dedent(f"""
            from pathlib import Path
            doc = Path({repr(os.path.join(REPO_ROOT, "docs", "reference", "configuration.md"))})
            text = doc.read_text()
            idx = text.find("MEMORY_TOML_HOT_RELOAD")
            assert idx != -1, "MEMORY_TOML_HOT_RELOAD not mentioned in configuration.md"
            # Inspect the surrounding context (same line + a few before/after).
            ctx = text[max(0, idx - 400): idx + 400]
            lowered = ctx.lower()
            assert ("off" in lowered) or ("opt-in" in lowered) or ("default" in lowered), (
                f"MEMORY_TOML_HOT_RELOAD not documented as OFF/opt-in/default:\\n{{ctx}}"
            )
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
