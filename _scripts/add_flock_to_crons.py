#!/usr/bin/env python3
"""Add flock-based locking to all cron scripts.

For each cron/cron_*.py:
  * If the file already imports ``_flock`` or calls
    ``acquire_lock_or_exit``, skip.
  * Otherwise:
      - Insert ``from _flock import acquire_lock_or_exit`` after the
        shebang + module docstring + ``from __future__ import``
        block, immediately before the first real ``import``.
      - Insert ``acquire_lock_or_exit("<cron_name>")`` as the first
        statement of the entry-point function (``main`` or
        ``consolidate_light``), even if that function starts with an
        ``import`` statement.

Idempotent: re-running after the patch is applied is a no-op.

Run from the repo root:
    venv/bin/python _scripts/add_flock_to_crons.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CRON_DIR = REPO / "cron"

CRON_FILES = sorted(CRON_DIR.glob("cron_*.py"))

ENTRY_POINT_OVERRIDES = {
    "cron_consolidate.py": "consolidate_light",
}


def _strip_existing_flock(text: str) -> str:
    """Remove any previously-added ``from _flock import ...`` line
    AND any ``acquire_lock_or_exit(...)`` call so the script can be
    re-patched from a clean state. Idempotent safety net for any
    cron file that already has the import or the call inserted
    (even at the wrong location).
    """
    text = re.sub(
        r"^[ \t]*from _flock import acquire_lock_or_exit[^\n]*\n",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^[ \t]*acquire_lock_or_exit\([^)]*\)\n",
        "",
        text,
        flags=re.MULTILINE,
    )
    return text


def _find_import_insertion_point(text: str) -> int:
    """Return the byte offset where the flock import should be inserted.

    The insertion point is AFTER:
      * the shebang line (`#!`)
      * the module docstring (triple-quoted)
      * any `from __future__ import ...` lines
      * any blank lines
    but BEFORE the first real import (so it groups with the
    cron script's other top-level imports).
    """
    pos = 0

    # 1) Shebang
    m = re.match(r"^#!.*\n", text[pos:])
    if m:
        pos += m.end()

    # 2) Optional encoding declaration
    m = re.match(r"[ \t]*#.*?coding[:=].*\n", text[pos:])
    if m:
        pos += m.end()

    # 3) Optional module docstring
    m = re.match(
        r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'',
        text[pos:],
    )
    if m:
        pos += m.end()

    # 4) Skip blank lines + __future__ imports
    while True:
        m = re.match(r"[ \t]*\n", text[pos:])
        if m:
            pos += m.end()
            continue
        m = re.match(r"from __future__ import[^\n]*\n", text[pos:])
        if m:
            pos += m.end()
            continue
        break

    return pos


def _find_call_insertion_point(text: str, entry_point: str) -> int | None:
    """Return the byte offset for the lock call insertion.

    The insertion point is the FIRST non-docstring, non-blank,
    non-leading-import line inside ``def <entry_point>(...):``.
    We walk past a docstring, blank lines, and any number of
    leading setup lines (including top-of-function imports).
    """
    ep_re = re.compile(
        rf"^def {re.escape(entry_point)}\([^)]*\)(?:\s*->\s*[^:]+)?:\n",
        re.MULTILINE,
    )
    m = ep_re.search(text)
    if not m:
        return None
    pos = m.end()
    rest = text[pos:]

    # Skip docstring.
    ds = re.match(r'\s*"""[\s\S]*?"""|\s*\'\'\'[\s\S]*?\'\'\'', rest)
    if ds:
        pos += ds.end()
        rest = text[pos:]

    # Skip blank lines.
    while True:
        blank = re.match(r"[ \t]*\n", rest)
        if not blank:
            break
        pos += blank.end()
        rest = text[pos:]

    # Find the end of the first "logical block" of statements at the
    # top of the function. The block ends at the first blank line
    # that follows non-blank content. This handles the common
    # pattern of:
    #
    #   def main():
    #       <import x>     <- pos starts here
    #       <os.environ.setdefault(...)>
    #       <blank line>
    #       <real work>
    end = pos
    saw_nonblank = False
    for stmt in re.finditer(r"^[ \t]*[^\n#]*\n", rest, re.MULTILINE):
        line = stmt.group(0)
        if line.strip() == "":
            if saw_nonblank:
                # End of the leading import/setup block.
                break
            continue
        saw_nonblank = True
        end = pos + stmt.end()
    return end


def patch_file(path: Path) -> tuple[bool, str]:
    text = path.read_text(encoding="utf-8")

    # Clean any stray flock import/call from a previous run.
    text = _strip_existing_flock(text)

    if "acquire_lock_or_exit" in text:
        return False, "already has acquire_lock_or_exit"

    cron_name = path.stem
    entry_point = ENTRY_POINT_OVERRIDES.get(path.name, "main")

    import_pos = _find_import_insertion_point(text)
    call_pos = _find_call_insertion_point(text, entry_point)
    if call_pos is None:
        return False, f"could not find def {entry_point}()"

    flock_import = "from _flock import acquire_lock_or_exit\n"
    lock_call = f"    acquire_lock_or_exit({cron_name!r})\n"

    # Insert the call first (the import doesn't shift the call
    # position because we always insert at or before the import
    # site).
    new_text = text[:call_pos] + lock_call + text[call_pos:]
    if call_pos <= import_pos:
        import_pos += len(lock_call)
    new_text = new_text[:import_pos] + flock_import + new_text[import_pos:]

    path.write_text(new_text, encoding="utf-8")
    return True, f"import + call in {entry_point}()"


def main() -> int:
    if not CRON_DIR.is_dir():
        print(f"ERROR: cron dir not found: {CRON_DIR}", file=sys.stderr)
        return 1

    changed = 0
    skipped = 0
    for path in CRON_FILES:
        ok, summary = patch_file(path)
        marker = "OK" if ok else "--"
        print(f"[{marker}] {path.relative_to(REPO)}: {summary}")
        if ok:
            changed += 1
        else:
            skipped += 1

    print()
    print(f"Changed: {changed}  Skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
