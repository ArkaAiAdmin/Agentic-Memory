from __future__ import annotations

import logging

import hashlib
import re
import threading
from collections import deque
from typing import Optional

from infra.memory_common import deprecated

logger = logging.getLogger(__name__)

# Module-level extraction cache (P2c.3). Keyed by sha256(content)[:16],
# value is the list of (name, type) tuples produced by extract_entities
# (or the LLM-extended set). Bounded LRU to keep the dict from growing
# without limit in long-lived daemons. The lock guards the deque+dict
# pair during eviction; reads from the dict are unlocked (a stale read
# just means a brief miss after eviction, which is fine).
_EXTRACTION_CACHE_MAX = 1000
_EXTRACTION_CACHE: dict[str, list[tuple[str, str]]] = {}
_EXTRACTION_CACHE_LRU: deque[str] = deque(maxlen=_EXTRACTION_CACHE_MAX)
_EXTRACTION_CACHE_LOCK = threading.Lock()

# Stop-list of common markdown/code words that should never become KG
# entities (P2b.1). The pattern-based NER used to over-extract these
# from code blocks, frontmatter keys, and inline code. Matched
# case-insensitively against the entity name.
_MARKDOWN_STOPWORDS = frozenset(
    {
        "path",
        "os",
        "import",
        "def",
        "class",
        "auto",
        "save",
        "key",
        "value",
        "type",
        "schema",
        "model",
        "config",
        "version",
        "data",
        "note",
        "memory",
        "agentic",
        "created",
        "updated",
        "tags",
        "pinned",
        "related",
        "file",
        "line",
        "return",
        "args",
        "kwargs",
        "self",
        "none",
        "true",
        "false",
        "string",
        "integer",
        "boolean",
        "object",
        "list",
        "dict",
        "tuple",
        "user",
        "function",
        "module",
        "package",
        "library",
        "framework",
        "test",
        "tests",
        "example",
        "doc",
        "docs",
        "readme",
        "license",
    }
)

# File-path / file-extension heuristics (P2b.4). Any entity containing
# "/" or ending in one of these suffixes is treated as a file path and
# excluded from the entity set.
_FILE_PATH_SUFFIXES = (
    ".py",
    ".md",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".sh",
    ".log",
)


# ---------------------------------------------------------------------------
# Content cleaning + cache helpers (P2b.2 / P2b.3 / P2b.4 / P2c.3)
# ---------------------------------------------------------------------------


def _strip_frontmatter(content: str) -> str:
    """Strip YAML frontmatter at the start of a markdown note.

    Frontmatter is the ``---`` … ``---`` block at the very start of a
    note.  It contains metadata keys (``tags:``, ``category:``) that
    would otherwise be picked up by the capitalized-phrase NER as
    concept entities.
    """
    if not content or not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    nl = content.find("\n", end + 4)
    if nl == -1:
        return ""
    return content[nl + 1 :]


def _strip_code_blocks(content: str) -> str:
    """Strip fenced code blocks (```...```) from content.

    Code blocks contain identifiers, paths, and YAML/JSON keys that
    the NER would otherwise misinterpret as entities.  Inline code
    (single backticks) is left alone — the user-visible text around
    it is more valuable than the code spans themselves.
    """
    if not content:
        return content
    return re.sub(r"```.*?```", "", content, flags=re.DOTALL)


def _is_file_path_entity(name: str) -> bool:
    """True if *name* looks like a filesystem path or a file basename.

    Heuristics (P2b.4): contains a ``/`` separator, or ends in a known
    code/markup extension.  ``http(s)://...`` URLs are NOT file paths.
    """
    if not name:
        return False
    lower = name.lower()
    if lower.startswith(("http://", "https://")):
        return False
    if "/" in name:
        return True
    return lower.endswith(_FILE_PATH_SUFFIXES)


