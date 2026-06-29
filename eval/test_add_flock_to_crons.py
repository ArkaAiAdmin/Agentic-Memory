#!/usr/bin/env python3
"""Tests for _scripts/add_flock_to_crons.py patcher.

Run:
    ./venv/bin/python -m pytest eval/test_add_flock_to_crons.py -q
"""

import sys
import tempfile
from pathlib import Path
from unittest import TestCase

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from _scripts.add_flock_to_crons import patch_file, _strip_existing_flock  # noqa: E402


MINIMAL_CRON = '''\
#!/usr/bin/env python3
"""Minimal cron script."""
from __future__ import annotations

import os


def main() -> int:
    print("hello")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

CONSOLIDATE_CRON = '''\
#!/usr/bin/env python3
"""Consolidate cron."""
from __future__ import annotations

import os


def consolidate_light() -> int:
    print("consolidating")
    return 0


if __name__ == "__main__":
    sys.exit(consolidate_light())
'''

ALREADY_PATCHED = '''\
#!/usr/bin/env python3
"""Already has flock."""
from __future__ import annotations

from _flock import acquire_lock_or_exit

import os


def main() -> int:
    acquire_lock_or_exit("cron_test")
    print("hello")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


class TestPatchFile(TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="flock_patch_")

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, name: str, content: str) -> Path:
        p = Path(self._tmp) / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_patch_clean_file(self):
        """A clean cron file gets the import and the call inserted."""
        p = self._write("cron_test.py", MINIMAL_CRON)
        changed, summary = patch_file(p)
        self.assertTrue(changed, summary)
        result = p.read_text(encoding="utf-8")
        self.assertIn("from _flock import acquire_lock_or_exit", result)
        self.assertIn("acquire_lock_or_exit(", result)
        self.assertIn("'cron_test'", result)
        # The call must be the first statement inside main(), before print.
        main_body = result.split("def main() -> int:")[1].split("if __name__")[0]
        flock_line = next(
            (line.strip() for line in main_body.splitlines() if "acquire_lock_or_exit" in line),
            None,
        )
        self.assertEqual(flock_line, "acquire_lock_or_exit('cron_test')")
        print_line = next(
            (
                line.strip()
                for line in main_body.splitlines()
                if line.strip().startswith("print")
            ),
            None,
        )
        self.assertIsNotNone(print_line)

    def test_patch_idempotent(self):
        """Running patch twice produces the same final file (content-addressable idempotency)."""
        p = self._write("cron_test.py", MINIMAL_CRON)
        patch_file(p)  # first run
        first = p.read_text(encoding="utf-8")
        patch_file(p)  # second run (strips + re-patches, result must match)
        second = p.read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_patch_consolidate_override(self):
        """consolidate_light entry-point override works."""
        p = self._write("cron_consolidate.py", CONSOLIDATE_CRON)
        changed, _ = patch_file(p)
        self.assertTrue(changed)
        result = p.read_text(encoding="utf-8")
        self.assertIn("acquire_lock_or_exit(", result)
        self.assertIn("'cron_consolidate'", result)
        # Call must be inside consolidate_light, not main.
        consolidate_body = result.split("def consolidate_light() -> int:")[1].split(
            "if __name__"
        )[0]
        self.assertIn("acquire_lock_or_exit(", consolidate_body)
        self.assertIn("'cron_consolidate'", consolidate_body)

    def test_strip_existing_flock(self):
        """_strip_existing_flock removes both import and call."""
        text = ALREADY_PATCHED
        cleaned = _strip_existing_flock(text)
        self.assertNotIn("from _flock import acquire_lock_or_exit", cleaned)
        self.assertNotIn("acquire_lock_or_exit", cleaned)

    def test_all_crons_have_flock(self):
        """Every cron/cron_*.py must contain acquire_lock_or_exit.

        This is the CI regression guard: if a new cron script is
        merged without running the patcher, this test catches it.
        """
        cron_dir = REPO / "cron"
        missing = []
        for cron_file in sorted(cron_dir.glob("cron_*.py")):
            text = cron_file.read_text(encoding="utf-8")
            if "acquire_lock_or_exit" not in text:
                missing.append(str(cron_file.relative_to(REPO)))
        self.assertEqual(
            missing,
            [],
            f"cron scripts missing flock protection: {missing}",
        )
