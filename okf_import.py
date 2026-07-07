"""
OKF Import — ingest an Open Knowledge Format directory into the memory database.

Reads an OKF directory produced by ``okf_export.py`` (or any frontmatter-aware
tool) and saves each ``.md`` file through ``save_memory``, preserving type,
resource, description, timestamp, tags, pinned status, related, valid_from,
valid_to, superseded_by, category, title_slug, and all other frontmatter fields.

Round-trips cleanly through okf_export.py.

Usage:
    python okf_import.py <okf_dir> [--dry-run]
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from infra.frontmatter import parse_frontmatter
from save_pipeline import save_memory

logger = logging.getLogger("okf_import")

# OKF standard frontmatter keys that the spec mentions or that save_memory
# also writes into its own generated frontmatter block.
OKF_STANDARD_KEYS = {
    "type",
    "title",
    "description",
    "resource",
    "tags",
    "pinned",
    "timestamp",
    "related",
    "valid_from",
    "valid_to",
    "superseded_by",
    "created",
    "updated",
    "observed_at",
    "category",
    "title_slug",
}

# Keys that are internal to this memory system and should not be surfaced
# as top-level OKF frontmatter in the re-emitted body.
INTERNAL_KEYS = {
    "category",
    "title_slug",
    "tags",
    "pinned",
    "created",
    "updated",
    "observed_at",
    "valid_from",
    "valid_to",
    "superseded_by",
    "related",
    "metadata",
}


def _coerce_tag(val) -> str:
    return str(val).strip().strip("'").strip('"')


def _collect_metadata(fm: dict) -> dict:
    """Return a metadata dict from non-standard frontmatter keys.

    The spec-compliant keys stay as top-level OKF frontmatter. Everything
    else is treated as producer-defined metadata and MUST round-trip.
    """
    out: dict = {}
    for k, v in fm.items():
        if k in OKF_STANDARD_KEYS or k in ("okf_version",):
            continue
        out[k] = v
    return out


def _reconstruct_body_with_metadata(original_body: str, metadata: dict) -> str:
    """Re-emit the body with a compact metadata frontmatter block so
    save_memory's _build_memory_file can capture it in the DB metadata
    column without losing prose that followed the original frontmatter."""
    if not metadata:
        return original_body
    lines = ["---", f"metadata: {json.dumps(metadata, ensure_ascii=False)}", "---", ""]
    return "\n".join(lines) + ("\n" + original_body if original_body else "")


def okf_import(
    source_dir: str | Path,
    *,
    is_global: bool = False,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict:
    """Import all ``.md`` files from *source_dir* (except index.md) as memories.

    Returns a dict with keys: ``imported``, ``skipped``, ``errors``, ``dry_run``.
    """
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        return {
            "imported": 0,
            "skipped": 0,
            "errors": 1,
            "error": f"Directory not found: {source_dir}",
        }

    imported = 0
    skipped = 0
    errors = 0
    error_details: list[str] = []

    # Collect all .md files, excluding index.md
    md_files: list[Path] = []
    for entry in sorted(source_dir.rglob("*.md")):
        rel = entry.relative_to(source_dir)
        if rel == Path("index.md"):
            continue
        if entry.is_file():
            md_files.append(entry)

    if not md_files:
        return {
            "imported": 0,
            "skipped": 0,
            "errors": 0,
            "error": f"No .md files found in {source_dir}",
        }

    # Pre-check: if not dry_run, verify the target DB is accessible.
    if not dry_run:
        try:
            from save_pipeline import _ensure_db_exists
            from infra.memory_common import resolve_db_path

            target_db = resolve_db_path()
            if not _ensure_db_exists(target_db):
                return {
                    "imported": 0,
                    "skipped": 0,
                    "errors": len(md_files),
                    "dry_run": dry_run,
                    "error": f"Target database not accessible: {target_db}",
                }
        except Exception:
            pass  # Let save_memory handle it

    for file_path in md_files:
        rel = file_path.relative_to(source_dir)
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Cannot read %s: %s", rel, e)
            errors += 1
            error_details.append(f"{rel}: read error: {e}")
            continue

        fm, body = parse_frontmatter(content)
        if not body:
            body = content

        # Determine category from directory structure
        category = fm.get("category") or (
            rel.parent.name if rel.parent.name != "." else "imported"
        )

        # Determine title_slug from filename
        stem = fm.get("title_slug") or rel.stem
        title_slug = stem.strip().replace(" ", "-").replace("_", "-").lower()

        # Tags
        tags_raw = fm.get("tags", [])
        if isinstance(tags_raw, str):
            try:
                tags_list = (
                    json.loads(tags_raw)
                    if tags_raw.startswith("[")
                    else [
                        t.strip() for t in tags_raw.strip("[]").split(",") if t.strip()
                    ]
                )
            except (json.JSONDecodeError, TypeError):
                tags_list = [tags_raw]
        elif isinstance(tags_raw, list):
            tags_list = [_coerce_tag(t) for t in tags_raw if t]
        else:
            tags_list = []

        # Pinned
        pinned_raw = fm.get("pinned", False)
        pinned = (
            bool(pinned_raw)
            if not isinstance(pinned_raw, str)
            else pinned_raw.lower() in ("true", "1", "yes")
        )

        # Standard OKF / memory fields that we re-emit into the body so
        # save_memory preserves them in the DB.
        keep_keys = {k: fm[k] for k in (
            "type",
            "title",
            "description",
            "resource",
            "timestamp",
            "related",
            "valid_from",
            "valid_to",
            "superseded_by",
        ) if k in fm and fm[k] not in (None, "", [])}
        extra_metadata = _collect_metadata(fm)
        merged_metadata = {**extra_metadata, **keep_keys}
        clean_body = _reconstruct_body_with_metadata(body, merged_metadata)

        if dry_run:
            logger.info(
                "[DRY RUN] Would save %s/%s (type=%s, resource=%s, tags=%s, pinned=%s, metadata_keys=%s)",
                category,
                title_slug,
                fm.get("type", "note"),
                fm.get("resource"),
                tags_list,
                pinned,
                sorted(merged_metadata.keys()),
            )
            imported += 1
            continue

        try:
            from save_pipeline import SaveValidationError
            try:
                save_result = save_memory(
                    content=clean_body,
                    category=category,
                    title_slug=title_slug,
                    tags=tags_list,
                    pinned=pinned,
                    is_global=is_global,
                    safety_wiring=False,
                    db_path=os.environ.get("MEMORY_DB_PATH"),
                )
            except SaveValidationError as e:
                save_result = str(e)
            if isinstance(save_result, str) and save_result.startswith("Error "):
                logger.error(
                    "save_memory failed for %s/%s: %s",
                    category,
                    title_slug,
                    save_result,
                )
                errors += 1
                error_details.append(f"{rel}: save error: {save_result}")
            else:
                imported += 1
        except Exception as e:
            logger.error("Exception importing %s: %s", rel, e)
            errors += 1
            error_details.append(f"{rel}: exception: {e}")

    result: dict[str, object] = {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
    }
    if error_details:
        result["error_details"] = error_details[:20]
        result["error"] = f"{errors} memories failed to import"
    return result


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}

    source = args[0] if args else None
    if not source:
        print("Usage: python okf_import.py <okf_dir> [--dry-run]")
        sys.exit(1)

    result = okf_import(source, dry_run="--dry-run" in flags)
    print(json.dumps(result, indent=2))
    if result.get("error") and result.get("errors", 0) > 0:
        sys.exit(1)
