"""MCP tools for multi-modal memory ingestion (files, URLs, images, audio, PDF).

Wraps the multi_modal.py ingestion pipeline as @mcp.tool() functions.
"""

import json

from typing import cast

from mcp_surface.mcp_instance import mcp
from mcp_surface.mcp_common import _err, ErrorCode, with_audit
from multi_modal import ingest_file, ingest_url


@mcp.tool()
@with_audit("memory_ingest")
def memory_ingest(
    file_path: str | None = None,
    url: str | None = None,
    category: str = "sessions",
    tags: str = "",
) -> str:
    """Ingest a file or web page URL into the memory system.

    Exactly one of file_path or url must be provided.
    Extracts content (PDF, image, audio, text, markdown, web page) and saves it as a memory note.

    Args:
        file_path: Absolute path to the file to ingest.
        url: Full URL to ingest (e.g. https://example.com/article).
        category: Memory category (default: "sessions").
        tags: Comma-separated tags (e.g. "research,important").
    """
    if file_path and url:
        return _err(ErrorCode.INVALID_PARAMS, "Cannot provide both file_path and url. Provide exactly one.")
    if not file_path and not url:
        return _err(ErrorCode.INVALID_PARAMS, "Must provide either file_path or url.")

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    if file_path:
        result = ingest_file(file_path=file_path, category=category, tags=tag_list)
    else:
        result = ingest_url(url=cast(str, url), category=category, tags=tag_list)

    if "error" in result and result["error"]:
        return _err(ErrorCode.INVALID_PARAMS, result["error"])
    return json.dumps(result)
