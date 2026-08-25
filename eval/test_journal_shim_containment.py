"""Regression tests for the 2026-08-22 storage-shim traversal bug.

The App Support migration left category dirs (auto_save, skill, ...) as
symlinks from the repo memory/ dir into a paired data root while journal.db
stayed shared through a symlink too. The journal drainer resolves its
markdown targets through those symlinks; the containment guard treated the
resolution as an escape and dead-lettered 1,318 real writes.

 Pins:
 - plain single-segment categories may resolve into the paired root
   discovered via the journal.db symlink witness
 - adversarial categories (traversal, absolute, nested) stay blocked
 - identity/empty categories and title-slug escapes stay blocked
 - without the shim witness, an externally-symlinked category dir stays
   rejected (no blanket symlink amnesty)
"""

import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from save_pipeline import (  # noqa: E402
    ErrorCode,
    SaveValidationError,
    _category_allowed_bases,
    _resolve_save_paths,
)

GLOBAL_MEM_SENTINEL = INSTALL_DIR / "memory"  # any stable existing dir


def _make_shim_layout(root: Path) -> tuple[Path, Path]:
    """base_a = repo-style memory dir; base_b = paired data root."""
    base_a = root / "repo-memory"
    base_b = root / "paired-data"
    base_a.mkdir()
    base_b.mkdir()
    (base_a / "memory.db").write_bytes(b"")
    (base_b / "journal.db").write_bytes(b"")
    (base_b / "auto_save").mkdir()
    (base_a / "journal.db").symlink_to(base_b / "journal.db")
    (base_a / "auto_save").symlink_to(base_b / "auto_save")
    return base_a, base_b


class TestCategoryAllowedBases(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base_a, self.base_b = _make_shim_layout(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_plain_category_discovers_paired_root_via_journal_symlink(self):
        bases = _category_allowed_bases(self.base_a, "auto_save", GLOBAL_MEM_SENTINEL)
        self.assertIn(self.base_a.resolve(), bases)
        self.assertIn(self.base_b.resolve(), bases)

    def test_traversal_category_does_not_get_paired_root(self):
        for nasty in ("../evil", "/abs/evil", "sub/dir", "..", ".", ""):
            bases = _category_allowed_bases(self.base_a, nasty, GLOBAL_MEM_SENTINEL)
            self.assertNotIn(
                self.base_b.resolve(),
                bases,
                msg=f"paired root leaked for category {nasty!r}",
            )

    def test_no_paired_root_without_journal_symlink_witness(self):
        plain_base = self.root / "plain-base"
        plain_base.mkdir()
        (plain_base / "journal.db").write_bytes(b"")
        bases = _category_allowed_bases(plain_base, "auto_save", GLOBAL_MEM_SENTINEL)
        self.assertEqual(bases, {plain_base.resolve(), GLOBAL_MEM_SENTINEL.resolve()})


class TestResolveSavePathsShim(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base_a, self.base_b = _make_shim_layout(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_shimmed_category_materializes_into_paired_root(self):
        target_base, file_path, _cat_dir, _proj, effective = _resolve_save_paths(
            "auto_save", "shim-test-slug", False, str(self.base_a / "memory.db")
        )
        self.assertEqual(effective, "auto_save")
        resolved_target = Path(str(file_path)).resolve()
        expected = (self.base_b / "auto_save" / "shim-test-slug.md").resolve()
        self.assertEqual(resolved_target, expected)
        self.assertTrue(resolved_target.is_relative_to(self.base_b.resolve()))

    def test_traversal_categories_still_rejected_in_shim_layout(self):
        for nasty in ("../../evil_escape", "/tmp/evil_abs", "auto_save/../../evil"):
            with self.assertRaises(SaveValidationError) as ctx:
                _resolve_save_paths(nasty, "slug", False, str(self.base_a / "memory.db"))
            self.assertEqual(ctx.exception.code, ErrorCode.TRAVERSAL, msg=nasty)

    def test_empty_category_identity_still_rejected(self):
        with self.assertRaises(SaveValidationError) as ctx:
            _resolve_save_paths("", "slug", False, str(self.base_a / "memory.db"))
        self.assertEqual(ctx.exception.code, ErrorCode.TRAVERSAL)
        self.assertIn("identity", str(ctx.exception))

    def test_title_slug_escape_still_rejected(self):
        with self.assertRaises(SaveValidationError) as ctx:
            _resolve_save_paths("auto_save", "../evil_slug", False, str(self.base_a / "memory.db"))
        self.assertEqual(ctx.exception.code, ErrorCode.TRAVERSAL)
        self.assertIn("Title slug", str(ctx.exception))

    def test_external_symlink_without_witness_still_rejected(self):
        elsewhere = self.root / "elsewhere"
        (elsewhere / "auto_save").mkdir(parents=True)
        base_c = self.root / "unshimmed"
        base_c.mkdir()
        (base_c / "memory.db").write_bytes(b"")
        (base_c / "journal.db").write_bytes(b"")  # real file: no witness
        (base_c / "auto_save").symlink_to(elsewhere / "auto_save")
        with self.assertRaises(SaveValidationError) as ctx:
            _resolve_save_paths("auto_save", "slug", False, str(base_c / "memory.db"))
        self.assertEqual(ctx.exception.code, ErrorCode.TRAVERSAL)


if __name__ == "__main__":
    unittest.main()
