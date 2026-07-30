"""OKF v0.2 conformance checks.

Implements the §11 conformance criteria from the spec:
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
from datetime import datetime
from pathlib import Path
from typing import Any

RESERVED_NAMES = {"index.md", "log.md"}
OKF_VERSION = "0.2"
REQUIRED_FM_KEYS = {"type"}
INDEX_FM_ALLOWLIST = {"okf_version"}
VERIFIED_STATUSES = {"draft", "stable", "deprecated"}
ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?)?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
        else:
            _validate_v02_fields(rel_str, fm, errors)

    # Soft guidance: warn about broken absolute links
    _warn_broken_links(bundle_dir, md_files, errors)

    return errors


def _validate_v02_fields(rel_str: str, fm: dict[str, Any], errors: list[str]) -> None:
    """Validate optional v0.2 frontmatter families when present."""
    # generated: { by: <actor>, at: <iso8601> }
    generated = fm.get("generated")
    if generated is not None:
        if not isinstance(generated, dict):
            errors.append(f"{rel_str}: `generated` must be a mapping")
        else:
            by = generated.get("by")
            at = generated.get("at")
            if not by or not isinstance(by, str):
                errors.append(f"{rel_str}: `generated.by` must be a non-empty string")
            if not at or not ISO8601_RE.match(str(at)):
                errors.append(f"{rel_str}: `generated.at` must be ISO 8601 datetime")

    # verified: list of {by, at} OR bare {by, at} mapping
    verified = fm.get("verified")
    if verified is not None:
        if isinstance(verified, dict):
            by = verified.get("by")
            at = verified.get("at")
            if not by or not isinstance(by, str):
                errors.append(f"{rel_str}: `verified.by` must be a non-empty string")
            if not at or not ISO8601_RE.match(str(at)):
                errors.append(f"{rel_str}: `verified.at` must be ISO 8601 datetime")
        elif isinstance(verified, list):
            for i, entry in enumerate(verified):
                if not isinstance(entry, dict):
                    errors.append(f"{rel_str}: `verified[{i}]` must be a mapping")
                    continue
                by = entry.get("by")
                at = entry.get("at")
                if not by or not isinstance(by, str):
                    errors.append(f"{rel_str}: `verified[{i}].by` must be a non-empty string")
                if not at or not ISO8601_RE.match(str(at)):
                    errors.append(f"{rel_str}: `verified[{i}].at` must be ISO 8601 datetime")
        else:
            errors.append(f"{rel_str}: `verified` must be a list or a mapping")

    # status: draft | stable | deprecated
    status = fm.get("status")
    if status is not None:
        if str(status).strip().lower() not in VERIFIED_STATUSES:
            errors.append(
                f"{rel_str}: `status` must be one of {VERIFIED_STATUSES}; got {status!r}"
            )

    # stale_after: YYYY-MM-DD
    stale_after = fm.get("stale_after")
    if stale_after is not None:
        if not DATE_RE.match(str(stale_after)):
            errors.append(f"{rel_str}: `stale_after` must be YYYY-MM-DD; got {stale_after!r}")

    # sources: list of entries, each with resource
    sources = fm.get("sources")
    if sources is not None:
        if not isinstance(sources, list):
            errors.append(f"{rel_str}: `sources` must be a list")
        else:
            for i, entry in enumerate(sources):
                if not isinstance(entry, dict):
                    errors.append(f"{rel_str}: `sources[{i}]` must be a mapping")
                    continue
                if "resource" not in entry or not entry["resource"]:
                    errors.append(f"{rel_str}: `sources[{i}]` must have a non-empty `resource`")

    # Attested Computation: runtime required
    if fm.get("type") == "Attested Computation":
        runtime = fm.get("runtime")
        if not runtime or not isinstance(runtime, str) or not runtime.strip():
            errors.append(f"{rel_str}: `Attested Computation` requires `runtime`")


def _validate_index(
    rel_str: str,
    parts: tuple[str, ...],
    fm: dict[str, Any],
    text: str,
    errors: list[str],
) -> None:
    """§8 + §11: index files contain no frontmatter, except the bundle-root
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
        # Subdirectory index: no frontmatter at all per §8
        if fm:
            errors.append(
                f"{rel_str}: subdirectory index.md must not contain frontmatter "
                f"(found keys: {list(fm)})"
            )


def _validate_log(rel_str: str, text: str, errors: list[str]) -> None:
    """§9: log.md uses date-grouped entries, newest first."""
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

    for target, source in seen_targets.items():
        errors.append(f"broken link: {source} -> {target}")


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
