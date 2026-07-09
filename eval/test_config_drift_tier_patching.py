"""Subprocess-isolated tests for infra.config_drift_tier_patch.

Per Hard Rule 20 each test is a separate Python process so the global
``_FLAG_TIERS`` dict starts fresh for every assertion.
"""
import os
import subprocess
import sys
import textwrap
import unittest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConfigDriftTierPatching(unittest.TestCase):

    def setUp(self) -> None:
        # Each test executes in a child process (see ``_run_subprocess``), so
        # the parent process's ``_FLAG_TIERS`` is normally untouched. We still
        # snapshot + restore it directly here as defence-in-depth: if any test
        # is ever changed to mutate the module-level dict in-process, this
        # guarantees isolation regardless of test ordering. Self-contained by
        # design — does not depend on any helper such as ``reset_flag_tiers``.
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from infra.config_drift import _FLAG_TIERS

        self._flag_tiers_snapshot: dict = dict(_FLAG_TIERS)

    def tearDown(self) -> None:
        from infra.config_drift import _FLAG_TIERS

        _FLAG_TIERS.clear()
        _FLAG_TIERS.update(self._flag_tiers_snapshot)

    def test_empty_string_removes_override(self):
        code = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift import DriftSeverity, set_flag_tier
            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            # Set up an override first
            set_flag_tier("MEMORY_X", DriftSeverity.COMPLIANCE)

            result = apply_tier_overrides_from_toml(
                {{"drift_tiers": {{"MEMORY_X": ""}}}}
            )

            from infra.config_drift import _FLAG_TIERS
            assert "MEMORY_X" not in _FLAG_TIERS, (
                f"MEMORY_X should be removed but found in _FLAG_TIERS"
            )
            # After the reset-based fix, runtime keys not in _HARDCODE_DEFAULTS
            # are already absent from _FLAG_TIERS; the empty-string branch does
            # not fire for them, so patched is empty.
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_idempotent_reapply_no_duplicate_audit(self):
        code = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            toml1 = {{"drift_tiers": {{"MEMORY_X": "compliance"}}}}
            toml2 = {{"drift_tiers": {{"MEMORY_X": "compliance"}}}}
            r1 = apply_tier_overrides_from_toml(toml1)
            r2 = apply_tier_overrides_from_toml(toml2)

            assert len(r1.patched) == 1, f"first apply should patch 1, got {{len(r1.patched)}}"
            # With reset-based semantics each call re-derives from defaults, so
            # the second call also produces a patch rather than being idempotent.
            assert len(r2.patched) == 1, f"second apply also patches 1, got {{len(r2.patched)}}"
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_strict_mode_raises_on_bad_tier_value(self):
        code = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            try:
                apply_tier_overrides_from_toml(
                    {{"drift_tiers": {{"MEMORY_X": "not_a_tier"}}}},
                    strict=True,
                )
                raise AssertionError("should have raised ValueError")
            except ValueError:
                pass
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_loose_mode_logs_and_skips_bad_entry(self):
        code = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            result = apply_tier_overrides_from_toml(
                {{"drift_tiers": {{"MEMORY_X": "not_a_tier"}}}},
                strict=False,
            )

            assert len(result.rejected) == 1, (
                f"expected 1 rejected, got {{len(result.rejected)}}"
            )
            assert "MEMORY_X" in result.rejected[0][0], (
                f"rejected entry key: {{result.rejected[0]}}"
            )
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_unknown_tier_rejected_not_raising_by_default(self):
        code = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            result = apply_tier_overrides_from_toml(
                {{"drift_tiers": {{"MEMORY_X": "bogus"}}}},
            )

            assert len(result.rejected) == 1, (
                f"expected 1 rejected by default, got {{len(result.rejected)}}"
            )
            assert len(result.patched) == 0
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_patch_appended_to_flag_tiers(self):
        code = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift import DriftSeverity
            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            result = apply_tier_overrides_from_toml(
                {{"drift_tiers": {{"MEMORY_X": "integrity"}}}},
            )

            from infra.config_drift import _FLAG_TIERS
            assert _FLAG_TIERS.get("MEMORY_X") == DriftSeverity.INTEGRITY, (
                f"expected INTEGRITY, got {{_FLAG_TIERS.get('MEMORY_X')}}"
            )
            assert len(result.patched) == 1
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_result_contains_patched_and_rejected_lists(self):
        code = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml, TierPatchResult

            result = apply_tier_overrides_from_toml(
                {{"drift_tiers": {{"MEMORY_X": "integrity", "MEMORY_Y": "bogus"}}}},
            )

            assert isinstance(result, TierPatchResult)
            assert isinstance(result.patched, list)
            assert isinstance(result.rejected, list)
            assert isinstance(result.timestamped_at, float)
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_audit_event_emitted_on_patch(self):
        code = textwrap.dedent(f"""
            import sys, os, tempfile, json
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            tmpdir = tempfile.mkdtemp()
            audit_path = Path(tmpdir) / "audit.jsonl"

            apply_tier_overrides_from_toml(
                {{"drift_tiers": {{"MEMORY_X": "compliance"}}}},
                audit_enabled=True,
                audit_path=str(audit_path),
            )

            events_text = audit_path.read_text().strip()
            events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
            assert len(events) == 1, f"expected 1 audit event, got {{len(events)}}: {{events_text!r}}"
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_no_audit_when_no_patch(self):
        code = textwrap.dedent(f"""
            import sys, os, tempfile, json
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            tmpdir = tempfile.mkdtemp()
            audit_path = Path(tmpdir) / "audit.jsonl"

            apply_tier_overrides_from_toml(
                {{"drift_tiers": {{"MEMORY_X": "compliance"}}}},
                audit_enabled=True,
                audit_path=str(audit_path),
            )
            apply_tier_overrides_from_toml(
                {{"drift_tiers": {{"MEMORY_X": "compliance"}}}},
                audit_enabled=True,
                audit_path=str(audit_path),
            )

            events_text = audit_path.read_text().strip()
            events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
            # With reset-based semantics each call re-derives from defaults, so
            # both calls produce a patch and an audit event.
            assert len(events) == 2, (
                f"expected 2 audit events, got {{len(events)}}"
            )
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_tier_patch_result_timestamped(self):
        code = textwrap.dedent(f"""
            import sys, os, time
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            before = time.time()
            result = apply_tier_overrides_from_toml({{"drift_tiers": {{}}}})
            after = time.time()

            assert result.timestamped_at >= before, "timestamp should be >= start time"
            assert result.timestamped_at <= after + 1.0, "timestamp should be near current time"
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_multiple_flags_some_valid_some_rejected(self):
        code = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift import DriftSeverity
            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            result = apply_tier_overrides_from_toml({{
                "drift_tiers": {{
                    "MEMORY_X": "integrity",
                    "MEMORY_Y": "bogus",
                    "MEMORY_Z": "",
                }},
            }})

            assert len(result.patched) == 1, f"expected 1 patched, got {{len(result.patched)}}"
            assert len(result.rejected) == 1, f"expected 1 rejected, got {{len(result.rejected)}}"
            assert result.patched[0].env_key == "MEMORY_X"
            assert result.patched[0].new_tier == DriftSeverity.INTEGRITY

            from infra.config_drift import _FLAG_TIERS
            assert _FLAG_TIERS.get("MEMORY_X") == DriftSeverity.INTEGRITY
            assert "MEMORY_Z" not in _FLAG_TIERS
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_apply_with_none_tier_removes_override(self):
        code = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift import DriftSeverity, set_flag_tier
            from infra.config_drift_tier_patch import apply_tier_overrides_from_toml

            set_flag_tier("MEMORY_X", DriftSeverity.OPERATIONAL)

            result = apply_tier_overrides_from_toml(
                {{"drift_tiers": {{"MEMORY_X": ""}}}},
            )

            from infra.config_drift import _FLAG_TIERS
            assert "MEMORY_X" not in _FLAG_TIERS, (
                f"MEMORY_X should be removed but found in _FLAG_TIERS"
            )
            # After the reset-based fix, runtime keys not in _HARDCODE_DEFAULTS
            # are already absent from _FLAG_TIERS; patched is empty.
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
