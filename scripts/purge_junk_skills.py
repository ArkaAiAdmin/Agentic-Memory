#!/usr/bin/env python3
"""One-time hygiene sweep: remove junk skills from ~/.agents/skills.

Junk families (eval/test residue, harness tool-echo captures) were
compiled into skill directories by early ungated extraction runs. Their
DB rows are already gone (decay/purge), but the directories remained and
still pollute the agent-facing skills catalog.

Two detection modes:
  1. Name globs   — known auto-generated junk families.
  2. Description  — SKILL.md frontmatter whose description matches
                    harness tool-echo markers (Tool result:, Sub-agent …).

Safety:
  - DRY-RUN by default; pass --execute to actually delete.
  - Never touches ~/.opencode/skills or any dir without SKILL.md.
  - Ambiguous entries (e.g. hand-compiled 'test-skill') are reported as
    skipped-review, not deleted.

Usage:
    venv/bin/python scripts/purge_junk_skills.py             # preview
    venv/bin/python scripts/purge_junk_skills.py --execute   # delete
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import AGENTS_SKILLS_DIR  # noqa: E402

NAME_GLOBS = [
    "search-note-*",
    "stress-test-memory-*",
    "test-memory-number-*",
    "test-note-*",
    "note-to-delete-in-live-test",
    "live-mcp-integration-test-note*",
    "scope-test-memory*",
    "idempotent-guard-test-content",
    "include-invalid-test-content",
    "access-tracking-test-content-query",
    "deep-reranking-cross-encoder-test-content",
    "full-cycle-memory-*",
    "consolidated-session-traj-*",
    "marker-tok-unit-noglob-*",
    "category-lessons-title-slug-*",
    "category-lessons-title-slug-mcp-del-*",
    "mcp-smoke-test-note*",
    "about-to-execute-*",
    "tool-result-*",
    "tool-failure-lesson-*",
    "sub-agent-spawned-*",
    "sub-agent-completed-*",
    "sub-agents-can-report-*",
    "discovery-by-agent-*",
    "modified-file-*",
    "a-long-chunk-test-content*",
    "a-short-test-note*",
    "memory-to-delete*",
    "test-for-importance*",
]

# Hand-named but real skills — never delete even if they look testy.
PROTECT = {"test-skill", "test-skill-final", "TestMcpSaveSkill", "lesson"}

DESC_JUNK_RE = re.compile(
    r"^(Tool result:|Sub-agent (spawned|completed):|\[Discovery by Agent|"
    r"\[Tool Failure Lesson\]|About to execute:)",
    re.IGNORECASE,
)


def _read_description(skill_dir: Path) -> str:
    f = skill_dir / "SKILL.md"
    if not f.exists():
        return ""
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r"^description:\s*(.+?)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def find_junk() -> tuple[list[Path], list[Path]]:
    """Return (junk_dirs, review_dirs) under AGENTS_SKILLS_DIR."""
    base = Path(AGENTS_SKILLS_DIR)
    if not base.exists():
        return [], []
    junk: dict[str, Path] = {}
    review: list[Path] = []
    import fnmatch

    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name in PROTECT:
            continue
        hit = False
        for pat in NAME_GLOBS:
            if fnmatch.fnmatch(d.name, pat):
                hit = True
                break
        if not hit:
            desc = _read_description(d)
            if desc and DESC_JUNK_RE.match(desc):
                hit = True
        if hit:
            junk[d.name] = d
    # Second pass: names that LOOK like residue but weren't matched —
    # surface them for human review instead of deleting silently.
    suspicious_re = re.compile(r"(^test[-_]|smoke|fixture|eval[-_])", re.IGNORECASE)
    for d in sorted(base.iterdir()):
        if d.is_dir() and d.name not in junk and d.name not in PROTECT:
            if suspicious_re.search(d.name):
                review.append(d)
    return list(junk.values()), review


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="Actually delete")
    args = ap.parse_args()

    junk, review = find_junk()
    print(f"{len(junk)} junk skill dir(s) found under {AGENTS_SKILLS_DIR}")
    for d in junk:
        print(f"  {'DEL' if args.execute else 'WOULD DEL'}  {d.name}")
    if review:
        print(f"\n{len(review)} suspicious dir(s) left for manual review:")
        for d in review:
            print(f"  REVIEW  {d.name}")

    if args.execute and junk:
        for d in junk:
            shutil.rmtree(d, ignore_errors=True)
        try:
            from mcp_surface.mcp_common import recompile_skills_catalog

            recompile_skills_catalog()
            print("\nSkills catalog recompiled.")
        except Exception as exc:  # pragma: no cover - best effort
            print(f"\nWARNING: catalog recompile failed: {exc}")
        print(f"Deleted {len(junk)} dir(s).")
    elif not args.execute:
        print("\nDry run — pass --execute to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
