"""
OKF Export — dump the memory database to an Open Knowledge Format directory.

Produces a portable, self-contained directory of markdown files:
  <target>/
    index.md           ← bundle-root index with okf_version frontmatter
    <category>/
      <title_slug>.md  ← one file per memory with full frontmatter

Round-trips cleanly through okf_import.py or any frontmatter-aware tool.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from okf_conformance import OKF_VERSION, validate_bundle

logger = logging.getLogger(__name__)

FRONTMATTER_KEYS = [
    "type",
    "title",
    "description",
    "resource",
    "tags",
    "pinned",
    "generated",
    "verified",
    "status",
    "stale_after",
    "sources",
    "related",
    "valid_from",
    "valid_to",
    "superseded_by",
    "runtime",
    "parameters",
    "computation",
    "executor",
    "attester",
    "created",
    "updated",
    "observed_at",
    "category",
    "title_slug",
]

EXTENSION_KEYS = {
    "created",
    "updated",
    "observed_at",
    "category",
    "title_slug",
    "related",
    "valid_from",
    "valid_to",
    "superseded_by",
    "verified",
    "status",
    "stale_after",
    "sources",
    "runtime",
    "parameters",
    "computation",
    "executor",
    "attester",
}

RESERVED_SLUGS = {"index", "log"}

OKF_INDEX_ALLOWLIST = {"okf_version"}


def _sanitize_title_slug(slug: str) -> str:
    """Make a slug safe for filesystem use without colliding with reserved names."""
    base = slug.strip().lower().replace(" ", "-").replace("_", "-")
    base = "".join(c for c in base if c.isalnum() or c in {"-", "_"})
    if not base:
        base = "untitled"
    if base in RESERVED_SLUGS:
        base = f"_{base}"
    return base


def _derive_title(slug: str, meta: dict[str, Any] | None) -> str:
    """Derive a human title from slug or metadata."""
    if meta:
        title_val = meta.get("title")
        if isinstance(title_val, str) and title_val.strip():
            return title_val.strip()[:120]
    base = slug.split("/", 1)[-1] if "/" in slug else slug
    return base.replace("-", " ").replace("_", " ").strip().title() or "Untitled"


def _fmt_value(value: Any, indent: int = 0) -> str:
    """Serialize a value to YAML with proper multi-line formatting for dicts/lists."""
    prefix = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = []
        for k, v in value.items():
            parts.append(f"{prefix}{k}: {_fmt_value(v, indent + 1)}")
        return "\n" + "\n".join(parts)
    elif isinstance(value, list):
        if not value:
            return "[]"
        parts = []
        for item in value:
            parts.append(f"{prefix}- {_fmt_value(item, indent + 1).lstrip()}")
        return "\n" + "\n".join(parts)
    elif isinstance(value, str):
        return value
    else:
        return json.dumps(value)


def _memory_to_okf(row: dict) -> str:
    """Convert a memory row to an OKF v0.2 markdown document."""
    raw_id: str = row["id"]
    category, title_slug = raw_id.split("/", 1)
    content: str = row["content"]

    # tags
    tags_raw = row.get("tags") or "[]"
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except (json.JSONDecodeError, TypeError):
        tags = []
    if isinstance(tags, list):
        tags_str = ", ".join(str(t) for t in tags if t)
    else:
        tags_str = str(tags)

    # timestamps
    created = row.get("created_at") or ""
    updated = row.get("updated_at") or ""
    observed = row.get("observed_at") or ""
    generated_at = updated or created or observed

    # metadata (type, description, resource, extra keys)
    meta: dict[str, Any] = {}
    metadata_raw = row.get("metadata")
    if (
        metadata_raw
        and isinstance(metadata_raw, str)
        and metadata_raw not in ("{}", "")
    ):
        try:
            parsed = json.loads(metadata_raw)
            if isinstance(parsed, dict):
                meta = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    memory_type = str(meta.get("type") or "note").strip() or "note"
    description = (meta.get("description") or "").strip()
    resource = (meta.get("resource") or "").strip()
    extra_keys = {k: v for k, v in meta.items() if k not in EXTENSION_KEYS}

    title = _derive_title(title_slug, meta)

    valid_from = row.get("valid_from") or ""
    valid_to = row.get("valid_to") or ""
    superseded_by = row.get("superseded_by") or ""

    pinned_val = row.get("pinned", 0)
    pinned_str = "true" if pinned_val and pinned_val not in (0, "0", False) else "false"

    # Build frontmatter in the order used by the spec example
    lines = ["---"]
    lines.append(f"type: {memory_type}")
    lines.append(f"title: {title}")
    if description:
        lines.append(f"description: {description}")
    if resource:
        lines.append(f"resource: {resource}")
    lines.append(f"tags: {tags_str}")
    lines.append(f"pinned: {pinned_str}")

    # v0.2: generated replaces timestamp
    if generated_at:
        lines.append(f"generated:")
        lines.append(f"  by: process:agentic-memory-export")
        lines.append(f"  at: {generated_at}")

    # v0.2: verified
    verified = meta.get("verified")
    if verified is not None:
        lines.append("verified:")
        lines.extend(_fmt_value(verified, indent=1).lstrip("\n").split("\n"))

    # v0.2: status
    status = meta.get("status")
    if status:
        lines.append(f"status: {status}")

    # v0.2: stale_after
    stale_after = meta.get("stale_after")
    if stale_after:
        lines.append(f"stale_after: {stale_after}")

    # v0.2: sources
    sources = meta.get("sources")
    if sources is not None:
        lines.append("sources:")
        lines.extend(_fmt_value(sources, indent=1).lstrip("\n").split("\n"))

    lines.append("related: []")
    if valid_from:
        lines.append(f"valid_from: {valid_from}")
    if valid_to:
        lines.append(f"valid_to: {valid_to}")
    if superseded_by:
        lines.append(f"superseded_by: {superseded_by}")

    # v0.2: Attested Computation fields
    if memory_type == "Attested Computation":
        runtime = meta.get("runtime")
        if runtime:
            lines.append(f"runtime: {runtime}")
        parameters = meta.get("parameters")
        if parameters is not None:
            lines.append("parameters:")
            lines.extend(_fmt_value(parameters, indent=1).lstrip("\n").split("\n"))
        computation = meta.get("computation")
        if computation:
            lines.append(f"computation: {computation}")
        executor = meta.get("executor")
        if executor is not None:
            lines.append("executor:")
            lines.extend(_fmt_value(executor, indent=1).lstrip("\n").split("\n"))
        attester = meta.get("attester")
        if attester is not None:
            lines.append("attester:")
            lines.extend(_fmt_value(attester, indent=1).lstrip("\n").split("\n"))

    # Memory-system extensions
    if created:
        lines.append(f"created: {created}")
    if updated:
        lines.append(f"updated: {updated}")
    if observed:
        lines.append(f"observed_at: {observed}")
    lines.append(f"category: {category}")
    lines.append(f"title_slug: {title_slug}")
    # Preserve any extra metadata keys (round-trip support)
    for k, v in extra_keys.items():
        lines.append(f"{k}: {json.dumps(v) if not isinstance(v, str) else v}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(content.strip())
    return "\n".join(lines) + "\n"


def okf_export(
    db_path: str | Path,
    target_dir: str | Path,
    *,
    include_deleted: bool = False,
    overwrite: bool = False,
    validate: bool = True,
) -> dict:
    """Export all memories from *db_path* into an OKF v0.2 directory at *target_dir*.

    Returns a dict with keys: ``exported``, ``skipped``, ``errors``,
    ``index_path``, and optional ``warnings``.
    """
    db_path = Path(db_path)
    target_dir = Path(target_dir)

    if not db_path.exists():
        return {
            "exported": 0,
            "skipped": 0,
            "errors": 1,
            "error": f"DB not found: {db_path}",
        }

    target_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        where_clause = "" if include_deleted else "WHERE deleted_at IS NULL"
        rows = conn.execute(
            f"SELECT * FROM memories {where_clause} ORDER BY category, id"
        ).fetchall()
    except sqlite3.OperationalError as e:
        conn.close()
        return {"exported": 0, "skipped": 0, "errors": 1, "error": str(e)}

    exported = 0
    skipped = 0
    errors = 0
    warnings: list[str] = []
    index_entries: list[dict] = []

    for row in rows:
        row_dict = dict(row)
        raw_id: str = row_dict["id"]
        try:
            category, title_slug = raw_id.split("/", 1)
        except ValueError:
            logger.warning("Skipping row with invalid id=%r", raw_id)
            skipped += 1
            continue

        safe_slug = _sanitize_title_slug(title_slug)
        if safe_slug != title_slug:
            warnings.append(f"{raw_id}: title_slug sanitized to {safe_slug!r}")

        cat_dir = target_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        out_path = cat_dir / f"{safe_slug}.md"

        # Reserved-filename collision: check original slug before sanitization
        if title_slug in RESERVED_SLUGS:
            out_path = cat_dir / f"{safe_slug}.md"
            warnings.append(f"{raw_id}: renamed to avoid reserved filename collision")

        if out_path.exists() and not overwrite:
            skipped += 1
            continue

        try:
            okf_content = _memory_to_okf(row_dict)
            out_path.write_text(okf_content, encoding="utf-8")
            exported += 1
            index_entries.append(
                {
                    "id": raw_id,
                    "category": category,
                    "title_slug": title_slug,
                    "path": f"{category}/{safe_slug}.md",
                    "title": _derive_title(title_slug, {}),
                    "created": row_dict.get("created_at", ""),
                    "updated": row_dict.get("updated_at", ""),
                    "tags": row_dict.get("tags", "[]"),
                    "pinned": bool(row_dict.get("pinned", 0)),
                }
            )
        except Exception as e:
            logger.error("Failed to export %s: %s", raw_id, e)
            errors += 1

    # Write bundle-root index.md with okf_version frontmatter per spec §12
    bundle_index = target_dir / "index.md"
    index_lines = ["---", f"okf_version: {OKF_VERSION}", "---", "", "# OKF Memory Index", ""]
    index_lines.append(f"**{len(index_entries)} memories** exported at {__import__('datetime').datetime.now().isoformat()}.")
    index_lines.append("")
    _write_okf_index_body(index_lines, index_entries)
    bundle_index.write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    conn.close()

    result: dict = {
        "exported": exported,
        "skipped": skipped,
        "errors": errors,
        "index_path": str(bundle_index),
        "warnings": warnings,
    }
    if errors:
        result["error"] = f"{errors} memories failed to export"
    if validate:
        try:
            violations = validate_bundle(target_dir)
        except Exception as exc:
            logger.warning("OKF conformance check failed: %s", exc)
        else:
            if violations:
                result["warnings"] = (result.get("warnings") or []) + violations
    return result


def _write_okf_index_body(lines: list[str], entries: list[dict]) -> None:
    """Append category-grouped concept listings to an index body."""
    by_cat: dict[str, list] = {}
    for e in entries:
        by_cat.setdefault(e["category"], []).append(e)

    for category in sorted(by_cat):
        cat_entries = by_cat[category]
        lines.append(f"## {category} ({len(cat_entries)})")
        lines.append("")
        for e in cat_entries:
            tags_raw = e.get("tags", "[]")
            try:
                tag_list = (
                    json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
                )
            except (json.JSONDecodeError, TypeError):
                tag_list = []
            tag_str = f" [{', '.join(str(t) for t in tag_list)}]" if tag_list else ""
            pinned_mark = " 📌" if e.get("pinned") else ""
            created = (e.get("created") or "")[:10]
            date_str = f" ({created})" if created else ""
            title = e.get("title") or e["title_slug"].replace("-", " ").title()
            lines.append(
                f"- [{title}]({e['path']}){date_str}{tag_str}{pinned_mark}"
            )
        lines.append("")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    db = sys.argv[1] if len(sys.argv) > 1 else None
    target = sys.argv[2] if len(sys.argv) > 2 else None
    if not db or not target:
        print("Usage: python okf_export.py <memory.db> <target_dir>")
        sys.exit(1)
    result = okf_export(db, target)
    print(json.dumps(result, indent=2))
    if result.get("error"):
        sys.exit(1)
