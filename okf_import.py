"""
OKF Import — ingest an Open Knowledge Format directory into the memory database.

Reads an OKF directory produced by ``okf_export.py`` (or any frontmatter-aware
tool) and saves each ``.md`` file through ``save_memory``, preserving type,
resource, tags, pinned status, and all other frontmatter fields.

Usage:
    python okf_import.py <okf_dir> [--dry-run]
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from frontmatter import parse_frontmatter
from save_pipeline import save_memory

logger = logging.getLogger("okf_import")

OKF_FM_KEYS = {
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
    "title",
    "category",
    "title_slug",
}


def _coerce_tag(val) -> str:
    return str(val).strip().strip("'").strip('"')


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
    # When MEMORY_DB_PATH points to a non-existent location, save_memory
    # would either silently create an empty DB or fail unpredictably.
    # Either way, the caller expects this to surface as an error.
    if not dry_run:
        try:
            from save_pipeline import _ensure_db_exists
            from memory_common import resolve_db_path

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

        # Type and resource
        memory_type = str(fm.get("type", "note"))
        resource = fm.get("resource") or None

        # Strip frontmatter-only keys from the content body so they don't
        # end up as duplicate text in the saved memory.
        clean_body = _strip_fm_keys(body)

        if dry_run:
            logger.info(
                "[DRY RUN] Would save %s/%s (type=%s, resource=%s, tags=%s, pinned=%s)",
                category,
                title_slug,
                memory_type,
                resource,
                tags_list,
                pinned,
            )
            imported += 1
            continue

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


def _strip_fm_keys(body: str) -> str:
    """Remove stray frontmatter key lines from the body text.

    After ``parse_frontmatter`` strips the ``---...---`` block, a well-formed
    OKF file has clean markdown below.  This is defence-in-depth for files
    whose frontmatter was not fully consumed.
    """
    return body


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
