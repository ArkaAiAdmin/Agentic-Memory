"""fact subpackage — decomposed from fact_extraction.py.

Re-exports all public symbols from the submodules so both
``from fact import extract_facts`` and the original
``from fact_extraction import extract_facts`` keep working.
"""

from fact.fact_clean import (
    _META_LABELS,
    _clean,
    _clean_description,
    _clean_description_inline,
    _first_sentence,
    _find_verb,
    _is_meta_header,
    _is_valid,
    _preprocess,
    _should_skip_category,
    _strip_articles,
    extract_event_time,
)
from fact.fact_extract import (
    _dedup_facts,
    _layer1_section_header_bold,
    _layer2_dash_bullets,
    _layer3_classification,
    _layer4_code_references,
    _layer5a_copula,
    _layer5b_colon_definitions,
    _layer5c_plain_dash_bullets,
    _layer5d_subject_verb_object,
    _should_use_llm_for_memory,
    _upsert_fact,
    extract_facts,
    index_facts_for_memory,
    lock_fact,
)
from fact.fact_schema import ensure_facts_schema
from fact.fact_search import (
    _build_fts_query,
    _facts_search_fts,
    _facts_search_like,
    facts_list,
    facts_list_db,
    facts_search,
    facts_search_db,
    facts_stats,
    facts_stats_db,
)

__all__ = [
    "extract_facts",
    "index_facts_for_memory",
    "ensure_facts_schema",
    "facts_search",
    "facts_search_db",
    "facts_list",
    "facts_list_db",
    "facts_stats",
    "facts_stats_db",
    "lock_fact",
    "extract_event_time",
    "_META_LABELS",
    "_layer1_section_header_bold",
    "_layer2_dash_bullets",
    "_layer3_classification",
    "_layer4_code_references",
    "_layer5a_copula",
    "_layer5b_colon_definitions",
    "_layer5c_plain_dash_bullets",
    "_layer5d_subject_verb_object",
    "_dedup_facts",
    "_upsert_fact",
    "_is_meta_header",
    "_clean",
    "_clean_description",
    "_clean_description_inline",
    "_first_sentence",
    "_is_valid",
    "_strip_articles",
    "_preprocess",
    "_should_skip_category",
    "_find_verb",
    "_should_use_llm_for_memory",
]
