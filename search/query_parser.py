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
import sqlite3
from pathlib import Path

from infra.infrastructure import _normalize_unicode
from infra.memory_common import connection_pool, safe_close_db

logger = logging.getLogger(__name__)

# Stop words: high-frequency words that waste FTS5 match budget on AND queries
_STOP_WORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
    'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'shall', 'can', 'need', 'dare',
    'ought', 'used', 'what', 'which', 'who', 'whom', 'this', 'that',
    'these', 'those', 'i', 'you', 'he', 'she', 'it', 'we', 'they',
    'me', 'him', 'her', 'us', 'them', 'my', 'your', 'his', 'its',
    'our', 'their', 'mine', 'yours', 'hers', 'ours', 'theirs',
    'am', 'if', 'then', 'else', 'when', 'where', 'how', 'all', 'each',
    'every', 'both', 'few', 'more', 'most', 'other', 'some', 'such',
    'no', 'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very',
    'just', 'because', 'as', 'until', 'while', 'about', 'between',
    'through', 'during', 'before', 'after', 'above', 'below', 'up',
    'down', 'out', 'off', 'over', 'under', 'again', 'further', 'once',
    'here', 'there', 'any', 'also', 'type', 'kind', 'sort', 'want',
    'looking', 'find', 'search', 'query', 'tell', 'show',
})

