#!/usr/bin/env python3
"""Multi-modal memory ingestion pipeline (image, audio, PDF, web).

Converts diverse input formats to markdown notes, then processes through
the existing save_pipeline indexers. The architecture separates ingestion
(here) from indexing (save_pipeline.py), so adding new input formats
doesn't change the core memory system.

Supported input types:
  - .txt, .md      → direct read
  - .pdf            → pymupdf text extraction (optional: marker-pdf for vision)
  - .jpg/.png/.webp → OCR via pytesseract + caption via BLIP (optional)
  - .mp3/.wav/.m4a  → transcription via faster-whisper (optional)
  - URLs             → web scraping via readability-lxml (optional)

All optional dependencies are lazy-imported; the module works gracefully
without them (returns a note explaining that a format isn't supported).
"""

from __future__ import annotations

import logging

import os
import urllib.request
from html.parser import HTMLParser
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from infra.ssrf import _SSRFRedirectHandler, _ssrf_validate_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security configuration
# ---------------------------------------------------------------------------

# SSRF guard (OWASP A10-001).
# If non-empty, ONLY these hosts (verified after DNS resolution and after every
# redirect) are permitted. An empty set means "any host that is not loopback,
# link-local, or private".
ALLOWED_HOSTS: frozenset[str] = frozenset()

# Path containment (OWASP A04-002).
# Files may only be ingested if their resolved real path stays within this
# directory. Override with the AGENTIC_MEMORY_INGEST_DIR env var.
INGEST_ALLOWED_DIR = Path(
    os.environ.get("AGENTIC_MEMORY_INGEST_DIR", str(Path(__file__).resolve().parent))
).resolve()


# ---------------------------------------------------------------------------
# SSRF guard (OWASP A10-001) — shared implementation in infra/ssrf.py
# ---------------------------------------------------------------------------

_opener = urllib.request.build_opener(_SSRFRedirectHandler(allowed_hosts=ALLOWED_HOSTS))


# ---------------------------------------------------------------------------
# Path containment (OWASP A04-002)
# ---------------------------------------------------------------------------


