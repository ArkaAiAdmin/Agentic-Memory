"""YAML frontmatter parsing for memory notes.

Extracted from memory_common.py during the 6-module refactor.

Provides:
  * ``parse_frontmatter(content)``: returns (metadata, body).
  * ``_coerce(val)``: best-effort string-to-type coercion (private).
"""
from __future__ import annotations

import re

__all__ = ['parse_frontmatter']


def _coerce(val):
    """Coerce a string value to its most likely type."""
    if val == '':
        return ''
    if val.startswith('[') and val.endswith(']'):
        inner = val[1:-1]
        items = [it.strip().strip('"').strip("'") for it in inner.split(',') if it.strip()]
        return items
    low = val.lower()
    if low in ('true', '1', 'yes', 'on'):
        return True
    if low in ('false', '0', 'no', 'off'):
        return False
    return val.strip('"').strip("'")


def parse_frontmatter(content):
    """Parse YAML frontmatter from markdown content.

    Returns (metadata, body). Handles:
    - Optional leading whitespace before opening `---`.
    - CRLF or LF line endings.
    - Multi-line continuation values (indented lines fold into the previous key).
    - Inline `[a, b, c]` and JSON-style `["a", "b"]` tag lists.
    - Booleans (`true`/`false`/`yes`/`no`/`on`/`off`).

    The regex is deliberately non-greedy and the closer is guarded by a
    lookbehind for a newline so a YAML continuation line that starts with
    `---` is not mistaken for the frontmatter close.
    """
    content_stripped = content.lstrip('\ufeff').lstrip()
    m = re.match(
        '\\A---\\s*\\r?\\n(.*?)\\r?\\n---\\s*(?:\\r?\\n|\\Z)(.*)',
        content_stripped,
        re.DOTALL,
    )
    if not m:
        return ({}, content)
    yaml_text = m.group(1)
    body = m.group(2)
    metadata = {}
    pending_key = None
    pending_val_parts = []
    pending_list = None
    for line in yaml_text.splitlines():
        raw = line
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if (
            pending_key is not None
            and (raw.startswith('  ') or raw.startswith('\t') or raw.startswith('- '))
            and stripped.startswith('- ')
        ):
            item = stripped[2:].strip()
            if item:
                if pending_list is None:
                    pending_list = []
                pending_list.append(_coerce(item))
            continue
        if (
            pending_key is not None
            and (raw.startswith(' ') or raw.startswith('\t'))
            and (':' not in stripped.split('#', 1)[0])
        ):
            pending_val_parts.append(stripped)
            continue
        if pending_key is not None:
            if pending_list is not None:
                metadata[pending_key] = pending_list
            else:
                metadata[pending_key] = _coerce(' '.join(pending_val_parts).strip())
            pending_list = None
            pending_val_parts = []
        if ':' not in stripped:
            continue
        key, _, val = stripped.partition(':')
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        pending_key = key
        pending_val_parts = [val] if val else []
    if pending_key is not None:
        if pending_list is not None:
            metadata[pending_key] = pending_list
        else:
            metadata[pending_key] = _coerce(' '.join(pending_val_parts).strip())
    return (metadata, body)