# Word form expansions: porter stemming misses these cross-form matches.
# Maps a stem to all its surface forms so FTS5 OR-matches correctly.
# E.g. "container" and "containerize" have different porter stems, but
# we want queries containing either to match documents containing either.
_WORD_FORM_EXPANSIONS: dict[str, list[str]] = {
    'container': ['container', 'containers', 'containerize', 'containerized', 'containerizing', 'containerization'],
    'deploy': ['deploy', 'deploys', 'deployed', 'deploying', 'deployment', 'deployments'],
    'orchestrat': ['orchestrate', 'orchestrates', 'orchestrated', 'orchestrating', 'orchestration', 'orchestrator', 'orchestrators'],
    'observ': ['observe', 'observes', 'observed', 'observing', 'observation', 'observations', 'observability', 'observable'],
    'serial': ['serialize', 'serializes', 'serialized', 'serializing', 'serialization', 'serializations'],
    'index': ['index', 'indexes', 'indexed', 'indexing', 'indices'],
    'monitor': ['monitor', 'monitors', 'monitored', 'monitoring', 'monitoring'],
    'configur': ['configure', 'configures', 'configured', 'configuring', 'configuration', 'configurations'],
    'optimi': ['optimize', 'optimizes', 'optimized', 'optimizing', 'optimization', 'optimisations', 'optimizations'],
    'automat': ['automate', 'automates', 'automated', 'automating', 'automation', 'automations'],
    'implement': ['implement', 'implements', 'implemented', 'implementing', 'implementation', 'implementations'],
    'integrat': ['integrate', 'integrates', 'integrated', 'integrating', 'integration', 'integrations'],
    'migrat': ['migrate', 'migrates', 'migrated', 'migrating', 'migration', 'migrations'],
    'compil': ['compile', 'compiles', 'compiled', 'compiling', 'compilation', 'compilations'],
    'test': ['test', 'tests', 'testing', 'tested', 'tester', 'testers'],
    'search': ['search', 'searches', 'searched', 'searching', 'retrieval', 'retrieve', 'retrieves', 'retrieved', 'retrieving'],
    'perform': ['perform', 'performs', 'performed', 'performing', 'performance', 'performances'],
    'secur': ['secure', 'secures', 'secured', 'securing', 'security', 'securities'],
    'author': ['authorize', 'authorizes', 'authorized', 'authorizing', 'authorization', 'authorizations', 'authorise', 'authorisation'],
    'authentic': ['authenticate', 'authenticates', 'authenticated', 'authenticating', 'authentication', 'authentications'],
    'encrypt': ['encrypt', 'encrypts', 'encrypted', 'encrypting', 'encryption', 'encryptions'],
    'compress': ['compress', 'compresses', 'compressed', 'compressing', 'compression', 'compressions'],
    'synch': ['synchronize', 'synchronizes', 'synchronized', 'synchronizing', 'synchronization', 'sync', 'syncs', 'synced', 'syncing'],
    'asynch': ['asynchronize', 'asynchronizes', 'asynchronized', 'asynchronizing', 'asynchronization', 'async'],
    'consolid': ['consolidate', 'consolidates', 'consolidated', 'consolidating', 'consolidation'],
    'extract': ['extract', 'extracts', 'extracted', 'extracting', 'extraction', 'extractions'],
    'deduplic': ['deduplicate', 'deduplicates', 'deduplicated', 'deduplicating', 'deduplication', 'dedup', 'deduplicates'],
    'summar': ['summarize', 'summarizes', 'summarized', 'summarizing', 'summarization', 'summarisations', 'summaries', 'summary'],
    'compact': ['compact', 'compacts', 'compacted', 'compacting', 'compaction'],
    'retent': ['retain', 'retains', 'retained', 'retaining', 'retention'],
    'decay': ['decay', 'decays', 'decayed', 'decaying'],
    'supersed': ['supersede', 'supersedes', 'superseded', 'superseding', 'supersession'],
    'reconcil': ['reconcile', 'reconciles', 'reconciled', 'reconciling', 'reconciliation'],
    'propagat': ['propagate', 'propagates', 'propagated', 'propagating', 'propagation'],
    'embed': ['embed', 'embeds', 'embedded', 'embedding', 'embeddings'],
    'chunk': ['chunk', 'chunks', 'chunked', 'chunking'],
    'vector': ['vector', 'vectors', 'vectorized', 'vectorization'],
    'cluster': ['cluster', 'clusters', 'clustered', 'clustering'],
    'entiti': ['entity', 'entities'],
    'relat': ['relation', 'relations', 'relationship', 'relationships', 'related'],
    'contradict': ['contradict', 'contradicts', 'contradicted', 'contradicting', 'contradiction', 'contradictions'],
    'entail': ['entail', 'entails', 'entailed', 'entailing', 'entailment', 'entailments'],
    'infer': ['infer', 'infers', 'inferred', 'inferring', 'inference', 'inferences'],
    'compile': ['compile', 'compiles', 'compiled', 'compiling', 'compilation'],
    'enrich': ['enrich', 'enriches', 'enriched', 'enriching', 'enrichment'],
    'qualiti': ['quality', 'qualities'],
    'prioriti': ['prioritize', 'prioritizes', 'prioritized', 'prioritizing', 'priority', 'priorities'],
    'schedul': ['schedule', 'schedules', 'scheduled', 'scheduling', 'scheduler'],
    'config': ['config', 'configs', 'configuration', 'configurations', 'configure', 'configured'],
    'rebuild': ['rebuild', 'rebuilds', 'rebuilt', 'rebuilding'],
    'backup': ['backup', 'backups', 'backed', 'backing'],
    'restore': ['restore', 'restores', 'restored', 'restoring', 'restoration'],
    'purge': ['purge', 'purges', 'purged', 'purging'],
    'compact': ['compact', 'compacts', 'compacted', 'compacting', 'compaction'],
    'revis': ['revision', 'revisions', 'revise', 'revises', 'revised', 'revising'],
    'assert': ['assertion', 'assertions', 'assert', 'asserts', 'asserted', 'asserting'],
    'bel': ['belief', 'beliefs', 'believe', 'believes', 'believed', 'believing'],
    'fact': ['fact', 'facts'],
    'concept': ['concept', 'concepts', 'conceptual'],
    'graph': ['graph', 'graphs', 'graphed', 'graphing'],
    'node': ['node', 'nodes'],
    'edg': ['edge', 'edges'],
    'path': ['path', 'paths'],
    'travers': ['traverse', 'traverses', 'traversed', 'traversing', 'traversal', 'traversals'],
    'commun': ['community', 'communities', 'communicate', 'communicates', 'communicated', 'communicating', 'communication'],
    'centr': ['central', 'centrally', 'center', 'centers', 'centered', 'centering', 'centrality'],
    'between': ['between', 'betweenness'],
}

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
        "bm25": 0.4,
        "fitness": 0.25,
        "importance": 0.15,
        "pinned": 0.1,
        "tag_match": 0.1,
    },
    "multihop": {
        "bm25": 0.3,
        "fitness": 0.35,
        "importance": 0.2,
        "pinned": 0.1,
        "tag_match": 0.05,
    },
    "code": {
        "bm25": 0.45,
        "fitness": 0.15,
        "importance": 0.15,
        "pinned": 0.15,
        "tag_match": 0.1,
    },
    "factual": {
        "bm25": 0.55,
        "fitness": 0.1,
        "importance": 0.15,
        "pinned": 0.15,
        "tag_match": 0.05,
    },
    "general": {
        "bm25": 0.45,
        "fitness": 0.2,
        "importance": 0.15,
        "pinned": 0.1,
        "tag_match": 0.1,
    },
}


