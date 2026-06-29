"""
OKF Export — dump the memory database to an Open Knowledge Format directory.

Produces a portable, self-contained directory of markdown files:
  <target>/
    index.md           ← catalog of all exported memories
    <category>/
      <title_slug>.md  ← one file per memory with full frontmatter

Every field (type, resource, tags, created, pinned, etc.) is preserved
in the YAML frontmatter so the export round-trips cleanly through
okf_import.py or any frontmatter-aware tool (Obsidian, Foam, etc.).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("okf_export")

FRONTMATTER_KEYS = [
    "created",
    "updated",
    "observed_at",
    "tags",
    "pinned",
    "type",
    "resource",
    "related",
    "valid_from",
    "valid_to",
    "superseded_by",
    "category",
    "title_slug",
]


def _fmt_list(items) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join(str(i) for i in items) + "]"


def _memory_to_okf(row: dict) -> str:
    """Convert a memory row to OKF markdown with full frontmatter."""
    note_id: str = row["id"]  # "category/title-slug"
    category, title_slug = note_id.split("/", 1)
    content: str = row["content"]
    tags_raw: str = row.get("tags") or "[]"
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except (json.JSONDecodeError, TypeError):
        tags = []
    if isinstance(tags, list):
        tags_str = _fmt_list(tags)
    else:
        tags_str = str(tags)

    pinned_val = row.get("pinned", 0)
    pinned_str = "true" if pinned_val and pinned_val not in (0, "0", False) else "false"

    created = row.get("created_at") or ""
    updated = row.get("updated_at") or ""
    observed = row.get("observed_at") or ""
    valid_from = row.get("valid_from") or ""
    valid_to = row.get("valid_to") or ""
    superseded_by = row.get("superseded_by") or ""

    # Parse metadata JSON for type / resource if present
    memory_type = "note"
    resource = None
    metadata_raw = row.get("metadata")
    if (
        metadata_raw
        and isinstance(metadata_raw, str)
        and metadata_raw not in ("{}", "")
    ):
        try:
            meta = json.loads(metadata_raw)
            if isinstance(meta, dict):
                memory_type = meta.get("type") or memory_type
                resource = meta.get("resource") or resource
        except (json.JSONDecodeError, TypeError):
            pass

    lines = ["---"]
    if created:
        lines.append(f"created: {created}")
    if updated:
        lines.append(f"updated: {updated}")
    if observed:
        lines.append(f"observed_at: {observed}")
    lines.append(f"tags: {tags_str}")
    lines.append(f"pinned: {pinned_str}")
    lines.append(f"type: {memory_type}")
    if resource:
        lines.append(f"resource: {resource}")
    lines.append("related: []")
    if valid_from:
        lines.append(f"valid_from: {valid_from}")
    if valid_to:
        lines.append(f"valid_to: {valid_to}")
    if superseded_by:
        lines.append(f"superseded_by: {superseded_by}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title_slug.replace('-', ' ').title()}")
    lines.append("")
    lines.append(content.strip())
    return "\n".join(lines) + "\n"


def okf_export(
    db_path: str | Path,
    target_dir: str | Path,
    *,
    include_deleted: bool = False,
    overwrite: bool = False,
) -> dict:
    """Export all memories from *db_path* into an OKF directory at *target_dir*.

    Returns a dict with keys: ``exported``, ``skipped``, ``errors``, ``index_path``.
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
    index_entries: list[dict] = []

    for row in rows:
        row_dict = dict(row)
        note_id: str = row_dict["id"]
        try:
            category, title_slug = note_id.split("/", 1)
        except ValueError:
            logger.warning("Skipping row with invalid id=%r", note_id)
            skipped += 1
            continue

        cat_dir = target_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        out_path = cat_dir / f"{title_slug}.md"

        if out_path.exists() and not overwrite:
            skipped += 1
            continue

        try:
            okf_content = _memory_to_okf(row_dict)
            out_path.write_text(okf_content, encoding="utf-8")
            exported += 1
            index_entries.append(
                {
                    "id": note_id,
                    "category": category,
                    "title_slug": title_slug,
                    "path": f"{category}/{title_slug}.md",
                    "created": row_dict.get("created_at", ""),
                    "updated": row_dict.get("updated_at", ""),
                    "tags": row_dict.get("tags", "[]"),
                    "pinned": bool(row_dict.get("pinned", 0)),
                }
            )
        except Exception as e:
            logger.error("Failed to export %s: %s", note_id, e)
            errors += 1

    # Write index.md
    index_path = target_dir / "index.md"
    _write_okf_index(index_path, index_entries)

    conn.close()

    result: dict = {
        "exported": exported,
        "skipped": skipped,
        "errors": errors,
        "index_path": str(index_path),
    }
    if errors:
        result["error"] = f"{errors} memories failed to export"
    return result


def _write_okf_index(index_path: Path, entries: list[dict]):
    """Generate an OKF index.md catalog."""
    from datetime import datetime as _dt

    lines = [
        "---",
        "title: OKF Memory Index",
        f"generated: {_dt.now().isoformat()}",
        f"count: {len(entries)}",
        "---",
        "",
        "# OKF Memory Index",
        "",
        f"**{len(entries)} memories** exported at {_dt.now().isoformat()}.",
        "",
    ]

    # Group by category
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
            created = e.get("created", "")[:10] if e.get("created") else ""
            date_str = f" ({created})" if created else ""
            lines.append(
                f"- [{e['title_slug']}]({e['path']}){date_str}{tag_str}{pinned_mark}"
            )
        lines.append("")

    index_path.write_text("\n".join(lines), encoding="utf-8")


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
