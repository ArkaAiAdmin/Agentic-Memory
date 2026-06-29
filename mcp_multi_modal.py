"""MCP tools for multi-modal memory ingestion (files, URLs, images, audio, PDF).

Wraps the multi_modal.py ingestion pipeline as @mcp.tool() functions.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401

import json

from mcp_instance import mcp
from mcp_common import _err, ErrorCode, with_audit
from multi_modal import ingest_file, ingest_url


@mcp.tool()
@with_audit("memory_ingest_file")
def memory_ingest_file(
    file_path: str,
    category: str = "sessions",
    tags: str = "",
) -> str:
    """Ingest a file (PDF, image, audio, text, markdown, URL file) into the memory system.

    Extracts text content from the file and saves it as a memory note.
    Supported formats: .txt, .md, .pdf, .jpg, .png, .webp, .mp3, .wav, .m4a, .url.
    Optional dependencies: pymupdf (PDF), pytesseract+PIL (images), faster-whisper (audio).

    Args:
        file_path: Absolute path to the file to ingest.
        category: Memory category (default: "sessions").
        tags: Comma-separated tags (e.g. "pdf,research,important").
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    result = ingest_file(file_path=file_path, category=category, tags=tag_list)
    if "error" in result and result["error"]:
        return _err(ErrorCode.INVALID_PARAMS, result["error"])
    return json.dumps(result)


@mcp.tool()
@with_audit("memory_ingest_url")
def memory_ingest_url(
    url: str,
    category: str = "sessions",
    tags: str = "",
) -> str:
    """Ingest a web page URL into the memory system.

    Downloads the page, extracts readable content via readability-lxml,
    and saves as a memory note.

    Args:
        url: Full URL to ingest (e.g. https://example.com/article).
        category: Memory category (default: "sessions").
        tags: Comma-separated tags (e.g. "web,research").
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    result = ingest_url(url=url, category=category, tags=tag_list)
    if "error" in result and result["error"]:
        return _err(ErrorCode.INVALID_PARAMS, result["error"])
    return json.dumps(result)