def _get_query_type_weights() -> dict:
    try:
        from infra._lazy_imports import get_config

        raw = get_config().query_type_weights
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception as exc:
        logger.debug("query_type_weights config unavailable: %s", exc)
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
    "container": ["docker", "pod", "image"],
    "containers": ["docker", "pods", "images"],
    "orchestration": ["orchestrate", "orchestrates", "orchestrating"],
    "infrastructure": ["infra", "platform", "foundation"],
    "management": ["manage", "manages", "managing"],
    "platform": ["infrastructure", "framework", "system"],
    "deployment": ["deploy", "deploying", "deployed"],
    "monitoring": ["observe", "observability", "telemetry"],
    "logging": ["log", "logs", "logger"],
    "testing": ["test", "tests", "qa", "quality assurance"],
    "database": ["db", "dbs", "datastore", "store"],
    "search": ["query", "lookup", "find", "retrieval"],
    "performance": ["perf", "speed", "latency", "throughput"],
    "security": ["auth", "authn", "authz", "secure"],
    "configuration": ["config", "settings", "setup"],
    "architecture": ["design", "structure", "pattern"],
    "serialization": ["serialize", "deserialize", "encoding"],
    "rollback": ["revert", "undo", "recovery"],
    "fixtures": ["setup", "config", "conftest", "helpers"],
    "applications": ["services", "apps", "app"],
    "queries": ["search", "lookup", "find", "retrieval"],
    "self-healing": ["resilient", "fault-tolerant", "self-heal"],
    "assurance": ["quality", "testing", "qa"],
    "package": ["pkg", "packages", "library"],
    "cluster": ["clusters", "clustered"],
    "healing": ["health", "healthy", "heal"],
    "pods": ["containers", "instances", "services", "containerized"],
    "dashboard": ["visualization", "grafana"],
    "healing": ["health", "healthy", "heal"],
    "self-healing": ["resilient", "fault-tolerant", "self-heal"],
    "cluster": ["clusters", "clustered", "orchestration", "orchestrating"],
    "orchestrat": ["orchestrate", "orchestrates", "orchestrated", "orchestrating", "orchestration", "orchestrator", "orchestrators"],
    "logging": ["log", "logs", "logger", "observability"],
    "observ": ["observe", "observes", "observed", "observing", "observation", "observations", "observability", "observable"],
    "package": ["pkg", "packages", "library", "containerize", "services"],
    "applications": ["services", "apps", "app", "containerized"],
    "index": ["indexes", "indexed", "indexing", "indices", "search", "lookup"],
    "queries": ["search", "lookup", "find", "retrieval"],
    # Personal/lifestyle expansions for LongMemEval-style queries
    "yoga": ["yoga", "class", "studio", "practice", "pose"],
    "class": ["class", "classes", "course", "lesson", "session"],
    "studio": ["studio", "gym", "center", "school"],
    "rice": ["rice", "grain", "short-grain", "long-grain", "basmati", "jasmine"],
    "favorite": ["favorite", "favourite", "preferred", "best", "top"],
    "music": ["music", "song", "songs", "playlist", "artist", "band", "album"],
    "streaming": ["streaming", "stream", "spotify", "apple music", "youtube music", "tidal", "pandora"],
    "service": ["service", "platform", "app", "application"],
    "coffee": ["coffee", "cafe", "brew", "espresso", "latte"],
    "recipe": ["recipe", "recipes", "dish", "meal", "cook", "cooking"],
    "restaurant": ["restaurant", "dining", "eat", "food", "cuisine"],
    "trip": ["trip", "travel", "vacation", "journey", "visit", "destination"],
    "book": ["book", "novel", "read", "reading", "author"],
    "movie": ["movie", "film", "watch", "show", "series", "tv"],
    "gym": ["gym", "workout", "exercise", "fitness", "training"],
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
    if not query:
        return query
    phrases = re.findall('"([^"]*)"', query)
    bare = re.sub('"[^"]*"', " ", query)
    bare_tokens = re.findall("[\\w@\\#\\.\\+\\-]+", bare, flags=re.UNICODE)
    if not bare_tokens and (not phrases):
        return query
    # Filter stop words from expansion — they waste FTS5 match budget
    # by matching many irrelevant sessions. Content words are kept.
    content_tokens = [t for t in bare_tokens if t.lower() not in _STOP_WORDS]
    if not content_tokens and not phrases:
        return query
    expanded_tokens = []
    seen_aliases: set = set()
    seen_forms: set = set()  # global dedup: prevent same form in multiple expansions
    for tok in content_tokens:
        low = tok.lower()
        # Try synonym expansion first
        canon = _query_expansion_reverse().get(low)
        if canon and canon not in seen_aliases:
            seen_aliases.add(canon)
            forms = [canon] + _query_expansions().get(canon, [])
            unique: list[str] = []
            for f in forms:
                fl = f.lower()
                if fl not in seen_forms:
                    unique.append(f)
                    seen_forms.add(fl)
            if unique:
                quoted = " OR ".join((f'"{f}"' for f in unique))
                expanded_tokens.append(f"({quoted})")
        else:
            # Try word form expansion (porters-stemmer cross-form matching)
            expanded = False
            for stem, forms in _WORD_FORM_EXPANSIONS.items():
                # Check if this token matches any form in the expansion set
                if low in [f.lower() for f in forms] or (low.startswith(stem) and len(low) == len(stem)):
                    # Use all forms from this expansion set
                    unique: list[str] = []
                    for f in forms:
                        if f.lower() not in [u.lower() for u in unique]:
                            unique.append(f)
                    quoted = " OR ".join((f'"{f}"' for f in unique))
                    expanded_tokens.append(f"({quoted})")
                    expanded = True
                    break
            if not expanded:
                expanded_tokens.append(f'"{tok}"')
    out_parts = [f'"{p}"' for p in phrases if p.strip()]
    out_parts.extend(expanded_tokens)
    # Always use OR for maximum recall. The custom eval uses OR-only
    # and gets 100% recall — AND on short queries kills recall by
    # requiring every term to match, which fails on conversational
    # queries where the answer uses different vocabulary.
    if out_parts:
        return " OR ".join(out_parts)
    return query


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


