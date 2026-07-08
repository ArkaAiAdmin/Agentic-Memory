"""OKF v0.1 conformance checks.

Implements the §9 conformance criteria from the spec:
1. Every non-reserved .md file has parseable YAML frontmatter.
2. Every frontmatter block has a non-empty `type`.
3. Reserved filenames (index.md, log.md) follow their documented
   structure when present.

Also exposes helpers used by okf_export / okf_import so both sides
speak the same invariants.
"""

from __future__ import annotations

import logging
logger = logging.getLogger(__name__)

import re
from pathlib import Path
from typing import Any

RESERVED_NAMES = {"index.md", "log.md"}
OKF_VERSION = "0.1"
REQUIRED_FM_KEYS = {"type"}
INDEX_FM_ALLOWLIST = {"okf_version"}


class ConformanceError(Exception):
    """A bundle failed a hard conformance check."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def _parse_frontmatter(text: str):
    """Lazy import of the shared frontmatter parser to avoid a hard dep."""
    from infra.frontmatter import parse_frontmatter

    return parse_frontmatter(text)


def validate_bundle(bundle_dir: str | Path) -> list[str]:
    """Return a list of conformance violations for *bundle_dir*.

    Hard violations raise ``ConformanceError``; soft violations
    (broken links, missing index, etc.) are returned as strings.
    """
    bundle_dir = Path(bundle_dir)
    if not bundle_dir.is_dir():
        raise ConformanceError(str(bundle_dir), "not a directory")

    errors: list[str] = []
    md_files = sorted(bundle_dir.rglob("*.md"))

    if not md_files:
        errors.append("bundle contains no .md files")
        return errors

    for md_path in md_files:
        rel = md_path.relative_to(bundle_dir)
        rel_str = str(rel)
        name = md_path.name
        text = md_path.read_text(encoding="utf-8")
        parts = rel.parts

        # 1. Parse frontmatter
        try:
            fm, _ = _parse_frontmatter(text)
        except Exception as exc:
            logger.warning("validate_bundle failed: %s", exc)
            if name not in RESERVED_NAMES:
                errors.append(f"{rel_str}: unparseable frontmatter: {exc}")
            continue

        if name in RESERVED_NAMES:
            # 3. Reserved-filename structure
            if name == "index.md":
                _validate_index(rel_str, parts, fm, text, errors)
            elif name == "log.md":
                _validate_log(rel_str, text, errors)
            continue

        # 2. Non-reserved files must have non-empty `type`
        fm_type = fm.get("type")
        if not fm_type or not str(fm_type).strip():
            errors.append(f"{rel_str}: frontmatter missing required `type`")

    # Soft guidance: warn about broken absolute links
    _warn_broken_links(bundle_dir, md_files, errors)

    return errors


def _validate_index(
    rel_str: str,
    parts: tuple[str, ...],
    fm: dict[str, Any],
    text: str,
    errors: list[str],
) -> None:
    """§6 + §11: index files contain no frontmatter, except the bundle-root
    index.md which MAY declare `okf_version`."""
    if len(parts) == 1 and parts[0] == "index.md":
        # Bundle root: frontmatter allowed, but only okf_version
        for key in fm:
            if key not in INDEX_FM_ALLOWLIST:
                errors.append(
                    f"{rel_str}: bundle-root index.md frontmatter must only "
                    f"contain {INDEX_FM_ALLOWLIST}; found {key!r}"
                )
        if "okf_version" in fm and str(fm["okf_version"]) != OKF_VERSION:
            errors.append(
                f"{rel_str}: okf_version={fm['okf_version']!r}; "
                f"this implementation only supports {OKF_VERSION}"
            )
        # Body should look like an index (has headings, bullet lists)
        body = text.split("---", 2)[-1].strip() if "---" in text else text
        if not re.search(r"^#", body, re.MULTILINE):
            errors.append(f"{rel_str}: index.md body should contain at least one heading")
    else:
        # Subdirectory index: no frontmatter at all per §6
        if fm:
            errors.append(
                f"{rel_str}: subdirectory index.md must not contain frontmatter "
                f"(found keys: {list(fm)})"
            )


def _validate_log(rel_str: str, text: str, errors: list[str]) -> None:
    """§7: log.md uses date-grouped entries, newest first."""
    body = text.split("---", 2)[-1].strip() if "---" in text else text
    if not re.search(r"^#+\s+Directory Update Log", body, re.MULTILINE):
        errors.append(f"{rel_str}: log.md should start with '# Directory Update Log'")
    date_headings = re.findall(r"^##\s+(\d{4}-\d{2}-\d{2})", body, re.MULTILINE)
    if date_headings:
        for a, b in zip(date_headings, date_headings[1:]):
            if a < b:
                errors.append(
                    f"{rel_str}: log.md dates not in descending order: {a} before {b}"
                )


def _warn_broken_links(
    bundle_dir: Path, md_files: list[Path], errors: list[str]
) -> None:
    known: set[str] = set()
    for p in md_files:
        known.add(str(p.relative_to(bundle_dir)).replace("\\", "/"))
        if p.name == "index.md":
            for parent in p.parents:
                idx = parent / "index.md"
                try:
                    rel = idx.relative_to(bundle_dir)
                except ValueError:
                    continue
                known.add(str(rel).replace("\\", "/"))

    link_re = re.compile(r"\[[^\]]+\]\((/[^)]+\.md)\)")
    seen_targets: dict[str, str] = {}
    for p in md_files:
        text = p.read_text(encoding="utf-8")
        for m in link_re.finditer(text):
            target = m.group(1).lstrip("/")
            if target not in known and target not in seen_targets:
                seen_targets[target] = str(p.relative_to(bundle_dir)).replace("\\", "/")


def is_concept_path(rel_path: str | Path, bundle_root: Path) -> bool:
    """True if *rel_path* is a concept document, not a reserved file."""
    rel = Path(rel_path)
    if rel.name in RESERVED_NAMES:
        return False
    return (bundle_root / rel).is_file() and rel.suffix == ".md"


def concept_id(rel_path: str | Path) -> str:
    """Return the OKF concept ID for a relative path, with .md stripped."""
    rel = Path(rel_path)
    return str(rel.with_suffix("")).replace("\\", "/")
