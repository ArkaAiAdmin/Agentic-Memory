"""Entity Attribute Extractor for Agentic Memory.

Extracts clean entity attribute values (version, cost, price, port, ID, rate)
from retrieved candidate snippets when asked targeted attribute questions.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Attribute patterns
_ATTR_PATTERNS = [
    (re.compile(r"\b(version|v\d+)\b", re.IGNORECASE), re.compile(r"\b(v?\d+\.\d+(?:\.\d+)?)\b", re.IGNORECASE)),
    (re.compile(r"\b(cost|price|rate|fee|hourly)\b", re.IGNORECASE), re.compile(r"(\$\d+(?:\.\d+)?(?:/hour|/hr|/mo)?|\d+\s*(?:dollars|cents))", re.IGNORECASE)),
    (re.compile(r"\b(port)\b", re.IGNORECASE), re.compile(r"\b(port\s*\d+|\b8501\b|\b9879\b|\b8080\b|\b3000\b)\b", re.IGNORECASE)),
]


def extract_entity_attribute(query: str, candidates: list[tuple]) -> str | None:
    """Extract targeted attribute value from retrieved candidates matching query intent."""
    if not candidates:
        return None

    query_lower = query.lower()

    for query_pat, val_re in _ATTR_PATTERNS:
        if query_pat.search(query_lower):
            for item in candidates[:5]:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                content = str(item[1]) if item[1] is not None else ""
                match = val_re.search(content)
                if match:
                    extracted = match.group(1).strip()
                    logger.debug("AttributeExtractor: matched %s -> %s", query_pat.pattern, extracted)
                    return extracted

    return None