def _enforce_path_containment(raw_path: Path) -> Path:
    """Resolve a path and ensure it stays within INGEST_ALLOWED_DIR.

    Rejects absolute paths and symlink/hardlink escapes that resolve outside
    the allowed root.
    """
    resolved = raw_path.resolve()
    if not resolved.is_relative_to(INGEST_ALLOWED_DIR):
        raise ValueError(
            f"Path containment: {raw_path} resolves to {resolved} "
            f"which is outside the allowed directory {INGEST_ALLOWED_DIR}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Entry point API
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS = frozenset(
    {
        ".txt",
        ".md",
        ".pdf",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".mp3",
        ".wav",
        ".m4a",
        ".url",
    }
)


def ingest_file(
    file_path: str | Path,
    category: str = "sessions",
    tags: Optional[list[str]] = None,
    memory_dir: Optional[str] = None,
) -> dict:
    """Ingest a file into the memory system.

    Args:
        file_path: Path to the file to ingest.
        category: Memory category (default: "sessions").
        tags: Optional list of tags.
        memory_dir: Override the memory directory for note placement.

    Returns:
        Dict with keys: note_id, format, content_preview, error (if failed).
    """
    path = Path(file_path)
    try:
        path = _enforce_path_containment(path)
    except ValueError as e:
        return {"note_id": "", "format": "", "error": str(e)}

    if not path.exists():
        return {"note_id": "", "format": "", "error": f"File not found: {path}"}

    fmt = path.suffix.lower()
    if fmt not in SUPPORTED_FORMATS:
        return {
            "note_id": "",
            "format": fmt,
            "error": f"Unsupported format: {fmt}. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}",
        }

    try:
        content, metadata = _extract_content(path, fmt)
    except Exception as e:
        logger.exception("ingest failed for %s", file_path)
        return {"note_id": "", "format": fmt, "error": str(e)}

    if not content or not content.strip():
        return {"note_id": "", "format": fmt, "error": "No content extracted"}

    # Save through the standard pipeline
    from save_pipeline import SaveValidationError
    from infra._lazy_imports import save_memory_auto

    title_slug = _slugify(path.stem)
    try:
        note_id = save_memory_auto(
            content=content,
            category=category,
            title_slug=title_slug,
            tags=tags or [fmt[1:], "ingested"],
            pinned=False,
            is_global=False,
            safety_wiring=True,
        )
    except SaveValidationError as e:
        return {"note_id": "", "format": fmt, "error": str(e)}

    return {
        "note_id": note_id,
        "format": fmt[1:],
        "content_preview": content[:200],
        "metadata": metadata,
    }


def ingest_url(
    url: str,
    category: str = "sessions",
    tags: Optional[list[str]] = None,
) -> dict:
    """Ingest a web page into the memory system.

    Downloads the page, extracts readable content via readability-lxml,
    converts to markdown, and saves as a memory note.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return {"note_id": "", "error": f"Invalid URL: {url}"}

    # SSRF guard (OWASP A10-001): reject internal/metadata addresses before fetch.
    try:
        _ssrf_validate_url(url, allowed_hosts=ALLOWED_HOSTS)
    except ValueError as e:
        return {"note_id": "", "error": str(e)}

    title = parsed.netloc
    content = f"# Web page: {url}\n\n"
    content += f"**Source:** {url}\n**Fetched:** {__import__('datetime').datetime.now().isoformat()}\n\n---\n\n"

    try:
        # Try readability-lxml if available
        import urllib.request
        import readability
        import html2text

        req = urllib.request.Request(url, headers={"User-Agent": "AgenticMemory/1.0"})
        with _opener.open(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        doc = readability.Document(html)
        title = doc.short_title() or parsed.netloc
        body = html2text.html2text(doc.summary())

        content += f"**Title:** {title}\n\n"
        content += body[:5000]  # cap at 5K chars

    except ImportError:
        # Fallback: basic fetch + text extraction
        import urllib.request
        import html.parser

        class _TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self._text = []
                self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    self._text.append(data)

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style"):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style"):
                    self._skip = False

            def result(self):
                return " ".join(self._text)

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "AgenticMemory/1.0"}
            )
            with _opener.open(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            extractor = _TextExtractor()
            extractor.feed(html)
            text = extractor.result()
            content += text[:5000]
        except Exception as e:
            logger.warning("ingest_url failed: %s", e)
            return {"note_id": "", "error": f"URL fetch failed: {e}"}

    except Exception as e:
        logger.warning("ingest_url failed: %s", e)
        return {"note_id": "", "error": f"URL processing failed: {e}"}

    return _save_content(content, category, tags or ["web", "url"], _slugify(title))


# ---------------------------------------------------------------------------
# Internal extractors
# ---------------------------------------------------------------------------


def _extract_content(path: Path, fmt: str) -> tuple[str, dict]:
    """Extract text content from a file based on its format."""
    metadata = {"source": str(path), "format": fmt, "size": path.stat().st_size}
    content = ""

    if fmt in (".txt", ".md"):
        content = path.read_text(encoding="utf-8", errors="replace")
        content = f"# {path.stem}\n\n{content}"

    elif fmt == ".pdf":
        content = _extract_pdf(path)
        content = f"# PDF: {path.name}\n\n{content}"

    elif fmt in (".jpg", ".jpeg", ".png", ".webp"):
        content = _extract_image(path)

    elif fmt in (".mp3", ".wav", ".m4a"):
        content = _extract_audio(path)

    elif fmt == ".url":
        url = path.read_text(encoding="utf-8").strip()
        result = ingest_url(url)
        content = result.get("content_preview", "")
        metadata["url"] = url

    return content, metadata


def _extract_pdf(path: Path) -> str:
    """Extract text from a PDF using pymupdf."""
    try:
        import pymupdf

        doc = pymupdf.open(str(path))
        text = "\n\n".join(page.get_text() for page in doc)
        return text[:10000]
    except ImportError:
        return f"[PDF ingestion requires pymupdf: pip install agentic-memory[pdf]]\n\nFile: {path}"


def _extract_image(path: Path) -> str:
    """Extract text from an image via OCR. Falls back to gracefully unsupported."""
    try:
        import pytesseract
        from PIL import Image

        text = pytesseract.image_to_string(Image.open(path))
        return f"# Image: {path.name}\n\n## OCR Text\n\n{text}"
    except ImportError:
        return f"[Image ingestion requires: pip install agentic-memory[vision,ocr]]\n\nFile: {path}"


def _extract_audio(path: Path) -> str:
    """Transcribe audio via faster-whisper."""
    try:
        import faster_whisper

        model = faster_whisper.WhisperModel("base", device="cpu")
        segments, _ = model.transcribe(str(path))
        text = " ".join(seg.text for seg in segments)
        return f"# Audio Transcript: {path.name}\n\n{text}"
    except ImportError:
        return f"[Audio transcription requires: pip install agentic-memory[speech]]\n\nFile: {path}"


def _save_content(content: str, category: str, tags: list, title_slug: str) -> dict:
    """Save content through the standard pipeline."""
    from save_pipeline import SaveValidationError
    from infra._lazy_imports import save_memory_auto

    try:
        note_id = save_memory_auto(
            content=content,
            category=category,
            title_slug=title_slug,
            tags=tags,
            pinned=False,
            is_global=True,
        )
        return {
            "note_id": note_id,
            "content_preview": content[:200],
            "tags": tags,
        }
    except SaveValidationError as e:
        return {
            "note_id": "",
            "content_preview": "",
            "tags": tags,
            "error": str(e),
        }


def _slugify(text: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip().lower())[:max_len].strip("-")
