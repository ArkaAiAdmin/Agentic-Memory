"""Optional spaCy-based NER augmentation.

Gated behind MEMORY_NER_SPACY (default off). When enabled, augments the
regex-based entity list from kg_extract.py with spaCy-detected PERSON,
ORG, GPE, PRODUCT, and FAC entities.

Requires: pip install agentic-memory[ner] && python -m spacy download en_core_web_sm
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SPACY_TYPE_MAP = {
    "PERSON": "person",
    "ORG": "organization",
    "GPE": "place",
    "PRODUCT": "product",
    "FAC": "place",
}


def augment_entities(text: str, existing: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Run spaCy NER on *text*, return additional (entity_text, entity_type) pairs.

    Entries that already exist in *existing* (case-insensitive match on
    entity text) are deduplicated so regex-extracted entities take
    precedence.
    """
    try:
        import spacy
        model: Any = spacy.load("en_core_web_sm")
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
        if ent.text.lower() not in existing_texts:
            new.append((ent.text, mapped))
            existing_texts.add(ent.text.lower())
    return new
