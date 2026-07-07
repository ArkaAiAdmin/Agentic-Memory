"""
OKF export/import MCP tools — memory_okf_export, memory_okf_import.

Exports memories to or imports from an Open Knowledge Format directory
of markdown files with full YAML frontmatter, compatible with Obsidian,
Foam, and other frontmatter-aware tools.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401

from pathlib import Path


import json
from mcp_common import _err, ErrorCode, logger, with_audit
from mcp_instance import mcp


def _enforce_okf_export_containment(output_dir: str) -> Path:
    """S5 — ensure the OKF export target stays within the memory root.

    The export operation writes the entire corpus (a data-exfiltration
    surface) to ``output_dir``. We require the resolved path to remain
    inside the active memory directory so a caller cannot redirect the
    full corpus to an arbitrary filesystem location.
    """
    from infra.infrastructure import resolve_active_memory_dir

    root = resolve_active_memory_dir().resolve()
    target = Path(output_dir).resolve()
    # Accept the memory root itself or any subdirectory beneath it.
    if target != root and not target.is_relative_to(root):
        raise ValueError(
            f"OKF export path containment: {output_dir} resolves to {target} "
            f"which is outside the memory root {root}"
        )
    return target


@mcp.tool()
@with_audit("memory_okf_export")
def memory_okf_export(
    output_dir: str,
    include_deleted: bool = False,
    overwrite: bool = False,
) -> str:
    """Export all memories to an OKF (Open Knowledge Format) directory.

    Produces one ``.md`` file per memory with full YAML frontmatter
    (created, updated, tags, pinned, type, resource, etc.) and a
    top-level ``index.md`` catalog.

    The output directory is portable — open it in Obsidian, Foam, or
    any frontmatter-aware markdown tool.

    Parameters
    ----------
    output_dir : str
        Directory to write OKF files into (created if it doesn't exist).
        Must resolve within the active memory directory (S5 path
        containment).
    include_deleted : bool
        Also export soft-deleted memories (default: false).
    overwrite : bool
        Overwrite existing files without skipping (default: false).
    """
    import okf_export as oe

    try:
        target = _enforce_okf_export_containment(output_dir)
    except ValueError as ve:
        return _err(ErrorCode.INVALID_PARAMS, str(ve))

    from infra.infrastructure import resolve_active_memory_dir

    db_path = resolve_active_memory_dir() / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"Memory DB not found at {db_path}")

    target = Path(output_dir)
    try:
        result = oe.okf_export(
            db_path,
            target,
            include_deleted=include_deleted,
            overwrite=overwrite,
        )
        return json.dumps(result, indent=2)
    except Exception:
        logger.exception("OKF export failed")
        return _err(ErrorCode.EXPORT_ERROR, "OKF export failed")


@mcp.tool()
@with_audit("memory_okf_import")
def memory_okf_import(
    input_dir: str,
    is_global: bool = False,
    dry_run: bool = False,
    overwrite: bool = False,
    confirm: bool = False,
) -> str:
    """Import memories from an OKF (Open Knowledge Format) directory.

    Reads ``.md`` files created by ``memory_okf_export`` (or any
    frontmatter-aware tool) and saves each through the memory system,
    preserving type, resource, tags, pinned status, and category.

    Parameters
    ----------
    input_dir : str
        Directory containing OKF ``.md`` files (excluding index.md).
    is_global : bool
        Import to the global memory store instead of project-local (default: false).
    dry_run : bool
        Preview what would be imported without writing (default: false).
    overwrite : bool
        Overwrite existing memories if they already exist (default: false).
        Requires ``confirm=True`` (S6) in addition to the router-level
        destructive-operation confirmation gate.
    confirm : bool
        Explicit confirmation required when ``overwrite=True`` (S6).
    """
    # S6: overwrite is a destructive merge; require explicit confirmation.
    if overwrite and not confirm:
        return _err(
            ErrorCode.INVALID_PARAMS,
            "OKF import with overwrite=True requires confirm=True.",
        )

    import okf_import as oi

    source = Path(input_dir)
    if not source.is_dir():
        return _err(ErrorCode.INVALID_PARAMS, f"Directory not found: {input_dir}")

    try:
        result = oi.okf_import(
            source,
            is_global=is_global,
            dry_run=dry_run,
            overwrite=overwrite,
        )
        return json.dumps(result, indent=2)
    except Exception:
        logger.exception("OKF import failed")
        return _err(ErrorCode.IMPORT_ERROR, "OKF import failed")
