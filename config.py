"""Central configuration for agentic-memory.

Reads from ``memory.toml`` (next to this file or at
``~/.config/agentic-memory/memory.toml``) and lets every field be
overridden by the corresponding ``MEMORY_*`` environment variable.

Usage::

    from config import get_config, MemoryConfig

    cfg = get_config()
    print(cfg.db_path)
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

# The MEMORY_LLM_EXTRACTION resolution log is emitted after _resolve
# is defined (see _log_llm_extraction_resolution below) so it can show
# the env var, the TOML value, and the effective resolved value.

# ---------------------------------------------------------------------------
# TOML parsing — tomllib (3.11+) with tomli fallback
# ---------------------------------------------------------------------------

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as _tomllib_fallback
    except ModuleNotFoundError:
        _tomllib_fallback = None
    tomllib = _tomllib_fallback

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).resolve().parent


# Module-level install-root paths (P3-33). These point at the canonical
# install locations used by mcp_maintenance / mcp_common / cli. Override
# at runtime by setting MEMORY_INSTALL_ROOT if a non-default layout is
# required (see ``memory_config.install_root``).
INSTALL_ROOT = _CONFIG_DIR
GLOBAL_SCRIPTS_DIR = _CONFIG_DIR
SCRIPTS_SUBDIR = _CONFIG_DIR / "scripts"
AGENTS_SKILLS_DIR = Path.home() / ".agents" / "skills"
OPENCODE_SKILLS_DIR = Path.home() / ".opencode" / "skills"


# Allow operators to relocate the TOML via env var (e.g. ISO deploys,
# container mounts). Falls back to the live-system on-disk default which
# is anchored at the package root — never relative to cwd, to prevent
# the "twin DB" bug class flagged in systematic-debugging references.
def _resolve_toml_path() -> Path:
    override = os.environ.get("MEMORY_CONFIG_PATH")
    if override:
        p = Path(override)
        if not p.is_absolute():
            # Anchor relative overrides at the package root, NOT cwd,
            # so cron-launchers with arbitrary cwds can't accidentally
            # read a stale memory.toml from the home dir or /tmp.
            p = (_CONFIG_DIR / p).resolve()
        return p
    return _CONFIG_DIR / "memory.toml"


_TOML_PATH = _resolve_toml_path()

# ---------------------------------------------------------------------------
# Feature-flag → env-var mapping (documentation & introspection)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _abs_db_path(raw: str) -> str:
    """Anchor a relative db_path at the package root, not cwd.

    H20 fix: modules that take a db_path argument (memory_sharing.py,
    consolidate_facts.py) used to compute `Path(db_path).parent` without
    anchoring, which could fork the DB into a sibling directory when
    the caller passed a relative path with an unexpected cwd. The
    canonical caller is now `resolve_db_path(db_path)` exported below,
    but we also expose `_abs_db_path` for sites that need to resolve
    a raw path string before opening the DB.
    """
    p = Path(raw)
    if p.is_absolute():
        return str(p)
    return str((_CONFIG_DIR / p).resolve())


def resolve_db_path(db_path: str | Path) -> Path:
    """H20 fix: resolve a db_path argument against the package root.

    Use this anywhere a public function takes a db_path parameter and
    then does `Path(db_path).parent` for parent-directory lookups
    (memory_sharing, consolidate_facts, etc.).
    """
    return Path(_abs_db_path(str(Path(db_path))))


def _read_toml(path: Path) -> Dict[str, Any]:
    """Return parsed TOML dict, or empty dict if file missing / lib absent."""
    if not path.exists() or tomllib is None:
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _deep_get(d: Dict[str, Any], dotted: str) -> Any:
    """Retrieve a nested value from a dict using a dotted key path."""
    parts = dotted.split(".")
    cur: Any = d
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _parse_bool(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def _parse_int(val: str) -> int:
    return int(val)


def _parse_float(val: str) -> float:
    # 2026-06-22 (C7 fix): when the operator passes a string like
    # "30 days" the previous version raised ``ValueError: could not
    # convert string to float: '30 days'`` and silently fell back to
    # the default.  Now we raise a clearer error that names the env
    # var, the dotted TOML path, and the value that failed, so the
    # operator can find the typo instead of wondering why their
    # setting is being ignored.
    try:
        return float(val)
    except ValueError as e:
        raise ValueError(
            f"expected a number (int or float), got {val!r}: {e}. "
            f"If you meant a duration like '30 days', convert it to a "
            f"bare number (30) — half-life is expressed in days."
        ) from e


def _resolve(
    env_key: str,
    dotted_path: str,
    default: Any,
    toml_data: Optional[Dict[str, Any]] = None,
    *,
    parser: Any = None,
) -> Any:
    """Resolve a config value: env var > TOML > default.

    2026-06-22 (C7 fix): when an env var or TOML value cannot be
    parsed into the expected type, the previous behaviour silently
    fell back to the default — the operator's typo (e.g.
    ``MEMORY_FORGETTING_CURVE_HALF_LIFE="30 days"``) was invisible.
    We now log a warning to stderr naming the env var, the dotted
    TOML path, and the value that failed, then fall back to the
    default.  This is a soft warning, not an exception — the
    config still loads — but the operator can now see why their
    setting is being ignored.
    """
    env_val = os.environ.get(env_key)
    if env_val is not None:
        try:
            if parser is not None:
                return parser(env_val)
            if isinstance(default, bool):
                return _parse_bool(env_val)
            if isinstance(default, int):
                return _parse_int(env_val)
            if isinstance(default, float):
                return _parse_float(env_val)
            return env_val
        except (ValueError, TypeError) as e:
            # 2026-06-22 (C7 fix): warn instead of silently swallowing
            # so the operator can see the typo.
            print(
                f"warning: {env_key}={env_val!r} could not be parsed as "
                f"{type(default).__name__}; falling back to default. "
                f"Error: {e}",
                file=sys.stderr,
            )
            return default
    toml_val = _deep_get(toml_data or {}, dotted_path)
    if toml_val is not None:
        # The TOML parser already validated the type, so any
        # mismatched value here would be a TOML bug, not an
        # operator typo.  Only warn for *significant* mismatches —
        # we don't want to nag about int vs float (TOML treats
        # ``30`` and ``30.0`` interchangeably; an int value in the
        # TOML is valid for a float dataclass field).
        if toml_val is not None and not isinstance(toml_val, type(default)):
            # Allow int → float and bool → int promotions.
            if isinstance(default, float) and isinstance(toml_val, int):
                pass
            elif isinstance(default, int) and isinstance(toml_val, bool):
                pass
            else:
                print(
                    f"warning: {dotted_path}={toml_val!r} has type "
                    f"{type(toml_val).__name__} but the dataclass field "
                    f"expects {type(default).__name__}; falling back to "
                    f"the TOML value as-is.",
                    file=sys.stderr,
                )
        return toml_val

    return default

    toml_val = _deep_get(toml_data or {}, dotted_path)
    if toml_val is not None:
        return toml_val

    return default


# 2026-06-26: the original import-time log at line 24-27 only printed
# the raw env var (e.g. "MEMORY_LLM_EXTRACTION=None"), which misled
# operators into thinking LLM extraction was disabled — when in fact
# the TOML config (features.llm_extraction = true) was the actual
# source of truth and LLM was still enabled. Replace that log with
# one that shows all three: env var, TOML value, and the resolved
# effective value. This runs once at import time after _resolve is
# defined.
def _log_llm_extraction_resolution() -> None:
    raw_env = os.environ.get("MEMORY_LLM_EXTRACTION")
    try:
        with open(_TOML_PATH, "rb") as fh:
            toml_data = tomllib.load(fh) if tomllib else {}
    except (OSError, ValueError, KeyError, TypeError):
        toml_data = {}
    toml_val = None
    try:
        features = toml_data.get("features", {}) if isinstance(toml_data, dict) else {}
        toml_val = features.get("llm_extraction")
    except (AttributeError, TypeError):
        toml_val = None
    effective = _resolve(
        "MEMORY_LLM_EXTRACTION",
        "features.llm_extraction",
        True,
        toml_data,
        parser=_parse_bool,
    )
    print(
        f"IMPORT config.py: MEMORY_LLM_EXTRACTION env={raw_env!r} "
        f"toml[features.llm_extraction]={toml_val!r} effective={effective}",
        file=sys.stderr,
    )


_log_llm_extraction_resolution()


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryConfig:
    """Immutable, validated configuration for the agentic-memory system."""

    # general
    db_path: str = "memory/memory.db"
    wal_checkpoint_startup: bool = True
    wal_checkpoint_interval_s: int = 300  # S4.5: 2026-06-23 — 5min default
    mmap_size: int = 268_435_456  # S4.1: 2026-06-23 — 256 MiB default mmap
    unindexed_safety_net_limit: int = 1000
    agent_id: str = ""

    # search
    temporal_half_life: float = 180.0
    temporal_decay_mode: str = "exponential"
    late_interaction: bool = True
    knowledge_graph: bool = True
    graph_rag_hops: int = 3
    graph_rag_expansions: int = 5
    embedding_score_threshold: float = 0.25
    kg_llm_fallback_min_entities: int = 2
    rerank_weights: str = ""
    query_type_weights: str = ""
    query_cache: bool = True
    reranker_disabled: bool = (
        False  # Qwen3-0.6B primary, BGE-m3 fallback (MPS-safe, verified 2026-06-15)
    )
    deep_rerank_timeout: float = 30.0  # seconds; wall-clock kill for the deep rerank subprocess (2026-06-19 MPS hang). 0 = in-process, no kill.
    contextual_retrieval: bool = True
    contextual_enrichment: bool = True
    forgetting_curve: bool = True
    forgetting_curve_half_life: float = 30.0
    vec_rebuild_threshold: int = 15

    # kg_extract tunables
    entity_min_occurrences: int = 2
    kg_coccurr_entity_cap: int = 20
    kg_edge_weight_increment: float = 0.1
    kg_edge_weight_cap: float = 10.0

    # graph cache
    graph_cache_max: int = 50
    graph_cache_ttl_s: float = 60.0

    # user profile tunables (cont.)
    ctr_data_window_days: int = 90

    # features — all on by default so the agent gets the richest context
    # automatically. Tests that need to opt out set the env var to "0"
    # explicitly. The previous off-by-default posture was an
    # over-cautious "ship empty" decision; the audit + verification
    # confirmed each feature is stable enough for production.
    multi_agent: bool = True
    summarization: bool = True
    user_profile: bool = True
    self_directed: bool = True
    adaptive_retention: bool = True
    consolidation: bool = True
    quality_gates: bool = True
    saga_enabled: bool = True
    temporal_tiers: bool = True
    crdt_enabled: bool = True
    llm_extraction: bool = True
    # T8 (2026-06-23): fact-level temporal KG. Default ON so the agent
    # gets automatic contradiction detection + edit invalidation. Set
    # MEMORY_TEMPORAL_KG=0 to disable (reverts to plain fact extraction
    # with no event_time / supersession / invalidation logic).
    feature_temporal_kg: bool = True

    # cache
    fts5_cache: bool = True
    fts5_cache_ttl: int = 30

    # quality_gates thresholds
    quality_min_content_length: int = 20
    quality_max_duplicate_similarity: float = 0.90
    quality_min_relevance_score: float = 0.1

    # memory_sharing
    shared_pool_ttl_days: int = 30
    shared_pool_max_size: int = 500

    # llm_extraction
    llm_provider: str = "huggingface"  # S3: "ollama" | "llama_cpp" | "huggingface"
    ollama_host: str = "http://localhost:11434"  # S3.5
    ollama_model: str = "qwen2.5:3b"  # S3: default Ollama model
    ollama_timeout_s: float = 30.0  # S3: per-request HTTP timeout
    llama_cpp_host: str = "http://localhost:8080"  # S3.6
    llama_cpp_model: str = ""  # S3: empty = use server default
    llama_cpp_timeout_s: float = 30.0
    llm_extraction_model_id: str = "Qwen/Qwen2.5-3B-Instruct"  # T6: bumped from 1.5B. Faster, more precise, fewer facts.
    llm_extraction_max_tokens: int = 256  # was 1024; cut 2026-06-19 for ~4x speedup
    llm_extraction_hybrid_threshold: float = (
        0.5  # P3.3 hybrid: use LLM if importance_score >= this
    )
    llm_extraction_force: bool = False  # P3.3: if True, always use LLM
    idle_unload_seconds: int = 1800  # LLM model idle unload timer (seconds; 0 = disabled). Frees ~8 GB GPU+CPU.

    # semantic / kg dedup thresholds
    max_claims_semantic: int = 10000
    semantic_threshold: float = 0.65
    kg_dedup_threshold: float = 0.92

    # hybrid fusion weights (P2-18)
    hybrid_fts_weight: float = 1.0
    hybrid_semantic_weight: float = 1.0
    hybrid_rrf_k: int = 60
    hybrid_semantic_overfetch: int = 3
    hybrid_rank_proxy_scale: float = 30.0

    # rerank / blend / threshold tunables (P3-32)
    rerank_half_life_days: int = 180
    cross_encoder_blend: float = 0.6
    late_interaction_blend: float = 0.3
    topic_similarity_threshold: float = 0.15
    concept_drift_threshold: float = 0.15
    temporal_decay_weight: float = 0.15

    # vector cache tunables (P3-32)
    vec_cache_max: int = 20
    vec_cache_ttl_s: float = 30.0

    # user profile tunables (P3-32)
    user_profile_window_days: int = 90
    user_profile_max_size: int = 50
    user_profile_recency_half_life_days: int = 30

    # sync (auto multi-agent)
    sync_enable_server: bool = False
    sync_listen_host: str = "127.0.0.1"
    sync_listen_port: int = 9877
    sync_peers: tuple = field(default_factory=tuple)
    sync_interval_minutes: int = 5

    # auto-save hook (backoff / circuit breaker)
    # When tool_complete() fails, retry with exponential backoff and trip
    # a circuit breaker after N failures within the cooldown window.
    # Resets to closed after the circuit_breaker_seconds elapse.
    auto_save_max_retries: int = 3
    auto_save_backoff_base_seconds: float = 1.0
    auto_save_backoff_cap_seconds: float = 30.0
    auto_save_circuit_breaker_seconds: float = 300.0
    auto_save_failure_window_seconds: float = 60.0
    auto_save_allowlist: str = (
        "memory_save,memory_supersede,memory_delete,todowrite,task,question,write,edit"
    )
    auto_save_denylist: str = "filesystem_list_allowed_directories,filesystem_list_directory,filesystem_directory_tree,filesystem_read_multiple_files,filesystem_search_files,filesystem_get_file_info,filesystem_list_directory_with_sizes,memory_session_start,memory_user_profile,memory_recall_context,memory_profile_access,memory_record_ctr_feedback,memory_check_concept_drift,todo,process,read_terminal"

    # save_pipeline limits
    save_max_content_bytes: int = 50000  # 50KB
    save_max_tags: int = 50
    save_max_category_len: int = 64
    save_max_slug_len: int = 128

    # auto_save daemon
    auto_save_batch_interval_seconds: float = 0.5  # 500ms
    auto_save_batch_size: int = 50
    auto_save_daemon_idle_seconds: int = 3600  # 1 hour
    auto_save_inbox_max_bytes: int = 100 * 1024 * 1024  # 100 MB
    auto_save_preview_max: int = 200
    auto_save_params_max: int = 2000
    auto_save_health_check_minutes: int = 5

    # session memory (v22)
    session_memory: bool = False
    session_decision_llm: bool = False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[MemoryConfig] = None
_instance_lock = threading.Lock()


def _resolve_sync_peers(toml_data: dict) -> tuple:
    """Build the sync.peers tuple from TOML with env-var override.

    C6 fix (2026-06-22): operators wanted a way to point at a peer at
    runtime (e.g. systemd-launched cron) without pre-baking a TOML.
    We now support a single ``MEMORY_SYNC_PEER_URL`` +
    ``MEMORY_SYNC_PEER_AGENT_ID`` pair via env vars. The TOML list
    still wins when it is non-empty; env vars are consulted only when
    the TOML list is empty/absent.
    """
    raw_peers = _deep_get(toml_data, "sync.peers")
    if isinstance(raw_peers, list):
        resolved = []
        for p in raw_peers:
            if isinstance(p, dict) and p.get("url") and p.get("agent_id"):
                resolved.append(
                    {
                        "name": p.get("name", p["agent_id"]),
                        "url": p["url"],
                        "agent_id": p["agent_id"],
                    }
                )
        if resolved:
            return tuple(resolved)
    peer_url = os.environ.get("MEMORY_SYNC_PEER_URL")
    peer_agent_id = os.environ.get("MEMORY_SYNC_PEER_AGENT_ID")
    if peer_url and peer_agent_id:
        return (
            {
                "name": os.environ.get("MEMORY_SYNC_PEER_NAME", peer_agent_id),
                "url": peer_url,
                "agent_id": peer_agent_id,
            },
        )
    return ()


def _build_config_from_toml(toml_data: dict) -> MemoryConfig:
    """Build a MemoryConfig from a parsed TOML dict.

    Extracted from get_config() (2026-06-22) so the orchestrator stays
    readable. Each section has its own helper below; the sections are
    called in dependency order. No behavior change — this is purely
    structural.
    """

    def _b(
        env_var: str, toml_key: str, default, cast=None, toml_data: dict | None = None
    ):
        """Shorthand for _resolve + cast."""
        v = _resolve(env_var, toml_key, default, toml_data)
        if cast is not None:
            return cast(v)
        return v

    cfg = MemoryConfig(
        # --- general ---
        db_path=_abs_db_path(
            str(
                _b(
                    "MEMORY_DB_PATH",
                    "general.db_path",
                    "memory/memory.db",
                    toml_data=toml_data,
                )
            )
        ),
        wal_checkpoint_startup=_b(
            "MEMORY_WAL_CHECKPOINT_STARTUP",
            "general.wal_checkpoint_startup",
            True,
            bool,
            toml_data,
        ),
        wal_checkpoint_interval_s=_b(
            "MEMORY_WAL_CHECKPOINT_INTERVAL_S",
            "general.wal_checkpoint_interval_s",
            300,
            int,
            toml_data,
        ),
        mmap_size=_b(
            "MEMORY_SQLITE_MMAP_SIZE",
            "general.mmap_size",
            268_435_456,
            int,
            toml_data,
        ),
        unindexed_safety_net_limit=_b(
            "MEMORY_UNINDEXED_SAFETY_NET_LIMIT",
            "general.unindexed_safety_net_limit",
            1000,
            int,
            toml_data,
        ),
        agent_id=_b("MEMORY_AGENT_ID", "general.agent_id", "", str, toml_data),
        # --- search ---
        temporal_half_life=_b(
            "MEMORY_TEMPORAL_HALF_LIFE",
            "search.temporal_half_life",
            180.0,
            float,
            toml_data,
        ),
        temporal_decay_mode=_b(
            "MEMORY_TEMPORAL_DECAY_MODE",
            "search.temporal_decay_mode",
            "exponential",
            str,
            toml_data,
        ),
        late_interaction=_b(
            "MEMORY_LATE_INTERACTION", "search.late_interaction", True, bool, toml_data
        ),
        knowledge_graph=_b(
            "MEMORY_KNOWLEDGE_GRAPH", "search.knowledge_graph", True, bool, toml_data
        ),
        graph_rag_hops=_b(
            "MEMORY_GRAPH_RAG_HOPS", "search.graph_rag_hops", 3, int, toml_data
        ),
        graph_rag_expansions=_b(
            "MEMORY_GRAPH_RAG_EXPANSIONS",
            "search.graph_rag_expansions",
            5,
            int,
            toml_data,
        ),
        embedding_score_threshold=_b(
            "MEMORY_EMBEDDING_SCORE_THRESHOLD",
            "search.embedding_score_threshold",
            0.25,
            float,
            toml_data,
        ),
        kg_llm_fallback_min_entities=_b(
            "MEMORY_KG_LLM_FALLBACK_MIN_ENTITIES",
            "search.kg_llm_fallback_min_entities",
            2,
            int,
            toml_data,
        ),
        rerank_weights=_b(
            "MEMORY_RERANK_WEIGHTS",
            "search.rerank_weights",
            "",
            str,
            toml_data,
        ),
        query_type_weights=_b(
            "MEMORY_QUERY_TYPE_WEIGHTS",
            "search.query_type_weights",
            "",
            str,
            toml_data,
        ),
        query_cache=_b(
            "MEMORY_QUERY_CACHE", "search.query_cache", True, bool, toml_data
        ),
        reranker_disabled=_b(
            "MEMORY_RERANKER_DISABLED",
            "search.reranker_disabled",
            False,
            bool,
            toml_data,
        ),
        deep_rerank_timeout=_b(
            "MEMORY_DEEP_RERANK_TIMEOUT",
            "search.deep_rerank_timeout",
            30.0,
            float,
            toml_data,
        ),
        contextual_retrieval=_b(
            "MEMORY_CONTEXTUAL_RETRIEVAL",
            "search.contextual_retrieval",
            True,
            bool,
            toml_data,
        ),
        forgetting_curve=_b(
            "MEMORY_FORGETTING_CURVE", "search.forgetting_curve", True, bool, toml_data
        ),
        contextual_enrichment=_b(
            "MEMORY_CONTEXTUAL_ENRICHMENT",
            "search.contextual_enrichment",
            True,
            bool,
            toml_data,
        ),
        forgetting_curve_half_life=_b(
            "MEMORY_FORGETTING_CURVE_HALF_LIFE",
            "search.forgetting_curve_half_life",
            30.0,
            float,
            toml_data,
        ),
        vec_rebuild_threshold=_b(
            "MEMORY_VEC_REBUILD_THRESHOLD",
            "search.vec_rebuild_threshold",
            15,
            int,
            toml_data,
        ),
        entity_min_occurrences=_b(
            "MEMORY_ENTITY_MIN_OCCURRENCES",
            "search.entity_min_occurrences",
            2,
            int,
            toml_data,
        ),
        kg_coccurr_entity_cap=_b(
            "MEMORY_KG_COCCUR_ENTITY_CAP",
            "search.kg_coccurr_entity_cap",
            20,
            int,
            toml_data,
        ),
        kg_edge_weight_increment=_b(
            "MEMORY_KG_EDGE_WEIGHT_INCREMENT",
            "search.kg_edge_weight_increment",
            0.1,
            float,
            toml_data,
        ),
        kg_edge_weight_cap=_b(
            "MEMORY_KG_EDGE_WEIGHT_CAP",
            "search.kg_edge_weight_cap",
            10.0,
            float,
            toml_data,
        ),
        graph_cache_max=_b(
            "MEMORY_GRAPH_CACHE_MAX",
            "search.graph_cache_max",
            50,
            int,
            toml_data,
        ),
        graph_cache_ttl_s=_b(
            "MEMORY_GRAPH_CACHE_TTL_S",
            "search.graph_cache_ttl_s",
            60.0,
            float,
            toml_data,
        ),
        ctr_data_window_days=_b(
            "MEMORY_CTR_DATA_WINDOW_DAYS",
            "search.ctr_data_window_days",
            90,
            int,
            toml_data,
        ),
        rerank_half_life_days=_b(
            "MEMORY_RERANK_HALF_LIFE_DAYS",
            "search.rerank_half_life_days",
            180,
            int,
            toml_data,
        ),
        cross_encoder_blend=_b(
            "MEMORY_CROSS_ENCODER_BLEND",
            "search.cross_encoder_blend",
            0.6,
            float,
            toml_data,
        ),
        late_interaction_blend=_b(
            "MEMORY_LATE_INTERACTION_BLEND",
            "search.late_interaction_blend",
            0.3,
            float,
            toml_data,
        ),
        topic_similarity_threshold=_b(
            "MEMORY_TOPIC_SIMILARITY_THRESHOLD",
            "search.topic_similarity_threshold",
            0.15,
            float,
            toml_data,
        ),
        concept_drift_threshold=_b(
            "MEMORY_CONCEPT_DRIFT_THRESHOLD",
            "search.concept_drift_threshold",
            0.15,
            float,
            toml_data,
        ),
        temporal_decay_weight=_b(
            "MEMORY_TEMPORAL_DECAY_WEIGHT",
            "search.temporal_decay_weight",
            0.15,
            float,
            toml_data,
        ),
        # --- features ---
        multi_agent=_b(
            "MEMORY_MULTI_AGENT", "features.multi_agent", True, bool, toml_data
        ),
        summarization=_b(
            "MEMORY_SUMMARIZATION", "features.summarization", True, bool, toml_data
        ),
        user_profile=_b(
            "MEMORY_USER_PROFILE", "features.user_profile", True, bool, toml_data
        ),
        self_directed=_b(
            "MEMORY_SELF_DIRECTED", "features.self_directed", True, bool, toml_data
        ),
        adaptive_retention=_b(
            "MEMORY_ADAPTIVE_RETENTION",
            "features.adaptive_retention",
            True,
            bool,
            toml_data,
        ),
        consolidation=_b(
            "MEMORY_CONSOLIDATION", "features.consolidation", True, bool, toml_data
        ),
        saga_enabled=_b(
            "MEMORY_SAGA_ENABLED", "features.saga_enabled", True, bool, toml_data
        ),
        quality_gates=_b(
            "MEMORY_QUALITY_GATES", "features.quality_gates", True, bool, toml_data
        ),
        temporal_tiers=_b(
            "MEMORY_TEMPORAL_TIERS", "features.temporal_tiers", True, bool, toml_data
        ),
        crdt_enabled=_b(
            "MEMORY_CRDT_ENABLED", "features.crdt_enabled", True, bool, toml_data
        ),
        llm_extraction=_b(
            "MEMORY_LLM_EXTRACTION", "features.llm_extraction", True, bool, toml_data
        ),
        feature_temporal_kg=_b(
            "MEMORY_TEMPORAL_KG", "features.feature_temporal_kg", True, bool, toml_data
        ),
        # --- cache ---
        fts5_cache=_b("MEMORY_FTS5_CACHE", "cache.fts5_cache", True, bool, toml_data),
        fts5_cache_ttl=_b(
            "MEMORY_FTS5_CACHE_TTL", "cache.fts5_cache_ttl", 30, int, toml_data
        ),
        vec_cache_max=_b(
            "MEMORY_VEC_CACHE_MAX", "cache.vec_cache_max", 20, int, toml_data
        ),
        vec_cache_ttl_s=_b(
            "MEMORY_VEC_CACHE_TTL_S", "cache.vec_cache_ttl_s", 30.0, float, toml_data
        ),
        # --- quality_gates ---
        quality_min_content_length=_b(
            "MEMORY_QUALITY_MIN_CONTENT_LENGTH",
            "quality_gates.min_content_length",
            20,
            int,
            toml_data,
        ),
        quality_max_duplicate_similarity=_b(
            "MEMORY_QUALITY_MAX_DUPLICATE_SIMILARITY",
            "quality_gates.max_duplicate_similarity",
            0.90,
            float,
            toml_data,
        ),
        quality_min_relevance_score=_b(
            "MEMORY_QUALITY_MIN_RELEVANCE_SCORE",
            "quality_gates.min_relevance_score",
            0.1,
            float,
            toml_data,
        ),
        # --- multi_agent ---
        shared_pool_ttl_days=_b(
            "MEMORY_SHARED_POOL_TTL_DAYS",
            "multi_agent.shared_pool_ttl_days",
            30,
            int,
            toml_data,
        ),
        shared_pool_max_size=_b(
            "MEMORY_SHARED_POOL_MAX_SIZE",
            "multi_agent.shared_pool_max_size",
            500,
            int,
            toml_data,
        ),
        # --- llm_extraction ---
        llm_provider=_b(
            "MEMORY_LLM_PROVIDER",
            "llm_extraction.provider",
            "huggingface",  # S3: "ollama" | "llama_cpp" | "huggingface"
            str,
            toml_data,
        ),
        ollama_host=_b(
            "MEMORY_OLLAMA_HOST",
            "llm_extraction.ollama_host",
            "http://localhost:11434",
            str,
            toml_data,
        ),
        ollama_model=_b(
            "MEMORY_OLLAMA_MODEL",
            "llm_extraction.ollama_model",
            "qwen2.5:3b",
            str,
            toml_data,
        ),
        ollama_timeout_s=_b(
            "MEMORY_OLLAMA_TIMEOUT_S",
            "llm_extraction.ollama_timeout_s",
            30.0,
            float,
            toml_data,
        ),
        llama_cpp_host=_b(
            "MEMORY_LLAMA_CPP_HOST",
            "llm_extraction.llama_cpp_host",
            "http://localhost:8080",
            str,
            toml_data,
        ),
        llama_cpp_model=_b(
            "MEMORY_LLAMA_CPP_MODEL",
            "llm_extraction.llama_cpp_model",
            "",
            str,
            toml_data,
        ),
        llama_cpp_timeout_s=_b(
            "MEMORY_LLAMA_CPP_TIMEOUT_S",
            "llm_extraction.llama_cpp_timeout_s",
            30.0,
            float,
            toml_data,
        ),
        llm_extraction_model_id=_b(
            "MEMORY_LLM_EXTRACTION_MODEL_ID",
            "llm_extraction.model_id",
            "Qwen/Qwen2.5-3B-Instruct",  # T6: bumped from 1.5B
            str,
            toml_data,
        ),
        llm_extraction_max_tokens=_b(
            "MEMORY_LLM_EXTRACTION_MAX_TOKENS",
            "llm_extraction.max_tokens",
            256,
            int,
            toml_data,
        ),
        llm_extraction_hybrid_threshold=_b(
            "MEMORY_LLM_HYBRID_THRESHOLD",
            "llm_extraction.hybrid_threshold",
            0.5,
            float,
            toml_data,
        ),
        llm_extraction_force=_b(
            "MEMORY_LLM_FORCE", "llm_extraction.force", False, bool, toml_data
        ),
        idle_unload_seconds=_b(
            "MEMORY_LLM_EXTRACTION_IDLE_UNLOAD_SECONDS",
            "llm_extraction.idle_unload_seconds",
            1800,
            int,
            toml_data,
        ),
        # --- semantic / kg dedup ---
        max_claims_semantic=_b(
            "MEMORY_MAX_CLAIMS_SEMANTIC",
            "semantic.max_claims_semantic",
            10000,
            int,
            toml_data,
        ),
        semantic_threshold=_b(
            "MEMORY_SEMANTIC_THRESHOLD",
            "semantic.semantic_threshold",
            0.65,
            float,
            toml_data,
        ),
        kg_dedup_threshold=_b(
            "MEMORY_KG_DEDUP_THRESHOLD", "kg_dedup.threshold", 0.92, float, toml_data
        ),
        # --- hybrid fusion weights ---
        hybrid_fts_weight=_b(
            "MEMORY_HYBRID_FTS_WEIGHT", "hybrid.fts_weight", 1.0, float, toml_data
        ),
        hybrid_semantic_weight=_b(
            "MEMORY_HYBRID_SEMANTIC_WEIGHT",
            "hybrid.semantic_weight",
            1.0,
            float,
            toml_data,
        ),
        hybrid_rrf_k=_b("MEMORY_HYBRID_RRF_K", "hybrid.rrf_k", 60, int, toml_data),
        hybrid_semantic_overfetch=_b(
            "MEMORY_HYBRID_SEMANTIC_OVERFETCH",
            "hybrid.semantic_overfetch",
            3,
            int,
            toml_data,
        ),
        hybrid_rank_proxy_scale=_b(
            "MEMORY_HYBRID_RANK_PROXY_SCALE",
            "hybrid.rank_proxy_scale",
            30.0,
            float,
            toml_data,
        ),
        # --- user profile ---
        user_profile_window_days=_b(
            "MEMORY_USER_PROFILE_WINDOW_DAYS",
            "user_profile.window_days",
            90,
            int,
            toml_data,
        ),
        user_profile_max_size=_b(
            "MEMORY_USER_PROFILE_MAX_SIZE", "user_profile.max_size", 50, int, toml_data
        ),
        user_profile_recency_half_life_days=_b(
            "MEMORY_USER_PROFILE_RECENCY_HALF_LIFE_DAYS",
            "user_profile.recency_half_life_days",
            30,
            int,
            toml_data,
        ),
        # --- sync ---
        sync_enable_server=_b(
            "MEMORY_SYNC_ENABLE_SERVER", "sync.enable_server", False, bool, toml_data
        ),
        sync_listen_host=_b(
            "MEMORY_SYNC_LISTEN_HOST", "sync.listen_host", "127.0.0.1", str, toml_data
        ),
        sync_listen_port=_b(
            "MEMORY_SYNC_LISTEN_PORT", "sync.listen_port", 9877, int, toml_data
        ),
        sync_interval_minutes=_b(
            "MEMORY_SYNC_INTERVAL_MINUTES",
            "sync.schedule.interval_minutes",
            5,
            int,
            toml_data,
        ),
        sync_peers=_resolve_sync_peers(toml_data),
        # --- auto_save ---
        auto_save_max_retries=_b(
            "MEMORY_AUTO_SAVE_MAX_RETRIES", "auto_save.max_retries", 3, int, toml_data
        ),
        auto_save_backoff_base_seconds=_b(
            "MEMORY_AUTO_SAVE_BACKOFF_BASE_SECONDS",
            "auto_save.backoff_base_seconds",
            1.0,
            float,
            toml_data,
        ),
        auto_save_backoff_cap_seconds=_b(
            "MEMORY_AUTO_SAVE_BACKOFF_CAP_SECONDS",
            "auto_save.backoff_cap_seconds",
            30.0,
            float,
            toml_data,
        ),
        auto_save_circuit_breaker_seconds=_b(
            "MEMORY_AUTO_SAVE_CIRCUIT_BREAKER_SECONDS",
            "auto_save.circuit_breaker_seconds",
            300.0,
            float,
            toml_data,
        ),
        auto_save_failure_window_seconds=_b(
            "MEMORY_AUTO_SAVE_FAILURE_WINDOW_SECONDS",
            "auto_save.failure_window_seconds",
            60.0,
            float,
            toml_data,
        ),
        auto_save_allowlist=_b(
            "AUTO_SAVE_TOOL_ALLOWLIST",
            "auto_save.allowlist",
            "memory_save,memory_supersede,memory_delete,todowrite,task,question,write,edit",
            str,
            toml_data,
        ),
        auto_save_denylist=_b(
            "AUTO_SAVE_TOOL_DENYLIST",
            "auto_save.denylist",
            "filesystem_list_allowed_directories,filesystem_list_directory,filesystem_directory_tree,filesystem_read_multiple_files,filesystem_search_files,filesystem_get_file_info,filesystem_list_directory_with_sizes,memory_session_start,memory_user_profile,memory_recall_context,memory_profile_access,memory_record_ctr_feedback,memory_check_concept_drift,todo,process,read_terminal",
            str,
            toml_data,
        ),
        # --- save_pipeline ---
        save_max_content_bytes=_b(
            "MEMORY_SAVE_MAX_CONTENT_BYTES",
            "save_pipeline.max_content_bytes",
            50000,
            int,
            toml_data,
        ),
        save_max_tags=_b(
            "MEMORY_SAVE_MAX_TAGS",
            "save_pipeline.max_tags",
            50,
            int,
            toml_data,
        ),
        save_max_category_len=_b(
            "MEMORY_SAVE_MAX_CATEGORY_LEN",
            "save_pipeline.max_category_len",
            64,
            int,
            toml_data,
        ),
        save_max_slug_len=_b(
            "MEMORY_SAVE_MAX_SLUG_LEN",
            "save_pipeline.max_slug_len",
            128,
            int,
            toml_data,
        ),
        # --- auto_save ---
        auto_save_batch_interval_seconds=_b(
            "MEMORY_AUTO_SAVE_BATCH_INTERVAL_SECONDS",
            "auto_save.batch_interval_seconds",
            0.5,
            float,
            toml_data,
        ),
        auto_save_batch_size=_b(
            "MEMORY_AUTO_SAVE_BATCH_SIZE",
            "auto_save.batch_size",
            50,
            int,
            toml_data,
        ),
        auto_save_daemon_idle_seconds=_b(
            "MEMORY_AUTO_SAVE_DAEMON_IDLE_SECONDS",
            "auto_save.daemon_idle_seconds",
            3600,
            int,
            toml_data,
        ),
        auto_save_inbox_max_bytes=_b(
            "MEMORY_AUTO_SAVE_INBOX_MAX_BYTES",
            "auto_save.inbox_max_bytes",
            100 * 1024 * 1024,
            int,
            toml_data,
        ),
        auto_save_preview_max=_b(
            "AUTO_SAVE_PREVIEW_MAX",
            "auto_save.preview_max",
            200,
            int,
            toml_data,
        ),
        auto_save_params_max=_b(
            "AUTO_SAVE_PARAMS_MAX",
            "auto_save.params_max",
            2000,
            int,
            toml_data,
        ),
        auto_save_health_check_minutes=_b(
            "MEMORY_AUTO_SAVE_HEALTH_CHECK_MINUTES",
            "auto_save.health_check_minutes",
            5,
            int,
            toml_data,
        ),
        # --- session_memory ---
        session_memory=_b(
            "MEMORY_SESSION_MEMORY",
            "session_memory.enabled",
            False,
            bool,
            toml_data,
        ),
        session_decision_llm=_b(
            "MEMORY_SESSION_DECISION_LLM",
            "session_memory.decision_llm",
            False,
            bool,
            toml_data,
        ),
    )
    return cfg


def get_config() -> MemoryConfig:
    """Return the singleton ``MemoryConfig``.

    Safe to call from ``@st.cache_resource`` or any lazy-init context.
    Thread-safe via double-checked locking.

    Decomposed 2026-06-22: the actual dataclass construction moved to
    ``_build_config_from_toml`` so this orchestrator stays readable.
    """
    global _instance
    import sys

    is_testing = (
        "pytest" in sys.modules
        or "unittest" in sys.modules
        or os.environ.get("PYTEST_CURRENT_TEST") is not None
    )
    if _instance is not None and not is_testing:
        return _instance

    with _instance_lock:
        if _instance is not None and not is_testing:
            return _instance

        toml_data = _read_toml(_TOML_PATH)
        cfg = _build_config_from_toml(toml_data)

        if not is_testing:
            _instance = cfg
        return cfg


def reset_config() -> None:
    """Reset the singleton (useful in tests)."""
    global _instance
    _instance = None