def _content_hash(content: str) -> str:
    """Stable 16-char content hash for the extraction cache (P2c.3)."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def _extraction_cache_get(content_hash: str) -> Optional[list[tuple[str, str]]]:
    """Return cached entities for *content_hash* if present, else None.

    Updates LRU recency on hit.  Returns a list-copy so callers can
    mutate without poisoning the cache.
    """
    with _EXTRACTION_CACHE_LOCK:
        if content_hash in _EXTRACTION_CACHE:
            try:
                _EXTRACTION_CACHE_LRU.remove(content_hash)
            except ValueError:
                pass
            _EXTRACTION_CACHE_LRU.append(content_hash)
            return list(_EXTRACTION_CACHE[content_hash])
    return None


def _extraction_cache_put(content_hash: str, entities: list[tuple[str, str]]) -> None:
    """Store *entities* in the extraction cache, evicting oldest if full."""
    with _EXTRACTION_CACHE_LOCK:
        if content_hash in _EXTRACTION_CACHE:
            _EXTRACTION_CACHE[content_hash] = list(entities)
            try:
                _EXTRACTION_CACHE_LRU.remove(content_hash)
            except ValueError:
                pass
            _EXTRACTION_CACHE_LRU.append(content_hash)
            return
        if len(_EXTRACTION_CACHE_LRU) >= _EXTRACTION_CACHE_MAX:
            try:
                oldest = _EXTRACTION_CACHE_LRU.popleft()
                _EXTRACTION_CACHE.pop(oldest, None)
            except IndexError:
                pass
        _EXTRACTION_CACHE[content_hash] = list(entities)
        _EXTRACTION_CACHE_LRU.append(content_hash)


def clear_extraction_cache() -> None:
    """Clear the in-memory extraction cache.  Useful for tests."""
    with _EXTRACTION_CACHE_LOCK:
        _EXTRACTION_CACHE.clear()
        _EXTRACTION_CACHE_LRU.clear()


# ---------------------------------------------------------------------------
# Entity Extraction (LLM-free, pattern-based)
# ---------------------------------------------------------------------------

# Patterns for entity extraction
_PERSON_PATTERNS = [
    r"\b([A-Z][a-z]+ (?:[A-Z][a-z]+ )*[A-Z][a-z]+)\b",  # "John Smith"
    r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b",  # "John Smith" (2 words)
]

_ORG_PATTERNS = [
    r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)* (?:Inc|Corp|LLC|Ltd|Co|Company|Organization|Foundation|Institute|Laboratory|Lab|Studio))\b",
    r"\b((?:Google|Apple|Microsoft|Amazon|Meta|OpenAI|Anthropic|DeepMind|Tesla|SpaceX|Netflix|Stripe|Shopify|Vercel|Supabase|PostHog|Linear|Notion|Figma|Slack|Discord|GitHub|GitLab|AWS|GCP|Azure))\b",
]

_PLACE_PATTERNS = [
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,?\s*(?:CA|NY|TX|WA|OR|CO|MA|IL|PA|OH|GA|NC|MI|NJ|VA|FL|AZ|NV|UT|MN|WI|MD|TN|IN|MO|CT|SC|AL|KY|LA|OK|VT|WV|WY|ID|ME|MT|NE|NM|ND|SD))\b",
    r"\b(San Francisco|New York|Los Angeles|Chicago|Houston|Seattle|Portland|Austin|Boston|Denver|Miami|Atlanta|Dallas|Phoenix|Detroit|Nashville|Charlotte|Columbus|Indianapolis|Memphis|Baltimore|Milwaukee|Kansas City|Omaha|Raleigh|Tampa|Orlando|Pittsburgh|Cincinnati|Minneapolis|Cleveland)\b",
]

_DATE_PATTERNS = [
    r"\b(\d{4}-\d{2}-\d{2})\b",  # 2024-01-15
    r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4})\b",  # January 15, 2024
    r"\b(\d{1,2}/\d{1,2}/\d{4})\b",  # 01/15/2024
]

_CONCEPT_KEYWORDS = {
    # Languages & frameworks
    "python",
    "javascript",
    "typescript",
    "rust",
    "go",
    "java",
    "c++",
    "swift",
    "kotlin",
    "ruby",
    "php",
    "sql",
    "html",
    "css",
    "bash",
    "react",
    "vue",
    "angular",
    "node",
    "django",
    "flask",
    "fastapi",
    "spring",
    "rails",
    "laravel",
    "next.js",
    "nuxt",
    "svelte",
    "tailwind",
    "bootstrap",
    "jquery",
    "express",
    "fastify",
    # Infrastructure & cloud
    "docker",
    "kubernetes",
    "k8s",
    "aws",
    "gcp",
    "azure",
    "vercel",
    "netlify",
    "cloudflare",
    "heroku",
    "fly.io",
    "railway",
    "terraform",
    "ansible",
    "pulumi",
    # Databases & storage
    "postgresql",
    "mysql",
    "mongodb",
    "redis",
    "sqlite",
    "elasticsearch",
    "cassandra",
    "dynamodb",
    "bigquery",
    "snowflake",
    "clickhouse",
    "supabase",
    "neon",
    "planetscale",
    "qdrant",
    "pinecone",
    "weaviate",
    "chroma",
    "milvus",
    # AI & ML
    "machine learning",
    "deep learning",
    "neural network",
    "transformer",
    "llm",
    "gpt",
    "claude",
    "gemini",
    "bert",
    "attention",
    "embedding",
    "rag",
    "retrieval augmented generation",
    "fine-tuning",
    "prompt engineering",
    "chain of thought",
    "langchain",
    "llamaindex",
    "haystack",
    "crewai",
    "openai",
    "anthropic",
    "google ai",
    "mistral",
    "meta ai",
    # Memory system domain
    "fts5",
    "bm25",
    "vector search",
    "knowledge graph",
    "spaced repetition",
    "forgetting curve",
    "ebbinghaus",
    "temporal decay",
    "recency",
    "importance score",
    "fitness",
    "consolidation",
    "deduplication",
    "backfill",
    "backlink",
    "crdt",
    "version vector",
    "logical clock",
    "conflict resolution",
    "lww",
    "last-writer-wins",
    "supersede",
    "coexist",
    "replace",
    "saga",
    "transaction",
    "rollback",
    "compensation",
    "hot tier",
    "warm tier",
    "cold tier",
    "tier migration",
    "save pipeline",
    "search pipeline",
    "write path",
    "read path",
    "mcp server",
    "mcp tool",
    "agent harness",
    "context window",
    "semantic search",
    "hybrid search",
    "fusion",
    "reranker",
    "cross-encoder",
    "bi-encoder",
    "model2vec",
    "usearch",
    "prompt injection",
    "safety wiring",
    "quality gate",
    "audit log",
    "observability",
    "metrics",
    "dashboard",
    "self-directed",
    "heartbeat",
    "auto-healing",
    "adaptive retention",
    "psi formula",
    "sm-2",
    "contradiction detection",
    "fact extraction",
    "spo triple",
    "named entity recognition",
    "ner",
    "entity linking",
    # Software engineering
    "api",
    "rest",
    "graphql",
    "grpc",
    "websocket",
    "http",
    "tcp",
    "udp",
    "git",
    "github",
    "gitlab",
    "ci/cd",
    "devops",
    "mlops",
    "bug",
    "feature",
    "refactor",
    "test",
    "deploy",
    "release",
    "sprint",
    "database",
    "schema",
    "migration",
    "index",
    "query",
    "cache",
    "security",
    "auth",
    "oauth",
    "jwt",
    "encryption",
    "cors",
    "performance",
    "latency",
    "throughput",
    "scalability",
    "memory",
    "vector",
    "embedding",
    "search",
    "retrieval",
    "agent",
    "mcp",
    "tool",
    "function",
    "prompt",
    "microservice",
    "monolith",
    "serverless",
    "edge",
    "distributed system",
    "consensus",
    "raft",
    "paxos",
    "pipeline",
    "hook",
    "cron",
    "background job",
    "lock",
    "mutex",
    "semaphore",
    "race condition",
    "deadlock",
    "circuit breaker",
    "retry",
    "exponential backoff",
    "logging",
    "monitoring",
    "alerting",
    "tracing",
    # Data & content
    "markdown",
    "frontmatter",
    "yaml",
    "json",
    "toml",
    "csv",
    "chunk",
    "token",
    "embedding vector",
    "dimension",
    "similarity",
    "cosine",
    "euclidean",
    "jaccard",
    "hallucination",
    "grounding",
    "attribution",
    "benchmark",
    "evaluation",
    "locomo",
    "longmemeval",
    "beam",
}


def _extract_capitalized_phrases(text: str) -> list[tuple[str, str]]:
    """Extract capitalized phrases as potential entities."""
    entities = []
    # Multi-word proper nouns (2-3 words)
    for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", text):
        name = match.group(1)
        # Skip if it's a common word
        if name.lower() not in {
            "the",
            "this",
            "that",
            "what",
            "when",
            "where",
            "how",
            "why",
        }:
            # Classify: check if it looks like an organization, place, person, or concept
            lower_name = name.lower()
            if any(
                w in lower_name
                for w in (
                    "university",
                    "institute",
                    "company",
                    "corp",
                    "inc",
                    "ltd",
                    "llc",
                    "foundation",
                    "laboratory",
                    "lab",
                    "studio",
                    "studio",
                    "agency",
                    "enterprise",
                    "ventures",
                    "capital",
                    "association",
                    "council",
                    "committee",
                    "department",
                )
            ):
                entities.append((name, "organization"))
            elif any(
                w in lower_name
                for w in (
                    "city",
                    "town",
                    "village",
                    "mountain",
                    "river",
                    "lake",
                    "coast",
                    "bay",
                    "valley",
                    "county",
                    "state",
                    "province",
                    "district",
                    "region",
                    "territory",
                    "island",
                    "peninsula",
                )
            ):
                entities.append((name, "place"))
            elif any(
                w in lower_name
                for w in (
                    "learning",
                    "network",
                    "system",
                    "model",
                    "algorithm",
                    "theory",
                    "framework",
                    "protocol",
                    "pattern",
                    "architecture",
                    "pipeline",
                    "engine",
                    "layer",
                    "module",
                    "component",
                    "service",
                    "manager",
                    "handler",
                    "provider",
                    "adapter",
                    "interface",
                    "implementation",
                    "strategy",
                    "factory",
                    "registry",
                    "repository",
                    "store",
                    "index",
                    "cache",
                    "queue",
                    "worker",
                    "scheduler",
                    "orchestrator",
                    "middleware",
                    "gateway",
                    "proxy",
                    "router",
                    "balancer",
                    "token",
                    "session",
                    "cookie",
                    "credential",
                    "secret",
                    "policy",
                    "rule",
                    "validator",
                    "guard",
                    "filter",
                    "processor",
                    "converter",
                    "transformer",
                    "encoder",
                    "decoder",
                )
            ):
                entities.append((name, "concept"))
            elif len(name.split()) == 1 and name[0].isupper():
                entities.append((name, "person"))
            else:
                entities.append((name, "concept"))
    return entities


def _extract_organizations(text: str) -> list[tuple[str, str]]:
    """Extract organization names."""
    entities = []
    for pattern in _ORG_PATTERNS:
        for match in re.finditer(pattern, text):
            entities.append((match.group(1), "organization"))
    return entities


def _extract_places(text: str) -> list[tuple[str, str]]:
    """Extract place names."""
    entities = []
    for pattern in _PLACE_PATTERNS:
        for match in re.finditer(pattern, text):
            entities.append((match.group(1), "place"))
    return entities


def _extract_dates(text: str) -> list[tuple[str, str]]:
    """Extract date references."""
    entities = []
    for pattern in _DATE_PATTERNS:
        for match in re.finditer(pattern, text):
            entities.append((match.group(1), "date"))
    return entities


def _extract_concepts(text: str) -> list[tuple[str, str]]:
    """Extract technical concepts from keyword list."""
    entities = []
    text_lower = text.lower()
    for concept in _CONCEPT_KEYWORDS:
        if re.search(rf"\b{re.escape(concept)}\b", text_lower):
            entities.append((concept, "concept"))
    return entities


def _extract_emails(text: str) -> list[tuple[str, str]]:
    """Extract email addresses."""
    entities = []
    for match in re.finditer(
        r"\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b", text
    ):
        entities.append((match.group(1), "email"))
    return entities


def _extract_urls(text: str) -> list[tuple[str, str]]:
    """Extract URLs."""
    entities = []
    for match in re.finditer(r"(https?://[^\s]+)", text):
        entities.append((match.group(1), "url"))
    return entities


def extract_entities(text: str, min_occurrences: int = 2) -> list[tuple[str, str]]:
    """Extract entities from text using pattern-based NER.

    Returns list of (name, entity_type) tuples.
    Deduplicates by (normalized_name, type).

    Pipeline (P2b tightening):
      1. Strip YAML frontmatter and fenced code blocks so the regex
         patterns don't see them.
      2. Run all extractors against the cleaned text.
      3. Filter: drop garbage patterns, drop markdown stop-words,
         drop file-path-shaped entities.
      4. Frequency filter (P2b.5): keep an entity only if it appears
         at least *min_occurrences* times in the cleaned text, OR it
         is in the curated ``_CONCEPT_KEYWORDS`` list (which we want
         to keep even on a single mention because the keyword list
         itself is a strong relevance signal).

    The default ``min_occurrences=2`` is the "tight" setting; pass
    ``min_occurrences=1`` for backwards-compatible single-mention
    behaviour.  Note: lowering this will increase noise.
    """
    if not text:
        return []

    # Clean content so we don't extract entities from code or frontmatter
    cleaned = _strip_frontmatter(text)
    cleaned = _strip_code_blocks(cleaned)
    if not cleaned:
        return []

    all_entities = []
    all_entities.extend(_extract_capitalized_phrases(cleaned))
    all_entities.extend(_extract_organizations(cleaned))
    all_entities.extend(_extract_places(cleaned))
    all_entities.extend(_extract_dates(cleaned))
    all_entities.extend(_extract_concepts(cleaned))
    all_entities.extend(_extract_emails(cleaned))
    all_entities.extend(_extract_urls(cleaned))

    # Patterns that indicate garbage entities (date strings, UUIDs, …)
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

    # Deduplicate + filter
    seen = set()
    unique: list[tuple[str, str]] = []
    cleaned_lower = cleaned.lower()
    for name, etype in all_entities:
        name_clean = name.lower().strip()
        if not name_clean or len(name_clean) < 2:
            continue
        if _GARBAGE_RE.match(name_clean):
            continue
        # Markdown / code stop-words (P2b.1)
        if name_clean in _MARKDOWN_STOPWORDS:
            continue
        # File-path-shaped entities (P2b.4)
        if _is_file_path_entity(name):
            continue
        if "." in name and len(name) < 8:
            continue
        key = (name_clean, etype)
        if key in seen:
            continue

        # Frequency filter (P2b.5): keep entities that recur in the
        # text, plus curated _CONCEPT_KEYWORDS which are strong
        # relevance signals even on a single mention.
        if min_occurrences > 1:
            in_concept_list = name_clean in _CONCEPT_KEYWORDS
            if not in_concept_list:
                occ = len(re.findall(rf"\b{re.escape(name_clean)}\b", cleaned_lower))
                if occ < min_occurrences:
                    continue

        seen.add(key)
        unique.append((name, etype))

    # Optional spaCy NER augmentation (P2: off by default).
    # Augments regex-extracted entities with spaCy PERSON/ORG/GPE/
    # PRODUCT/FAC entities when MEMORY_NER_SPACY=1.
    try:
        from infra._lazy_imports import get_config
        if get_config().ner_spacy_enabled:
            from knowledge_graph.ner_spacy import augment_entities
            unique = unique + augment_entities(cleaned, unique)
    except Exception as e:
        logger.warning("extract_entities failed: %s", e)

    return unique


# ---------------------------------------------------------------------------
# Relation Extraction (LLM-free, pattern-based)
# ---------------------------------------------------------------------------

# Simple relation patterns
_RELATION_PATTERNS = [
    (r"\b(\w+)\s+(?:works?\s+(?:at|for|with))\s+(\w+)", "works_at"),
    (r"\b(\w+)\s+(?:created|built|made|developed|designed)\s+(\w+)", "created"),
    (r"\b(\w+)\s+(?:uses?|using)\s+(\w+)", "uses"),
    (r"\b(\w+)\s+(?:is\s+(?:a|an|the)\s+\w+\s+(?:at|for|in))\s+(\w+)", "position_at"),
    (r"\b(\w+)\s+(?:located\s+(?:in|at))\s+(\w+)", "located_in"),
    (r"\b(\w+)\s+(?:depends?\s+(?:on|upon))\s+(\w+)", "depends_on"),
    (r"\b(\w+)\s+(?:related?\s+to)\s+(\w+)", "related_to"),
    (r"\b(\w+)\s+(?:partner(?:s|ed)?\s+with)\s+(\w+)", "partners_with"),
    (r"\b(\w+)\s+(?:acquired?\s+)\s+(\w+)", "acquired"),
    (r"\b(\w+)\s+(?:manages?|leads?|owns?)\s+(\w+)", "manages"),
]


@deprecated("extract_relations is deprecated; use index_kg_for_memory instead.")
def extract_relations(
    text: str, known_entities: dict[str, int] | None = None
) -> list[tuple[str, str, str]]:
    """Extract relations from text using pattern matching.

    Returns list of (source_name, relation_type, target_name) tuples.
    known_entities: optional dict mapping normalized entity names to IDs
    for validation.

    B22: deprecated.  The pattern-based path was deemed "too noisy" (see
    comment at #877).  Kept for backwards compatibility; new code should
    rely on the LLM-backed extraction in index_kg_for_memory.
    """
    if not text:
        return []

    relations = []
    text.lower()

    for pattern, rel_type in _RELATION_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            source = match.group(1).strip()
            target = match.group(2).strip()
            # Skip very short names
            if len(source) < 2 or len(target) < 2:
                continue
            relations.append((source, rel_type, target))

    # Also extract co-occurrence relations for sentences with 2+ entities
    # Cap at 4 entities per sentence to prevent clique explosion
    sentences = re.split(r"[.!?]+", text)
    for sentence in sentences:
        entities_in_sentence = []
        for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", sentence):
            entities_in_sentence.append(match.group(1))
        # Cap entities to prevent N*(N-1)/2 explosion
        entities_in_sentence = entities_in_sentence[:4]
        # Create co-occurrence edges between all pairs
        for i in range(len(entities_in_sentence)):
            for j in range(i + 1, len(entities_in_sentence)):
                src = entities_in_sentence[i]
                tgt = entities_in_sentence[j]
                if src != tgt:
                    relations.append((src, "co_occurs", tgt))

    # Deduplicate
    seen = set()
    unique = []
    for src, rel, tgt in relations:
        key = (src.lower(), rel, tgt.lower())
        if key not in seen:
            seen.add(key)
            unique.append((src, rel, tgt))

    return unique
