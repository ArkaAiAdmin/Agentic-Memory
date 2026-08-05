"""YAML frontmatter parsing for memory notes.

Extracted from memory_common.py during the 6-module refactor.

Provides:
  * ``parse_frontmatter(content)``: returns (metadata, body).
  * ``_coerce(val)``: best-effort string-to-type coercion (private).
"""
from __future__ import annotations

import json
import re

__all__ = ['parse_frontmatter']


def _coerce(val):
    """Coerce a string value to its most likely type."""
    if val == '':
        return ''
    if val.startswith('[') and val.endswith(']'):
        try:
            return [str(it) for it in json.loads(val)]
        except (json.JSONDecodeError, ValueError):
            inner = val[1:-1]
            items = [it.strip().strip('"').strip("'") for it in inner.split(',') if it.strip()]
            return items
    if val.startswith('{') and val.endswith('}'):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            inner = val[1:-1]
            result = {}
            for pair in inner.split(','):
                pair = pair.strip()
                if ':' in pair:
                    k, _, v = pair.partition(':')
                    result[k.strip().strip('"').strip("'")] = v.strip().strip('"').strip("'")
            return result
    low = val.lower()
    if low in ('true', '1', 'yes', 'on'):
        return True
    if low in ('false', '0', 'no', 'off'):
        return False
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
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
    pending_dict = None
    for line in yaml_text.splitlines():
        raw = line
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if (
            pending_list is not None
            and raw.startswith(' ')
            and ':' in stripped.split('#', 1)[0]
            and not stripped.startswith('- ')
        ):
            k, _, v = stripped.partition(':')
            k = k.strip()
            v = _coerce(v.strip().strip('"').strip("'"))
            if pending_list and isinstance(pending_list[-1], dict):
                pending_list[-1][k] = v
            continue
        if (
            pending_key is not None
            and (raw.startswith('  ') or raw.startswith('\t') or raw.startswith('- '))
            and stripped.startswith('- ')
        ):
            item_text = stripped[2:].strip()
            if item_text:
                if pending_list is None:
                    pending_list = []
                if ':' in item_text:
                    item_dict = {}
                    k, _, v = item_text.partition(':')
                    item_dict[k.strip()] = _coerce(v.strip().strip('"').strip("'"))
                    pending_list.append(item_dict)
                else:
                    pending_list.append(_coerce(item_text))
            continue
        if (
            pending_key is not None
            and raw.startswith(' ')
            and ':' in stripped.split('#', 1)[0]
        ):
            k, _, v = stripped.partition(':')
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if pending_dict is None:
                pending_dict = {}
            pending_dict[k] = v
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
            elif pending_dict is not None:
                metadata[pending_key] = pending_dict
            else:
                metadata[pending_key] = _coerce(' '.join(pending_val_parts).strip())
            pending_list = None
            pending_dict = None
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
        elif pending_dict is not None:
            metadata[pending_key] = pending_dict
        else:
            metadata[pending_key] = _coerce(' '.join(pending_val_parts).strip())
    return (metadata, body)
