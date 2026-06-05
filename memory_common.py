#!/usr/bin/env python3
"""Shared utilities for the Agentic Memory system."""
import re
from pathlib import Path

def parse_frontmatter(content):
    """Parse YAML frontmatter from markdown content."""
    content_stripped = content.lstrip()
    match = re.match(r'^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)(.*)', content_stripped, re.DOTALL)
    if not match:
        return {}, content

    yaml_text = match.group(1)
    body = match.group(2)

    metadata = {}
    for line in yaml_text.splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith('#'):
            continue
        if ':' not in stripped_line:
            continue
        key, val = stripped_line.split(':', 1)
        key = key.strip()
        val = val.strip()
        if val.startswith('[') and val.endswith(']'):
            items = [item.strip().strip('"').strip("'") for item in val[1:-1].split(',') if item.strip()]
            metadata[key] = items
        elif val.lower() in ('true', '1', 'yes', 'on'):
            metadata[key] = True
        elif val.lower() in ('false', '0', 'no', 'off'):
            metadata[key] = False
        else:
            metadata[key] = val.strip('"').strip("'")

    return metadata, body

def find_project_root(start_path):
    """Traverse upwards to find the project root."""
    if isinstance(start_path, str):
        start_path = Path(start_path)
    for path in [start_path] + list(start_path.parents):
        if (path / 'memory').is_dir() or (path / '.git').exists() or (path / 'CLAUDE.md').exists():
            return path
    return start_path