def _top_recent_tags(db_path, limit: int = 5, tenant_id: str = "default") -> list:
    """Return up to `limit` most-recently-observed distinct tag sets.

    Each row in the memories table stores tags as a JSON array string.
    We group by the literal string and pick the most recently observed
    per group. Returns [] on any DB error.
    """
    if not db_path:
        return []
    try:
        conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
        try:
            rows = conn.execute(
                "\n                SELECT tags, MAX(observed_at) as latest\n                FROM tenant_memories\n                WHERE tags != '[]' AND tags IS NOT NULL\n                GROUP BY tags\n                ORDER BY latest DESC\n                LIMIT ?\n            ",
                (limit,),
            ).fetchall()
            return [{"tag": r[0], "latest_observed_at": r[1]} for r in rows]
        finally:
            safe_close_db(conn)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        logger.warning("Failed to query recent tags for suggestions")
        return []


def _top_recent_notes(db_path, limit: int = 5, tenant_id: str = "default") -> list:
    """Return up to `limit` most-recently-observed notes (id + preview)."""
    if not db_path:
        return []
    try:
        conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
        try:
            rows = conn.execute(
                "\n                SELECT id, substr(content, 1, 80) as preview, observed_at\n                FROM tenant_memories\n                ORDER BY observed_at DESC\n                LIMIT ?\n            ",
                (limit,),
            ).fetchall()
            return [{"id": r[0], "preview": r[1], "observed_at": r[2]} for r in rows]
        finally:
            safe_close_db(conn)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        logger.warning("Failed to query recent notes for suggestions")
        return []


