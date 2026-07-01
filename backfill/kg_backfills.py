"""Knowledge-graph backfill functions for the backfill pipeline.

Extracted from backfill_all.py (2026-06-20) as part of the god-module
decomposition. Contains:

- _is_stopword: noise-word filter
- _is_valid_entity: entity quality filter (P3.2)
- _backfill_kg_facts: extract facts from memories into kg_facts
- _backfill_kg_graph: derive kg_entities + kg_edges from kg_facts

Behavior is identical to the inline versions. Re-exported from
backfill_all for backward compat.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)


# English stopwords + technical noise that the regex extraction
# pulls out as "entities" but are useless for knowledge graph queries.
_ENTITY_STOPWORDS = frozenset(
    {
        # English articles, pronouns, demonstratives
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "them",
        "his",
        "her",
        "my",
        "your",
        "our",
        "their",
        "me",
        "us",
        "him",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "should",
        "could",
        "can",
        "may",
        "might",
        "must",
        "shall",
        "ought",
        # Common prepositions / conjunctions
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "from",
        "as",
        "into",
        "about",
        "between",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "up",
        "down",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "and",
        "or",
        "but",
        "if",
        "while",
        "so",
        "than",
        "that",
        "because",
        "although",
        "since",
        "unless",
        "until",
        # Common adverbs / determiners
        "not",
        "no",
        "yes",
        "all",
        "any",
        "some",
        "most",
        "more",
        "less",
        "much",
        "many",
        "few",
        "several",
        "other",
        "such",
        "only",
        "own",
        "same",
        "very",
        "too",
        "also",
        # Markdown / formatting tokens that the regex catches
        "section",
        "header",
        "footer",
        "title",
        "subtitle",
        "code",
        "codeblock",
        "pre",
        "blockquote",
        "table",
        "row",
        "col",
        "item",
        "items",
        "list",
        "note",
        "notes",
        "see",
        "also",
        "example",
        "examples",
        "todo",
        "fixme",
        "xxx",
        "tbd",
        # Common verb forms (less useful as entities on their own)
        "use",
        "used",
        "uses",
        "using",
        "make",
        "makes",
        "made",
        "create",
        "creates",
        "created",
        "creating",
        "have",
        "has",
        "having",
        "include",
        "includes",
        "including",
        "included",
        "set",
        "sets",
        "setting",
        "get",
        "gets",
        "getting",
        "run",
        "runs",
        "running",
        "ran",
        "go",
        "goes",
        "going",
        "went",
        "gone",
        "add",
        "adds",
        "adding",
        "added",
        "remove",
        "removes",
        "removing",
        "removed",
        "delete",
        "deletes",
        "deleting",
        "deleted",
        "update",
        "updates",
        "updating",
        "updated",
        "check",
        "checks",
        "checking",
        "checked",
        "test",
        "tests",
        "testing",
        "tested",
        "find",
        "finds",
        "finding",
        "found",
        "give",
        "gives",
        "giving",
        "gave",
        "given",
        "call",
        "calls",
        "calling",
        "called",
        "try",
        "tries",
        "trying",
        "tried",
        "ask",
        "asks",
        "asking",
        "asked",
        "need",
        "needs",
        "needing",
        "needed",
        "feel",
        "feels",
        "feeling",
        "felt",
        "leave",
        "leaves",
        "leaving",
        "left",
        "put",
        "puts",
        "putting",
        "mean",
        "means",
        "meaning",
        "meant",
        "keep",
        "keeps",
        "keeping",
        "kept",
        "let",
        "lets",
        "letting",
        "begin",
        "begins",
        "beginning",
        "began",
        "begun",
        "help",
        "helps",
        "helping",
        "helped",
        "turn",
        "turns",
        "turning",
        "turned",
        "start",
        "starts",
        "starting",
        "started",
        "show",
        "shows",
        "showing",
        "showed",
        "shown",
        "hear",
        "hears",
        "hearing",
        "heard",
        "play",
        "plays",
        "playing",
        "played",
        "move",
        "moves",
        "moving",
        "moved",
        "live",
        "lives",
        "living",
        "lived",
        "believe",
        "believes",
        "believing",
        "believed",
        "bring",
        "brings",
        "bringing",
        "brought",
        "happen",
        "happens",
        "happening",
        "happened",
        "write",
        "writes",
        "writing",
        "wrote",
        "written",
        "provide",
        "provides",
        "providing",
        "provided",
        "sit",
        "sits",
        "sitting",
        "sat",
        "lose",
        "loses",
        "losing",
        "lost",
        "pay",
        "pays",
        "paying",
        "paid",
        "meet",
        "meets",
        "meeting",
        "met",
        "continue",
        "continues",
        "continuing",
        "continued",
        "learn",
        "learns",
        "learning",
        "learned",
        "learnt",
        "change",
        "changes",
        "changing",
        "changed",
        "lead",
        "leads",
        "leading",
        "led",
        "understand",
        "understands",
        "understanding",
        "understood",
        "watch",
        "watches",
        "watching",
        "watched",
        "follow",
        "follows",
        "following",
        "followed",
        "stop",
        "stops",
        "stopping",
        "stopped",
        "speak",
        "speaks",
        "speaking",
        "spoke",
        "spoken",
        "read",
        "reads",
        "reading",
        "allow",
        "allows",
        "allowing",
        "allowed",
        "spend",
        "spends",
        "spending",
        "spent",
        "grow",
        "grows",
        "growing",
        "grew",
        "grown",
        "open",
        "opens",
        "opening",
        "opened",
        "walk",
        "walks",
        "walking",
        "walked",
        "win",
        "wins",
        "winning",
        "won",
        "offer",
        "offers",
        "offering",
        "offered",
        "remember",
        "remembers",
        "remembering",
        "remembered",
        "consider",
        "considers",
        "considering",
        "considered",
        "appear",
        "appears",
        "appearing",
        "appeared",
        "buy",
        "buys",
        "buying",
        "bought",
        "wait",
        "waits",
        "waiting",
        "waited",
        "serve",
        "serves",
        "serving",
        "served",
        "die",
        "dies",
        "dying",
        "died",
        "send",
        "sends",
        "sending",
        "sent",
        "expect",
        "expects",
        "expecting",
        "expected",
        "build",
        "builds",
        "building",
        "built",
        "stay",
        "stays",
        "staying",
        "stayed",
        "fall",
        "falls",
        "falling",
        "fell",
        "fallen",
        "cut",
        "cuts",
        "cutting",
        "reach",
        "reaches",
        "reaching",
        "reached",
        "kill",
        "kills",
        "killing",
        "killed",
        "remain",
        "remains",
        "remaining",
        "remained",
    }
)


def _is_stopword(name: str) -> bool:
    """Return True if `name` is a noise word that should not be an entity."""
    return name.lower() in _ENTITY_STOPWORDS

def _is_valid_entity(name: str, min_len: int = 3) -> bool:
    """Return True if `name` is a useful entity (not noise).

    Filters:
      - too short (< min_len chars)
      - punctuation-only (no alphanumeric chars)
      - stopword (in _ENTITY_STOPWORDS)
      - starts/ends with non-alphanumeric
      - pure-numeric
    """
    if not name:
        return False
    n = name.strip()
    if len(n) < min_len:
        return False
    if not any(c.isalnum() for c in n):
        return False
    if n.isdigit():
        return False
    if not n[0].isalnum() or not n[-1].isalnum():
        return False
    if _is_stopword(n):
        return False
    return True


def _backfill_kg_facts(
    conn,
    commit_every: int = 50,
    progress_every: int = 100,
):
    """Extract facts from all memories into kg_facts.

    2026-06-19 fix: per-batch commits + progress markers.

    Previously this function held a single huge transaction and committed
    only at the end, which made the 2-3 hour LLM extraction run risky
    (any crash/kill lost all progress). It also logged nothing during
    the extraction, so a stuck or slow run looked identical to a healthy
    one. The function now:
      - commits every `commit_every` memories (default 50)
      - logs progress every `progress_every` memories (default 100)
      - is safe to kill (Ctrl-C, OOM) — partial progress persists
      - returns the total_facts count so callers can log final state

    The extraction function ``index_facts_for_memory`` already uses
    UPSERT semantics internally, so re-running on the same memory
    is idempotent (no duplicate facts).
    """
    if os.environ.get("MEMORY_KNOWLEDGE_GRAPH") == "0":
        logger.info("KG backfill skipped (MEMORY_KNOWLEDGE_GRAPH=0)")
        return 0

    try:
        from fact import ensure_facts_schema, index_facts_for_memory

        ensure_facts_schema(conn)
    except Exception as e:
        logger.warning("Cannot initialize KG schema: %s", e)
        return 0

    rows = conn.execute(
        "SELECT id, content FROM memories WHERE deleted_at IS NULL ORDER BY id"
    ).fetchall()
    total = len(rows)
    total_facts = 0
    errors = 0
    t_start = time.time()
    logger.info(
        "KG facts backfill: starting on %d memories (commit_every=%d, progress_every=%d)",
        total,
        commit_every,
        progress_every,
    )
    for i, (mem_id, content) in enumerate(rows, start=1):
        if not content or len(content) < 20:
            continue
        try:
            result = index_facts_for_memory(conn, mem_id, content)
            total_facts += result.get("facts", 0)
        except Exception as e:
            errors += 1
            logger.debug("Fact extraction failed for %s: %s", mem_id, e)
        # Per-batch commit so progress survives crashes
        if i % commit_every == 0:
            try:
                conn.commit()
            except Exception as e:
                logger.warning("Commit failed at i=%d: %s", i, e)
        # Progress marker
        if i % progress_every == 0 or i == total:
            elapsed = time.time() - t_start
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            logger.info(
                "KG facts backfill: %d/%d memories processed (%.1f/s, "
                "%.0fs elapsed, ETA %.0fs, %d facts so far, %d errors)",
                i,
                total,
                rate,
                elapsed,
                eta,
                total_facts,
                errors,
            )
    # Final commit
    try:
        conn.commit()
    except Exception as e:
        logger.warning("Final commit failed: %s", e)
    logger.info(
        "KG facts backfilled: %d facts from %d memories (%d errors, %.1fs total)",
        total_facts,
        total,
        errors,
        time.time() - t_start,
    )
    return total_facts


def _backfill_kg_graph(conn):
    """Derive kg_entities + kg_edges from kg_facts."""
    try:
        has_facts = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
    except Exception:
        return
    if has_facts == 0:
        return

    # Ensure tables exist
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kg_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                entity_type TEXT,
                mentions INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(name, entity_type)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kg_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES kg_entities(id),
                target_id INTEGER NOT NULL REFERENCES kg_entities(id),
                relation TEXT NOT NULL DEFAULT 'related_to',
                weight REAL DEFAULT 1.0,
                created_at TEXT,
                valid_at TEXT,
                invalid_at TEXT,
                UNIQUE(source_id, target_id, relation)
            )
        """)
    except Exception as e:
        logger.warning("Cannot create KG tables: %s", e)
        return

    existing_entities = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
    existing_edges = conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
    if existing_entities > 0 and existing_edges > 0:
        return

    # 2026-06-19 (P3.2): filter low-quality entities. With ~1,800 facts
    # we were producing ~3,250 entities, most of them noise — single
    # letters, English stopwords, markdown tokens. Add:
    #   1. min_entity_length (default 3 chars)
    #   2. min_entity_mentions (default 2 — must appear in ≥2 facts)
    #   3. stopword list (English + technical noise)
    #   4. punctuation-only filter
    #   5. compute real mention counts (was hardcoded to 1)
    min_len = int(os.environ.get("MEMORY_KG_MIN_ENTITY_LENGTH", "3"))
    # 2026-06-19 P3.2: default to 1 (was 2) — most entities in this corpus
    # are unique to one fact. Raise via MEMORY_KG_MIN_ENTITY_MENTIONS=2
    # for a stricter graph.
    min_mentions = int(os.environ.get("MEMORY_KG_MIN_ENTITY_MENTIONS", "1"))
    # Predicates like "has_description" / "has_value" create long descriptive
    # strings as their object — these are NOT real entities. Default
    # exclusion list keeps the entity graph focused on real concepts.
    # Override with MEMORY_KG_INCLUDE_ALL_PREDICATES=1 to include them.
    exclude_predicates: set[str] = set()
    if os.environ.get("MEMORY_KG_INCLUDE_ALL_PREDICATES") != "1":
        exclude_predicates = {"has_description", "has_value", "deletes", "checks"}

    # Build entities from unique subjects + objects + count mentions.
    # Predicate filter applies to OBJECTS only: a fact like
    # "X has_description Y" has Y as a long descriptive string (not an
    # entity), but X is still a valid subject entity.
    all_facts = conn.execute(
        "SELECT subject, predicate, object, confidence, source_memory FROM kg_facts"
    ).fetchall()
    mention_counts: dict[str, int] = {}
    for subj, pred, obj, _, _ in all_facts:
        # Subject is always a candidate entity
        mention_counts[subj.lower()] = mention_counts.get(subj.lower(), 0) + 1
        # Object is a candidate only if the predicate is structural
        if pred not in exclude_predicates:
            mention_counts[obj.lower()] = mention_counts.get(obj.lower(), 0) + 1

    kept_count = 0
    dropped = {"too_short": 0, "stopword": 0, "punctuation": 0, "low_mentions": 0}
    for name, count in mention_counts.items():
        if not _is_valid_entity(name, min_len=min_len):
            if len(name) < min_len:
                dropped["too_short"] += 1
            elif _is_stopword(name):
                dropped["stopword"] += 1
            else:
                dropped["punctuation"] += 1
            continue
        if count < min_mentions:
            dropped["low_mentions"] += 1
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO kg_entities (name, entity_type, mentions, created_at, updated_at) "
                "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                (name, "concept", count),
            )
            kept_count += 1
        except Exception:
            pass

    logger.info(
        "KG entity filter: %d kept, %d dropped (too_short=%d, stopword=%d, "
        "punctuation=%d, low_mentions=%d, min_len=%d, min_mentions=%d, "
        "excluded_object_predicates=%d)",
        kept_count,
        sum(dropped.values()),
        dropped["too_short"],
        dropped["stopword"],
        dropped["punctuation"],
        dropped["low_mentions"],
        min_len,
        min_mentions,
        len(exclude_predicates),
    )

    # Build edges from SPO triples.
    # Predicate filter applies to OBJECTS (matches entity-creation logic).
    entity_ids = {}
    for row in conn.execute("SELECT id, name FROM kg_entities").fetchall():
        entity_ids[row[1].lower()] = row[0]

    seen_edges = set()
    for subj, pred, obj, conf, _ in all_facts:
        # Subject is always a real entity; object is too unless its
        # predicate is in the description-exclude list
        if pred in exclude_predicates:
            continue
        src = entity_ids.get(subj.lower())
        tgt = entity_ids.get(obj.lower())
        if src and tgt and src != tgt:
            edge_key = (src, tgt, pred)  # use real predicate, not "relates_to"
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO kg_edges "
                        "(source_id, target_id, relation, weight, created_at) "
                        "VALUES (?, ?, ?, ?, datetime('now'))",
                        (src, tgt, pred, conf),
                    )
                except Exception:
                    pass

    logger.info(
        "KG graph backfilled: %d entities, %d edges", kept_count, len(seen_edges)
    )
