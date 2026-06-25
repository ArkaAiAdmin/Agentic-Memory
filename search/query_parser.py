"""Search query parsing, expansion, and zero-result suggestion helpers.

Extracted from search_pipeline.py (2026-06-20) as part of the
god-module decomposition. Contains the pure query-shaping primitives
that the main search_memories orchestrator calls:

- _parse_search_query: full query -> (normalized, fts_query, bare, graph_rag_terms)
- _escape_fts_query, _escape_phrase: FTS5 escaping primitives
- _expand_query: synonym/abbreviation expansion (QW2)
- _did_you_mean: typo/synonym correction candidates
- _detect_query_type, _weights_for_query_type: query classification (QW3)
- _graph_rag_expand: KG-based query expansion
- _top_recent_tags, _top_recent_notes, _top_recent_source_files:
  zero-result suggestion channels
- _build_zero_result_suggestions: composes the four channels

The _QUERY_EXPANSIONS dict and query-type regexes live here.
Behavior is identical to the inline versions in search_pipeline.
Re-exported from search_pipeline for backward compat.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from infrastructure import _normalize_unicode
from memory_common import connection_pool, safe_close_db

logger = logging.getLogger(__name__)

# Query type classification regexes (QW3)
_QUERY_TYPE_TEMPORAL_RE = re.compile(
    "\\b(when|what year|what date|how long ago|last (week|month|year)|recent|latest|yesterday|today|tomorrow|ago|\\d{4}[-/]\\d{2}|in \\d{4})\\b",
    re.IGNORECASE,
)
_QUERY_TYPE_MULTIHOP_RE = re.compile(
    "\\b(compare|difference|between|relationship|both|and also|plus|along with|combined)\\b",
    re.IGNORECASE,
)
_QUERY_TYPE_CODE_RE = re.compile(
    "\\b(function|class|method|import|return|def |var |let |const |\\.py|\\.js|\\.ts|\\.go|\\.rs|error|exception|stacktrace|syntax|compile|build|test|spec|fixture)\\b",
    re.IGNORECASE,
)
_QUERY_TYPE_FACTUAL_RE = re.compile(
    "\\b(what is|what are|who is|where is|define|definition of|meaning of|how many|capital of)\\b",
    re.IGNORECASE,
)

_QUERY_TYPE_WEIGHTS: dict[str, dict[str, float]] = {
    "temporal": {
        "bm25": 0.3,
        "fitness": 0.2,
        "importance": 0.1,
        "pinned": 0.05,
        "recency": 0.3,
        "tag_match": 0.05,
    },
    "multihop": {
        "bm25": 0.3,
        "fitness": 0.35,
        "importance": 0.2,
        "pinned": 0.1,
        "recency": 0.0,
        "tag_match": 0.05,
    },
    "code": {
        "bm25": 0.4,
        "fitness": 0.15,
        "importance": 0.15,
        "pinned": 0.15,
        "recency": 0.05,
        "tag_match": 0.1,
    },
    "factual": {
        "bm25": 0.55,
        "fitness": 0.1,
        "importance": 0.15,
        "pinned": 0.15,
        "recency": 0.0,
        "tag_match": 0.05,
    },
    "general": {
        "bm25": 0.4,
        "fitness": 0.2,
        "importance": 0.15,
        "pinned": 0.1,
        "recency": 0.1,
        "tag_match": 0.05,
    },
}


def _get_query_type_weights() -> dict:
    try:
        from _lazy_imports import get_config

        raw = get_config().query_type_weights
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        pass
    return _QUERY_TYPE_WEIGHTS


_QUERY_EXPANSIONS: dict[str, list[str]] = {
    "ml": ["machine learning", "machine-learning"],
    "ai": ["artificial intelligence"],
    "nlp": ["natural language processing", "natural-language"],
    "llm": ["large language model", "language model"],
    "llms": ["large language models", "language models"],
    "db": ["database"],
    "dbs": ["databases"],
    "sql": ["structured query language"],
    "auth": ["authentication", "authorization"],
    "authn": ["authentication"],
    "authz": ["authorization"],
    "api": ["application programming interface", "endpoint"],
    "apis": ["endpoints", "application programming interfaces"],
    "ui": ["user interface", "frontend"],
    "ux": ["user experience"],
    "k8s": ["kubernetes"],
    "tf": ["terraform"],
    "ci": ["continuous integration"],
    "cd": ["continuous deployment", "continuous delivery"],
    "qa": ["quality assurance", "testing"],
    "perf": ["performance"],
    "config": ["configuration", "settings"],
    "configs": ["configurations"],
    "env": ["environment", "env var", "envvars"],
    "deps": ["dependencies"],
    "pkg": ["package"],
    "pkgs": ["packages"],
    "lib": ["library"],
    "libs": ["libraries"],
    "repo": ["repository"],
    "repos": ["repositories"],
    "pr": ["pull request"],
    "prs": ["pull requests"],
    "cli": ["command line", "command-line"],
    "async": ["asynchronous", "non-blocking"],
    "sync": ["synchronous", "blocking"],
    "os": ["operating system"],
    "fs": ["filesystem", "file system"],
    "i18n": ["internationalization"],
    "l10n": ["localization"],
    "a11y": ["accessibility"],
    "ip": ["internet protocol", "ip address"],
    "tcp": ["transmission control protocol"],
    "udp": ["user datagram protocol"],
    "http": ["hypertext transfer protocol"],
    "https": ["secure http", "tls"],
    "tls": ["transport layer security", "ssl"],
    "ssl": ["secure sockets layer"],
    "json": ["javascript object notation"],
    "yaml": ["yaml ain't markup language"],
    "yml": ["yaml", "yaml ain't markup language"],
    "html": ["hypertext markup language"],
    "css": ["cascading style sheets"],
    "js": ["javascript", "ecmascript"],
    "ts": ["typescript"],
    "py": ["python"],
    "rb": ["ruby"],
    "go": ["golang"],
    "rs": ["rust"],
    "crud": ["create read update delete"],
    "rest": ["representational state transfer", "restful"],
    "grpc": ["remote procedure call"],
    "ws": ["websocket", "web socket"],
    "orm": ["object relational mapper", "object-relational mapping"],
    "mvc": ["model view controller"],
    "mvvm": ["model view viewmodel"],
    "ssr": ["server side rendering", "server-side rendering"],
    "spa": ["single page application", "single-page application"],
    "pwa": ["progressive web app"],
    "csp": ["content security policy"],
    "cors": ["cross origin resource sharing", "cross-origin"],
    "csrf": ["cross site request forgery", "cross-site"],
    "xss": ["cross site scripting", "cross-site"],
    "owasp": ["open web application security project"],
    "vuln": ["vulnerability"],
    "vulns": ["vulnerabilities"],
    "cve": ["common vulnerabilities and exposures"],
    "pci": ["payment card industry"],
    "gdpr": ["general data protection regulation"],
    "hipaa": ["health insurance portability accountability act"],
    "soc2": ["soc 2", "service organization control 2"],
    "sla": ["service level agreement"],
    "rto": ["recovery time objective"],
    "rpo": ["recovery point objective"],
    "dr": ["disaster recovery"],
    "ha": ["high availability"],
    "lb": ["load balancer", "load balancing"],
    "vm": ["virtual machine"],
    "vms": ["virtual machines"],
    "vmware": ["vsphere"],
    "e2e": ["end to end", "end-to-end"],
    "i3e": ["integration"],
    "rlhf": ["reinforcement learning from human feedback"],
    "rag": ["retrieval augmented generation", "retrieval-augmented"],
    "gpu": ["graphics processing unit"],
    "tpu": ["tensor processing unit"],
    "nn": ["neural network", "neural net"],
    "cnn": ["convolutional neural network"],
    "rnn": ["recurrent neural network"],
    "transformer": ["transformer architecture", "attention model"],
    "gpt": ["generative pre trained transformer"],
    "bert": ["bidirectional encoder representations"],
}

_QUERY_EXPANSION_REVERSE: dict[str, str] = {}
for _canon in _QUERY_EXPANSIONS:
    _QUERY_EXPANSION_REVERSE[_canon] = _canon
for _canon, _alts in _QUERY_EXPANSIONS.items():
    for _a in _alts:
        _al = _a.lower()
        if _al not in _QUERY_EXPANSION_REVERSE:
            _QUERY_EXPANSION_REVERSE[_al] = _canon


def _query_expansions() -> dict:
    return _QUERY_EXPANSIONS


def _query_expansion_reverse() -> dict:
    return _QUERY_EXPANSION_REVERSE


def _expand_query(query: str) -> str:
    """QW2: Expand query terms using the synonym/abbreviation dictionary.

    Returns a string where each detected term has been replaced with an OR
    group of all its known forms. E.g. "DB speed" becomes
    '"database" OR "db" "speed"' (when joined with the rest of the query).

    Quoted phrases are preserved as-is (don't expand inside phrases).
    The original tokens are always kept so the user's literal query still matches.
    """
    if not query or not _query_expansions():
        return query
    phrases = re.findall('"([^"]*)"', query)
    bare = re.sub('"[^"]*"', " ", query)
    bare_tokens = re.findall("[\\w@\\#\\.\\+\\-]+", bare, flags=re.UNICODE)
    if not bare_tokens and (not phrases):
        return query
    expanded_tokens = []
    seen_aliases: set = set()
    for tok in bare_tokens:
        low = tok.lower()
        canon = _query_expansion_reverse().get(low)
        if canon and canon not in seen_aliases:
            seen_aliases.add(canon)
            forms = [canon] + _query_expansions().get(canon, [])
            unique: list[str] = []
            for f in forms:
                if f.lower() not in [u.lower() for u in unique]:
                    unique.append(f)
            quoted = " OR ".join((f'"{f}"' for f in unique))
            expanded_tokens.append(f"({quoted})")
        else:
            expanded_tokens.append(f'"{tok}"')
    out_parts = [f'"{p}"' for p in phrases if p.strip()]
    out_parts.extend(expanded_tokens)
    return " AND ".join(out_parts)


def _did_you_mean(query: str, synonym_map: dict) -> list:
    """Return up to 3 expanded query strings based on the synonym map.

    For each word in the query that appears as a key in `synonym_map`,
    produce a variant where that word is replaced by one of its synonyms.
    Up to 3 variants total are returned (one per matching word, then
    truncated). Words without a known synonym are skipped.
    """
    if not query or not synonym_map:
        return []
    words = query.lower().split()
    expansions = []
    for i, w in enumerate(words):
        clean = w.strip(".,;:!?()[]{}\"'`")
        syns = synonym_map.get(clean)
        if not syns:
            continue
        for syn in syns[:3]:
            new_query = " ".join(words[:i] + [syn] + words[i + 1 :])
            expansions.append(new_query)
    return expansions[:3]


def _top_recent_tags(db_path, limit: int = 5) -> list:
    """Return up to `limit` most-recently-observed distinct tag sets.

    Each row in the memories table stores tags as a JSON array string.
    We group by the literal string and pick the most recently observed
    per group. Returns [] on any DB error.
    """
    if not db_path:
        return []
    try:
        conn = connection_pool.get(str(db_path))
        try:
            rows = conn.execute(
                "\n                SELECT tags, MAX(observed_at) as latest\n                FROM memories\n                WHERE tags != '[]' AND tags IS NOT NULL\n                GROUP BY tags\n                ORDER BY latest DESC\n                LIMIT ?\n            ",
                (limit,),
            ).fetchall()
            return [{"tag": r[0], "latest_observed_at": r[1]} for r in rows]
        finally:
            safe_close_db(conn)
    except Exception:
        logger.warning("Failed to query recent tags for suggestions")
        return []


def _top_recent_notes(db_path, limit: int = 5) -> list:
    """Return up to `limit` most-recently-observed notes (id + preview)."""
    if not db_path:
        return []
    try:
        conn = connection_pool.get(str(db_path))
        try:
            rows = conn.execute(
                "\n                SELECT id, substr(content, 1, 80) as preview, observed_at\n                FROM memories\n                ORDER BY observed_at DESC\n                LIMIT ?\n            ",
                (limit,),
            ).fetchall()
            return [{"id": r[0], "preview": r[1], "observed_at": r[2]} for r in rows]
        finally:
            safe_close_db(conn)
    except Exception:
        logger.warning("Failed to query recent notes for suggestions")
        return []


def _top_recent_source_files(db_path, limit: int = 5) -> list:
    """Return up to `limit` source files grouped by recency, with counts."""
    if not db_path:
        return []
    try:
        conn = connection_pool.get(str(db_path))
        try:
            rows = conn.execute(
                "\n                SELECT source_file, COUNT(*) as cnt, MAX(observed_at) as latest\n                FROM memories\n                GROUP BY source_file\n                ORDER BY latest DESC\n                LIMIT ?\n            ",
                (limit,),
            ).fetchall()
            return [
                {"source_file": r[0], "count": r[1], "latest_observed_at": r[2]}
                for r in rows
            ]
        finally:
            safe_close_db(conn)
    except Exception:
        logger.warning("Failed to query recent source files for suggestions")
        return []


def _build_zero_result_suggestions(db_path, query: str) -> dict:
    """Assemble the suggestions payload for a 0-result search.

    Returns a dict with four keys (did_you_mean / by_tag / by_recency /
    by_source_file), each a list. Any failing channel degrades to [].
    """
    return {
        "did_you_mean": _did_you_mean(query, _query_expansions()),
        "by_tag": _top_recent_tags(db_path, limit=5),
        "by_recency": _top_recent_notes(db_path, limit=5),
        "by_source_file": _top_recent_source_files(db_path, limit=5),
    }


def _detect_query_type(query: str) -> str:
    """QW3: classify a query into one of {temporal, multihop, code, factual, general}.

    Detection is conservative — the first matching pattern wins, and
    "general" is the fallback. False positives are worse than false
    negatives here, so the patterns are tight.
    """
    if not query:
        return "general"
    if _QUERY_TYPE_CODE_RE.search(query):
        return "code"
    if _QUERY_TYPE_TEMPORAL_RE.search(query):
        return "temporal"
    if _QUERY_TYPE_MULTIHOP_RE.search(query):
        return "multihop"
    if _QUERY_TYPE_FACTUAL_RE.search(query):
        return "factual"
    return "general"


def _weights_for_query_type(query_type: str) -> dict:
    """QW3: return the merged weight dict for a given query type.

    The result always sums to 1.0 and always contains all six channel keys.
    """
    weights = _get_query_type_weights()
    return dict(weights.get(query_type, weights["general"]))


def _escape_phrase(s: str) -> str:
    """Escape a string for safe inclusion in an FTS5 double-quoted phrase."""
    return '"' + s.replace('"', '""') + '"'


def _escape_fts_query(query: str) -> str:
    """Escape FTS5 special characters in a user-provided query string.

    FTS5 special operators: ``*`` (prefix wildcard), ``^`` (prefix boost),
    standalone ``NEAR`` and ``NOT``. These are quoted so they become
    literal search terms.  Parentheses and ``+`` are FTS5 grouping/prefix
    syntax — they must NOT be escaped.
    """
    q = query
    q = re.sub(r"\bNEAR\b", '"NEAR"', q, flags=re.IGNORECASE)
    q = re.sub(r"\bNOT\b", '"NOT"', q, flags=re.IGNORECASE)
    q = q.replace('"', '""')
    q = q.replace("*", "\\*")
    q = q.replace("^", "\\^")
    return q


def _graph_rag_expand(query: str, db_path: Path) -> list[str]:
    """Graph-RAG: extract entities from query, traverse KG, return related entity names.

    When the knowledge graph is enabled, this function:
    1. Extracts entities from the search query using pattern-based NER
    2. Searches the KG for matching entities and their neighbors (1-2 hops)
    3. Returns display names of related entities as query expansion terms

    These terms are added to the FTS query to boost recall for notes
    that mention KG-related entities but don't contain the original query tokens.

    The Graph-RAG config flags are read from search_pipeline's lazy
    __getattr__ to keep this module decoupled from the orchestrator's
    config-resolution machinery.
    """
    import search_pipeline

    if not getattr(search_pipeline, "_GRAPH_RAG_ENABLED", False):
        return []
    try:
        from knowledge_graph import (
            KG_ENABLED,
            graph_search as _graph_search,
            extract_entities,
        )

        if not KG_ENABLED:
            return []
    except ImportError:
        return []
    query_entities = extract_entities(query)
    if not query_entities:
        return []
    try:
        from _lazy_imports import open_db

        with open_db(db_path) as conn:
            all_related = []
            combined_query = " ".join(name for name, _ in query_entities[:5])
            results = _graph_search(
                conn,
                combined_query,
                limit=10,
                max_hops=getattr(search_pipeline, "_GRAPH_RAG_MAX_HOPS", 3),
            )
            for entity in results.get("entities", []):
                display = entity.get("name", "")
                if display and display.lower() not in {
                    n.lower() for n, _ in query_entities
                }:
                    all_related.append(display)
            seen = set()
            expanded = []
            for name in all_related:
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    expanded.append(name)
                if len(expanded) >= getattr(
                    search_pipeline, "_GRAPH_RAG_MAX_EXPANSIONS", 5
                ):
                    break
            return expanded
    except Exception:
        logger.warning("Failed to expand query via graph RAG")
        return []


def _parse_search_query(query: str, db_path: Path) -> tuple[str, str, str, list[str]]:
    """Parse a search query into components.

    Returns (normalized_query, fts_query, bare_query_text, graph_rag_terms).
    """
    normalized_query = _normalize_unicode(query)
    phrases = re.findall('"([^"]*)"', normalized_query)
    bare = re.sub('"[^"]*"', " ", normalized_query)
    bare_words = re.findall("[\\w@\\#\\.\\+\\-]+", bare, flags=re.UNICODE)
    terms = [_escape_phrase(p) for p in phrases if p.strip()]
    terms += [_escape_phrase(_escape_fts_query(w)) for w in bare_words if w]
    expanded = _expand_query(normalized_query)
    fts_query = (
        expanded if expanded and expanded != normalized_query else " OR ".join(terms)
    )
    graph_rag_terms = _graph_rag_expand(normalized_query, db_path)
    if graph_rag_terms:
        graph_rag_fts = " OR ".join((f'"{t}"' for t in graph_rag_terms))
        fts_query = f"({fts_query}) OR ({graph_rag_fts})"
    return normalized_query, fts_query, " ".join(bare_words), graph_rag_terms
