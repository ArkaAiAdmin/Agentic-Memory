"""Subprocess-isolated tests for toml_watch hot-reload behaviour.

Per Hard Rule 20 each test is an independent Python process so module
singletons (``_active_policy``, ``_FLAG_TIERS``, ``_watcher_state``, …)
start from a clean state.
"""
import os
import subprocess
import sys
import textwrap
import unittest


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


MINIMAL_TOML = """
[drift]
default_mode = "warn"

[drift_tiers]
MEMORY_SAGA_ENABLED = "integrity"
"""


class TestTomlHotReload(unittest.TestCase):

    def test_hot_reload_off_no_effect_when_toml_changes(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            import infra.toml_watch as tw
            import infra.config_drift_policy as cdp

            tmpdir = tempfile.mkdtemp()
            toml_path = Path(tmpdir) / "memory.toml"
            toml_path.write_text({repr(MINIMAL_TOML)})

            import infra.config as cfg
            cfg._TOML_PATH = toml_path

            tw._watcher_state.clear()
            tw._last_known_bytes = b""
            tw.refresh_mtime(); tw.refresh_mtime()

            p_before = cdp._active_policy
            cdp.resolve_policy()
            p_before2 = cdp._active_policy

            os.utime(toml_path, None)
            time.sleep(0.2)

            # No hot-reload env set, so apply_hot_reload is a no-op;
            # the policy cache stays wherever it was after resolve.
            p_after = cdp._active_policy
            assert p_before2 is p_after, (
                "policy cache should be unchanged when hot reload is off"
            )
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_hot_reload_on_detects_mtime_advance(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile, json
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            import infra.toml_watch as tw
            import infra.config_drift_policy as cdp
            import infra.config_drift_audit as cda

            tmpdir = tempfile.mkdtemp()
            toml_path = Path(tmpdir) / "memory.toml"
            toml_path.write_text({repr(MINIMAL_TOML)})

            import infra.config as cfg
            cfg._TOML_PATH = toml_path

            tw._watcher_state.clear()
            tw._last_known_bytes = b""
            tw.refresh_mtime(); tw.refresh_mtime()

            # resolve_policy() wires the subscriber AND starts the poller.
            cdp.resolve_policy()
            assert cdp._active_policy is not None, "policy should resolve first"
            scope_before = cdp._active_policy.scope
            hash_before = cdp._active_policy.policy_hash()

            # Advance mtime well past the poller debounce window. The poller
            # is the single trigger for firing subscribers (apply_hot_reload
            # no longer re-fires them), so _on_toml_change rebuilds the
            # policy after the change is detected. We detect the rebuild by
            # the toml_hot_reload audit event (written by the subscriber),
            # then confirm the rebuilt policy keeps the same scope.
            future = time.time() + 10
            os.utime(toml_path, (future, future))

            deadline = time.time() + 6
            seen = False
            while time.time() < deadline:
                p = cdp.resolve_policy()
                _af = cda._resolve_audit_path(
                    getattr(p, "audit_path", "") or "memory/config_drift_audit.jsonl"
                )
                if _af.exists():
                    _txt = _af.read_text().strip()
                    _events = [json.loads(ln) for ln in _txt.splitlines() if ln.strip()]
                    if any(e.get("decision") == "toml_hot_reload" for e in _events):
                        assert p is not None, "policy should be rebuilt, got None"
                        assert p.scope == scope_before, (
                            f"scope should be unchanged: {{p.scope}} vs {{scope_before}}"
                        )
                        seen = True
                        break
                time.sleep(0.2)
            assert seen, "policy was not rebuilt after hot reload (no audit event)"
            print("PASS")
        """)
        result = _run_subprocess(code, {"MEMORY_TOML_HOT_RELOAD": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_hot_reload_audit_event_written(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile, json
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            import infra.toml_watch as tw
            import infra.config_drift_policy as cdp
            import infra.config_drift_audit as cda

            tmpdir = tempfile.mkdtemp()
            toml_path = Path(tmpdir) / "memory.toml"
            toml_path.write_text({repr(MINIMAL_TOML)})

            import infra.config as cfg
            cfg._TOML_PATH = toml_path

            tw._watcher_state.clear()
            tw._last_known_bytes = b""
            tw.refresh_mtime(); tw.refresh_mtime()

            # Resolve policy so subscribers are wired up + poller started.
            cdp.resolve_policy()

            # Advance mtime into the future so the poller detects a real change
            # (the single trigger that fires subscribers -> _on_toml_change ->
            # _record_toml_reload_event, which writes the audit event).
            future = time.time() + 10
            os.utime(toml_path, (future, future))

            deadline = time.time() + 6
            found = False
            while time.time() < deadline:
                p2 = cdp.resolve_policy()
                _af = cda._resolve_audit_path(
                    getattr(p2, "audit_path", "") or "memory/config_drift_audit.jsonl"
                )
                if _af.exists():
                    _txt = _af.read_text().strip()
                    _events = [json.loads(ln) for ln in _txt.splitlines() if ln.strip()]
                    if any(e.get("decision") == "toml_hot_reload" for e in _events):
                        found = True
                        break
                time.sleep(0.2)
            assert found, "no toml_hot_reload event in audit within timeout"
            print("PASS")
        """)
        result = _run_subprocess(code, {"MEMORY_TOML_HOT_RELOAD": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_hot_reload_poller_thread_is_daemon(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            import infra.toml_watch as tw

            import infra.config as cfg
            existing = cfg._TOML_PATH
            tmpdir = tempfile.mkdtemp()
            fake_toml = Path(tmpdir) / "memory.toml"
            fake_toml.write_text("[drift]\\ndefault_mode = 'warn'\\n")
            cfg._TOML_PATH = fake_toml

            tw._subscribers.clear()
            tw.start_watcher()
            t = tw._poller_thread
            assert t is not None, "_poller_thread should not be None after start"
            assert t.name == "toml-watcher", f"expected 'toml-watcher', got {{t.name!r}}"
            assert t.daemon is True, f"expected daemon=True, got {{t.daemon}}"
            tw.stop_watcher()
            cfg._TOML_PATH = existing
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_multiple_subscribers_both_fired(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            import infra.toml_watch as tw
            import infra.config_drift_policy as cdp

            tmpdir = tempfile.mkdtemp()
            toml_path = Path(tmpdir) / "memory.toml"
            toml_path.write_text({repr(MINIMAL_TOML)})

            import infra.config as cfg
            cfg._TOML_PATH = toml_path

            tw._watcher_state.clear()
            tw._subscribers.clear()
            tw._last_known_bytes = b""
            tw.refresh_mtime(); tw.refresh_mtime()

            # Wire up policy subscriber so apply_hot_reload has someone to call
            cdp.resolve_policy()

            tw.start_watcher(poll_s=0.05)

            fires_a = []
            fires_b = []
            def cb_a(mt): fires_a.append(mt)
            def cb_b(mt): fires_b.append(mt)

            tw.subscribe(cb_a)
            tw.subscribe(cb_b)

            # Advance mtime well beyond the poller debounce window so the
            # change is actually detected (os.utime(None) may land within
            # the 0.05s debounce and be suppressed).
            future = time.time() + 10
            os.utime(toml_path, (future, future))
            time.sleep(0.6)
            tw.stop_watcher()

            assert len(fires_a) >= 1, f"cb_a fired {{len(fires_a)}}x, expected >= 1"
            assert len(fires_b) >= 1, f"cb_b fired {{len(fires_b)}}x, expected >= 1"
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_subscribe_idempotent(self):
        code = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            import infra.toml_watch as tw

            tw._subscribers.clear()

            fires = []
            def cb(mt): fires.append(mt)

            tw.subscribe(cb)
            tw.subscribe(cb)   # idempotent — same cb twice
            sub_count = tw._subscribers.count(cb)
            assert sub_count == 1, (
                f"same callback should not be added twice: count={{sub_count}}"
            )

            # Directly fire subscribers (same thing apply_hot_reload does)
            tw._fire_subscribers(1000.0)

            fire_count = len(fires)
            assert fire_count == 1, f"cb fired {{fire_count}}x, expected 1"
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_watcher_mtime_then_policy_stale_then_refresh(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            import infra.toml_watch as tw
            import infra.config_drift_policy as cdp

            tmpdir = tempfile.mkdtemp()
            toml_path = Path(tmpdir) / "memory.toml"
            toml_path.write_text({repr(MINIMAL_TOML)})

            import infra.config as cfg
            cfg._TOML_PATH = toml_path

            tw._watcher_state.clear()
            tw._last_known_bytes = b""
            tw.refresh_mtime()
            mt = tw.refresh_mtime()
            assert mt > 0, f"expected cached mtime > 0, got {{mt}}"

            p1 = cdp.resolve_policy()
            assert p1 is not None

            os.utime(toml_path, None)
            tw.apply_hot_reload()

            p2 = cdp.resolve_policy()
            assert p2 is not None
            print("PASS")
        """)
        result = _run_subprocess(code, {"MEMORY_TOML_HOT_RELOAD": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_hot_reload_does_not_double_enforce(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            import infra.toml_watch as tw
            import infra.config_drift_policy as cdp

            tmpdir = tempfile.mkdtemp()
            toml_path = Path(tmpdir) / "memory.toml"
            toml_path.write_text({repr(MINIMAL_TOML)})

            import infra.config as cfg
            cfg._TOML_PATH = toml_path

            tw._watcher_state.clear()
            tw._last_known_bytes = b""
            tw.refresh_mtime(); tw.refresh_mtime()

            # Simulate startup enforcement already ran
            cdp.resolve_policy()
            cdp._active_has_inited = True

            # Call the watcher subscriber directly (this is what the poller
            # calls when mtime changes). It resets _active_has_inited so the
            # next get_config()->run_startup_enforcement() can re-run.
            cdp._on_toml_change(tw.current_mtime())
            first = cdp._active_has_inited
            assert first is False, f"expected False (reset) after first cycle, got {{first}}"

            cdp._on_toml_change(tw.current_mtime())
            second = cdp._active_has_inited
            assert second is False, f"expected False (reset) after second cycle, got {{second}}"
            print("PASS")
        """)
        result = _run_subprocess(code, {"MEMORY_TOML_HOT_RELOAD": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_policy_hash_changes_after_reload(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            import infra.toml_watch as tw
            import infra.config_drift_policy as cdp

            tmpdir = tempfile.mkdtemp()
            toml_path = Path(tmpdir) / "memory.toml"
            toml_v1 = "[drift]\\ndefault_mode = \\"warn\\"\\n"
            toml_v2 = "[drift]\\ndefault_mode = \\"soft_block\\"\\n"
            toml_path.write_text(toml_v1)

            import infra.config as cfg
            cfg._TOML_PATH = toml_path

            tw._watcher_state.clear()
            tw._last_known_bytes = b""
            tw.refresh_mtime(); tw.refresh_mtime()

            p1 = cdp.resolve_policy()
            h1 = p1.policy_hash()

            toml_path.write_text(toml_v2)
            os.utime(toml_path, None)
            tw.apply_hot_reload()

            p2 = cdp.resolve_policy()
            h2 = p2.policy_hash()

            assert h1 != h2, f"hash should change: {{h1}} vs {{h2}}"
            print("PASS")
        """)
        result = _run_subprocess(code, {"MEMORY_TOML_HOT_RELOAD": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_no_policy_change_skips_rebuild(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            import infra.toml_watch as tw
            import infra.config_drift_policy as cdp

            tmpdir = tempfile.mkdtemp()
            toml_path = Path(tmpdir) / "memory.toml"
            content = "[drift]\\ndefault_mode = \\"warn\\"\\n"
            toml_path.write_text(content)

            import infra.config as cfg
            cfg._TOML_PATH = toml_path

            tw._watcher_state.clear()
            tw._last_known_bytes = content.encode()
            tw.refresh_mtime(); tw.refresh_mtime()

            p1 = cdp.resolve_policy()
            h1 = p1.policy_hash()

            os.utime(toml_path, None)
            tw.apply_hot_reload()

            p2 = cdp.resolve_policy()
            h2 = p2.policy_hash()

            assert h1 == h2, f"hash should not change: {{h1}} vs {{h2}}"
            print("PASS")
        """)
        result = _run_subprocess(code, {"MEMORY_TOML_HOT_RELOAD": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_stop_watcher_no_future_events(self):
        code = textwrap.dedent(f"""
            import os, sys, time, tempfile
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from pathlib import Path
            import infra.toml_watch as tw

            tmpdir = tempfile.mkdtemp()
            toml_path = Path(tmpdir) / "memory.toml"
            toml_path.write_text({repr(MINIMAL_TOML)})

            import infra.config as cfg
            cfg._TOML_PATH = toml_path

            tw._watcher_state.clear()
            tw._subscribers.clear()
            tw._last_known_bytes = b""
            tw.refresh_mtime(); tw.refresh_mtime()

            tw.start_watcher(poll_s=0.05)

            fires = []
            tw.subscribe(lambda mt: fires.append(mt))

            tw.stop_watcher()

            os.utime(toml_path, None)
            time.sleep(0.5)

            fire_count = len(fires)
            assert fire_count == 0, (
                f"expected 0 fires after stop, got {{fire_count}}"
            )
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_clean_env_no_legacy_policy_pollution(self):
        code = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})

            from infra.config_drift import _FLAG_TIERS
            from infra.config_drift_tier_patch import _HARDCODE_DEFAULTS

            expected_keys = set(_HARDCODE_DEFAULTS.keys())
            actual_keys = set(_FLAG_TIERS.keys())

            missing = expected_keys - actual_keys
            assert not missing, f"missing keys in _FLAG_TIERS: {{missing}}"
            print("PASS")
        """)
        result = _run_subprocess(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