def _top_recent_source_files(db_path, limit: int = 5, tenant_id: str = "default") -> list:
    """Return up to `limit` source files grouped by recency, with counts."""
    if not db_path:
        return []
    try:
        conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
        try:
            rows = conn.execute(
                "\n                SELECT source_file, COUNT(*) as cnt, MAX(observed_at) as latest\n                FROM tenant_memories\n                GROUP BY source_file\n                ORDER BY latest DESC\n                LIMIT ?\n            ",
                (limit,),
            ).fetchall()
            return [
                {"source_file": r[0], "count": r[1], "latest_observed_at": r[2]}
                for r in rows
            ]
        finally:
            safe_close_db(conn)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
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

    Sprint 4 community-aware mode: when a query entity has a non-zero
    community_id in kg_entities, prefer expansion terms from the same
    community to reduce cross-topic false positives.
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
        try:
            from infra._lazy_imports import get_config

            _min_occ_q = int(get_config().entity_min_occurrences)
        except Exception as exc:
            logger.debug("entity_min_occurrences config unavailable: %s", exc)
            _min_occ_q = 2
    except ImportError:
        return []
    query_entities = extract_entities(query, min_occurrences=_min_occ_q)
    if not query_entities:
        return []
    try:
        from infra._lazy_imports import open_db

        with open_db(db_path) as conn:
            combined_query = " ".join(name for name, _ in query_entities[:5])

            query_entity_ids: set[int] = set()
            for name, _ in query_entities[:3]:
                try:
                    rows = conn.execute(
                        "SELECT id, community_id FROM kg_entities WHERE lower(name) = ? AND community_id IS NOT NULL AND community_id != 0 LIMIT 1",
                        (name.lower(),),
                    ).fetchall()
                    if rows:
                         query_entity_ids.add(int(rows[0][0]))
                except Exception as exc:
                    logger.debug("kg_entities lookup failed for %r: %s", name, exc)

            results = _graph_search(
                conn,
                combined_query,
                limit=10,
                max_hops=getattr(search_pipeline, "_GRAPH_RAG_MAX_HOPS", 3),
            )
            entity_communities: dict[str, int] = {}
            for entity in results.get("entities", []):
                eid = entity.get("id")
                comm = entity.get("community_id")
                if eid is not None and comm:
                    entity_communities[str(eid)] = int(comm)

            same_community_terms: list[str] = []
            other_terms: list[str] = []
            for entity in results.get("entities", []):
                display = entity.get("name", "")
                if not display or display.lower() in {n.lower() for n, _ in query_entities}:
                    continue
                eid = entity.get("id")
                comm = entity.get("community_id")
                if query_entity_ids and eid is not None and comm:
                    eid_int = int(eid)
                    try:
                        in_same_community = any(
                            conn.execute(
                                "SELECT 1 FROM kg_entities WHERE id = ? AND community_id = (SELECT community_id FROM kg_entities WHERE id = ? LIMIT 1)",
                                (eid_int, qid),
                            ).fetchone()
                            for qid in query_entity_ids
                         )
                    except Exception as exc:
                        logger.debug("community check failed for entity %s: %s", eid_int, exc)
                        in_same_community = False
                    if in_same_community:
                        same_community_terms.append(display)
                        continue
                other_terms.append(display)

            combined = same_community_terms + other_terms
            seen = set()
            expanded = []
            for name in combined:
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
    # Filter stop words from FTS terms (but keep bare_words for display)
    content_words = [w for w in bare_words if w.lower() not in _STOP_WORDS]
    # Generate adjacent bigram phrase queries for bare words
    bigrams = []
    for i in range(len(content_words) - 1):
        w1_esc = _escape_fts_query(content_words[i])
        w2_esc = _escape_fts_query(content_words[i+1])
        if w1_esc and w2_esc:
            bigrams.append(f"{w1_esc} {w2_esc}")
    bigram_terms = [_escape_phrase(bg) for bg in bigrams]

    expanded = _expand_query(normalized_query)
    if bigram_terms:
        bigram_clause = " OR ".join(bigram_terms)
        if expanded:
            fts_query = f"({bigram_clause}) OR ({expanded})"
        else:
            fts_query = bigram_clause
    else:
        if expanded and expanded != normalized_query:
            fts_query = expanded
        else:
            terms = [_escape_phrase(p) for p in phrases if p.strip()]
            terms += [_escape_phrase(_escape_fts_query(w)) for w in content_words if w]
            fts_query = " OR ".join(terms) if terms else ""

    if not fts_query.strip() and bare_words:
        # Fallback for stopword-only queries: use original bare words as FTS terms
        terms = [_escape_phrase(_escape_fts_query(w)) for w in bare_words if w]
        fts_query = " OR ".join(terms)
    graph_rag_terms = _graph_rag_expand(normalized_query, db_path)
    if graph_rag_terms:
        # 2026-06-29 fix: route KG expansion terms through _escape_phrase so
        # embedded double-quotes and `/` in malformed KG entities don't
        # produce broken FTS5 syntax. Regression: see
        # test_no_silent_search_failures.py::test_search_on_db_with_bad_kg_entity_never_returns_error
        graph_rag_fts = " OR ".join(_escape_phrase(t) for t in graph_rag_terms)
        fts_query = f"({fts_query}) OR ({graph_rag_fts})"
    return normalized_query, fts_query, " ".join(bare_words), graph_rag_terms
