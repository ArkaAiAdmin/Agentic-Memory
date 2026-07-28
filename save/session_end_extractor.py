from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

# Patterns matching notable technical findings and lessons
FINDING_PATTERNS = [
    r"(?i)\b(fixed|resolved|solved)\s*:\s*(.+)",
    r"(?i)\b(root\s*cause)\s*:\s*(.+)",
    r"(?i)\b(bug|error|issue)\s*:\s*(.+)",
    r"(?i)\b(lesson|learned|takeaway|workflow)\s*:\s*(.+)",
    r"(?i)\b(workaround)\s*:\s*(.+)",
]

def extract_session_findings(marker: dict) -> dict:
    """Scan notes updated in the active session for technical findings/lessons
    and write them as category='lessons' notes with importance=3.
    """
    first_tool_at = marker.get("first_tool_at")
    if not first_tool_at:
        return {"scanned": 0, "extracted": 0}

    from infra.infrastructure import resolve_active_memory_dir
    from infra.db import open_db

    db_path = resolve_active_memory_dir() / "memory.db"
    if not db_path.exists():
        return {"scanned": 0, "extracted": 0}

    # Connect via open_db (sets up tenant_memories TEMP VIEW + connection pool)
    with open_db(db_path) as conn:
        # Convert first_tool_at to ISO8601 string
        cutoff_iso = datetime.fromtimestamp(first_tool_at, timezone.utc).isoformat()
        
        # Query memories modified in this session, excluding 'lessons' and deleted ones
        rows = conn.execute(
            "SELECT id, content FROM tenant_memories WHERE updated_at >= ? "
            "AND (category IS NULL OR category != 'lessons') AND deleted_at IS NULL",
            (cutoff_iso,)
        ).fetchall()
        
        scanned_count = len(rows)
        extracted_count = 0
        
        for mid, content in rows:
            if not content:
                continue
            
            lines = content.split("\n")
            for line in lines:
                line_strip = line.strip()
                if not line_strip:
                    continue
                # Clean markdown prefixes like bullet list symbols
                cleaned = re.sub(r"^[-*+]\s+|^[0-9]+\.\s+", "", line_strip)
                for pattern in FINDING_PATTERNS:
                    match = re.search(pattern, cleaned)
                    if match:
                        prefix = match.group(1).strip()
                        detail = match.group(2).strip()
                        # Clean up trailing punctuation or formatting
                        detail_clean = re.sub(r"[#*_`]+$", "", detail).strip()
                        if len(detail_clean) > 10:
                            # Verify if similar lesson already exists
                            dupe_check = (
                                conn.execute(
                                    "SELECT COUNT(*) FROM tenant_memories WHERE category='lessons' "
                                    "AND content LIKE ? AND deleted_at IS NULL",
                                    (f"%{detail_clean[:100]}%",)
                                ).fetchone()
                            )
                            if dupe_check is not None:
                                dupe_count = dupe_check[0]
                            else:
                                dupe_count = 0
                            
                            if dupe_check == 0:
                                # Save finding as lesson memory note
                                title = f"{prefix.capitalize()}: {detail_clean[:60]}"
                                if len(detail_clean) > 60:
                                    title += "..."
                                
                                # Format lesson markdown
                                lesson_content = (
                                    f"# Lesson: {title}\n\n"
                                    f"**Session Finding:** {cleaned}\n"
                                    f"**Source Note:** {mid}\n"
                                    f"**Extracted at:** {datetime.now(timezone.utc).isoformat()}\n\n"
                                    f"### Details\n"
                                    f"{detail_clean}\n"
                                )
                                
                                tags = ["session-finding", "auto-lesson"]
                                if prefix.lower() in ["fixed", "resolved", "solved", "bug", "error", "issue"]:
                                    tags.append("bugfix")
                                if prefix.lower() in ["workaround"]:
                                    tags.append("workaround")
                                
                                # Use save_memory_auto for proper indexing and hooks
                                from save.pipeline import save_memory_auto
                                slug = f"session-finding-{uuid4().hex[:8]}"
                                save_memory_auto(
                                    content=lesson_content,
                                    category="lessons",
                                    title_slug=slug,
                                    tags=tags,
                                    pinned=False,
                                    importance=3,
                                    safety_wiring=False,
                                )
                                extracted_count += 1
                                break # only one match per line
                                
        return {"scanned": scanned_count, "extracted": extracted_count}
