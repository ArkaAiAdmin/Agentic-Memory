"""Optional spaCy-based NER augmentation.

Gated behind MEMORY_NER_SPACY (default off). When enabled, augments the
regex-based entity list from kg_extract.py with spaCy-detected PERSON,
ORG, GPE, PRODUCT, and FAC entities.

Requires: pip install agentic-memory[ner] && python -m spacy download en_core_web_trf
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SPACY_TYPE_MAP = {
    "PERSON": "person",
    "ORG": "organization",
    "GPE": "place",
    "PRODUCT": "product",
    "FAC": "place",
}

# Same garbage filter as kg_extract._GARBAGE_RE — ISO dates, UUIDs,
# timestamps, pure numbers, etc.  Must stay in sync.
_GARBAGE_RE = re.compile(
    r"^(?:"
    r"\d{4}[-/]\d{2}[-/]\d{2}"  # ISO dates
    r"|\d+:\d+:\d+"  # timestamps
    r"|\d+:\d+"  # short time
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # UUID
    r"|\d+$"  # pure numbers
    r"|[a-z]{2,5}-\d+$"  # short-prefix-number (ms-123, px-456)
    r"|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"  # ISO datetime
    r"|\w+:\d{2,4}$"  # word:number (tx:1234)
    r")$"
)


def augment_entities(text: str, existing: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Run spaCy NER on *text*, return additional (entity_text, entity_type) pairs.

    Entries that already exist in *existing* (case-insensitive match on
    entity text) are deduplicated so regex-extracted entities take
    precedence.  Garbage patterns (ISO dates, UUIDs, pure numbers) are
    filtered to match the regex pipeline's garbage filter.
    """
    try:
        import spacy
        model: Any = spacy.load("en_core_web_trf")
    except Exception as e:
        logger.warning("spaCy NER unavailable: %s", e)
        return []
    doc = model(text[:100_000])
    existing_texts = {e[0].lower() for e in existing}
    new: list[tuple[str, str]] = []
    for ent in doc.ents:
        label = ent.label_
        if not isinstance(label, str) or not label:
            continue
        mapped = _SPACY_TYPE_MAP.get(label)
        if mapped is None:
            mapped = label.lower()
        name_lower = ent.text.lower().strip()
        if not name_lower or len(name_lower) < 2:
            continue
        if _GARBAGE_RE.match(name_lower):
            continue
        if name_lower not in existing_texts:
            new.append((ent.text, mapped))
            existing_texts.add(name_lower)
    return new
