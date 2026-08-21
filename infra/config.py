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

import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Integrity-critical flag monitoring (OWASP A05-002)
# ---------------------------------------------------------------------------
# When one of these ``MEMORY_*`` env vars overrides a flag that underpins
# crash-consistency / data integrity, we MUST surface a WARNING at startup
# so an operator knows the integrity guarantee is being downgraded. The
# warning is emitted only when the env override actively DISABLES the flag
# (resolved value is explicitly False), which is the dangerous case.
logger = logging.getLogger(__name__)

_INTEGRITY_CRITICAL_FLAGS: frozenset[str] = frozenset(
    {
        "MEMORY_SAGA_ENABLED",
        "MEMORY_CRDT_ENABLED",
        "MEMORY_WRITE_JOURNAL_ENABLED",
        "MEMORY_QUALITY_GATES",
    }
)

# ---------------------------------------------------------------------------
# TOML parsing — tomllib (3.11+) with tomli fallback
# ---------------------------------------------------------------------------

tomllib: ModuleType | None
try:
    import tomllib as _tomllib_stdlib
    tomllib = _tomllib_stdlib
except ModuleNotFoundError:
    try:
        import tomli as _tomllib_fallback
        tomllib = _tomllib_fallback
    except ModuleNotFoundError:
        tomllib = None

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).resolve().parent.parent


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


_TOML_CACHE: dict[str, tuple[float, Dict[str, Any]]] = {}


def _read_toml(path: Path) -> Dict[str, Any]:
    """Return parsed TOML dict, or empty dict if file missing / lib absent."""
    if not path.exists() or tomllib is None:
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _TOML_CACHE.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
            parsed = data if isinstance(data, dict) else {}
            _TOML_CACHE[str(path)] = (mtime, parsed)
            return parsed
    except Exception:
        return {}


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
            resolved: Any
            if parser is not None:
                resolved = parser(env_val)
            elif isinstance(default, bool):
                resolved = _parse_bool(env_val)
            elif isinstance(default, int):
                resolved = _parse_int(env_val)
            elif isinstance(default, float):
                resolved = _parse_float(env_val)
            else:
                resolved = env_val
            # OWASP A05-002: an env override that DISABLES an
            # integrity-critical flag silently weakens crash-consistency /
            # data-integrity guarantees. Surface it loudly.
            if env_key in _INTEGRITY_CRITICAL_FLAGS and resolved is False:
                sys.stderr.write(
                    "warning: SECURITY: integrity-critical flag %s overridden via env to "
                    "disabled — crash-consistency / integrity guarantees are being "
                    "downgraded. This bypasses the saga write path, CRDT merge "
                    "safety, the CQRS write journal, and/or quality gates.\n"
                    % (env_key,)
                )
            return resolved
        except (ValueError, TypeError) as e:
            # 2026-06-22 (C7 fix): warn directly to stderr so the message
            # is always visible even when no logging handler is configured
            # (e.g. in a bare subprocess or freshly-imported module).
            sys.stderr.write(
                "warning: %s=%r could not be parsed as %s; falling back to default. Error: %s\n"
                % (env_key, env_val, type(default).__name__, e)
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
            # Allow int → float, bool → int, and None → dict promotions.
            if isinstance(default, float) and isinstance(toml_val, int):
                pass
            elif isinstance(default, int) and isinstance(toml_val, bool):
                pass
            elif default is None and isinstance(toml_val, dict):
                pass
            else:
                sys.stderr.write(
                    "warning: %s=%r has type %s but the dataclass field expects %s; "
                    "falling back to the TOML value as-is.\n"
                    % (dotted_path, toml_val, type(toml_val).__name__, type(default).__name__)
                )
        return toml_val

    return default


# 2026-06-26: the original import-time log only printed the raw env var
# (e.g. "MEMORY_LLM_EXTRACTION=None"), which misled operators into thinking
# LLM extraction was disabled — when in fact the TOML config was the source
# of truth. This function shows all three: env var, TOML value, and the
# resolved effective value.
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
    sys.stderr.write(
        "IMPORT config.py: MEMORY_LLM_EXTRACTION env=%r "
        "toml[features.llm_extraction]=%r effective=%r\n"
        % (raw_env, toml_val, effective)
    )


_log_llm_extraction_resolution()


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneralDBConfig:
    db_path: str = "memory/memory.db"
    wal_checkpoint_startup: bool = True
    wal_checkpoint_interval_s: int = 300
    mmap_size: int = 268_435_456
    unindexed_safety_net_limit: int = 1000
    db_pool_size: int = 24
    agent_id: str = ""


@dataclass(frozen=True)
class SearchConfig:
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
    query_cache_max: int = 128
    search_parallel_enabled: bool = True
    reranker_disabled: bool = False
    llm_allow_remote_code: bool = False
    deep_rerank_timeout: float = 30.0
    contextual_retrieval: bool = True
    contextual_enrichment: bool = True
    forgetting_curve: bool = True
    forgetting_curve_half_life: float = 30.0
    vec_rebuild_threshold: int = 15
    vec_rebuild_adaptive: bool = True
    ctr_data_window_days: int = 90
    exploration_mode: str = "off"
    search_compute_budget_ms: float = 200.0


@dataclass(frozen=True)
class KGConfig:
    entity_min_occurrences: int = 2
    kg_coccurr_entity_cap: int = 20
    kg_edge_weight_increment: float = 0.1
    kg_edge_weight_cap: float = 10.0
    ner_spacy_enabled: bool = False


@dataclass(frozen=True)
class GraphCacheConfig:
    graph_cache_max: int = 50
    graph_cache_ttl_s: float = 60.0


@dataclass(frozen=True)
class WritePipelineConfig:
    write_journal: bool = False
    write_journal_fallback_sync: bool = False
    quality_gates: bool = True
    saga_enabled: bool = True
    defer_expensive: bool = True
    save_max_content_bytes: int = 50000  # 50KB
    save_max_tags: int = 50
    save_max_category_len: int = 64
    save_max_slug_len: int = 128


@dataclass(frozen=True)
class EmbeddingConfig:
    backend: str = "auto"
    model_id: str = "Potion-8M"
    model_revision: str = "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a"
    idle_unload_seconds: int = 600


def _validate_model_revision(revision: str) -> None:
    """Ensure model_revision is either empty or a 40-char lowercase hex SHA."""
    if not revision:
        return
    if not (len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)):
        raise ValueError(
            f"model_revision must be a 40-char lowercase hex SHA or empty, got {revision!r}"
        )


@dataclass(frozen=True)
class AutoSaveConfig:
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 300.0
    circuit_breaker_seconds: float = 300.0
    failure_window_seconds: float = 60.0
    batch_interval_seconds: float = 5.0
    batch_size: int = 50
    daemon_idle_seconds: int = 300
    inbox_max_bytes: int = 500_000
    preview_max: int = 200
    params_max: int = 2000
    health_check_minutes: int = 15
    allowlist: str = (
        "memory_save,memory_supersede,memory_delete,todowrite,task,question,write,edit"
    )
    denylist: str = (
        "filesystem_list_allowed_directories,filesystem_list_directory,"
        "filesystem_directory_tree,filesystem_read_multiple_files,"
        "filesystem_search_files,filesystem_get_file_info,"
        "filesystem_list_directory_with_sizes,memory_session_start,"
        "memory_user_profile,memory_recall,memory_profile_access,"
        "memory_record_ctr_feedback,memory_check_concept_drift,todo,process,"
        "read_terminal"
    )
    keyword_routing: bool = True
    always_sessions: bool = False


@dataclass(frozen=True)
class HealthCheckConfig:
    vec_index_drift_threshold: int = 50
    disk_pct_used_threshold: int = 95


@dataclass(frozen=True)
class SyncConfig:
    enable_server: bool = False
    listen_host: str = "127.0.0.1"
    listen_port: int = 9877
    peers: tuple = field(default_factory=tuple)
    interval_minutes: int = 5


@dataclass(frozen=True)
class APIConfig:
    enable_server: bool = False
    listen_host: str = "127.0.0.1"
    listen_port: int = 9878
    api_token: str = ""
    insecure_loopback: bool = False
    dashboard_address: str = "127.0.0.1"


@dataclass(frozen=True)
class QualityGatesConfig:
    min_content_length: int = 20
    max_duplicate_similarity: float = 0.90
    min_relevance_score: float = 0.30


@dataclass(frozen=True)
class MemorySharingConfig:
    shared_pool_ttl_days: int = 30
    shared_pool_max_size: int = 1000


@dataclass(frozen=True)
class CacheConfig:
    fts5_cache: bool = True
    fts5_cache_ttl: int = 30
    vec_cache_max: int = 500
    vec_cache_ttl_s: float = 300.0


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "none"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    ollama_timeout_s: float = 30.0
    llama_cpp_host: str = "http://localhost:8080"
    llama_cpp_model: str = ""
    llama_cpp_timeout_s: float = 30.0
    openai_compatible_host: str = "http://127.0.0.1:1234"
    openai_compatible_model: str = "qwen/qwen3.5-9b"
    openai_compatible_timeout_s: float = 120.0
    extraction_model_id: str = "Qwen/Qwen2.5-3B-Instruct"
    extraction_max_tokens: int = 256
    extraction_hybrid_threshold: float = 0.5
    allow_remote_code: bool = False
    extraction_force: bool = False


@dataclass(frozen=True)
class HybridSearchConfig:
    fts_weight: float = 0.5
    semantic_weight: float = 0.3
    rrf_k: int = 60
    semantic_overfetch: int = 50
    rank_proxy_scale: float = 30.0


@dataclass(frozen=True)
class RerankConfig:
    half_life_days: float = 180.0
    cross_encoder_blend: float = 0.6
    late_interaction_blend: float = 0.3
    topic_similarity_threshold: float = 0.15
    concept_drift_threshold: float = 0.15
    temporal_decay_weight: float = 0.15
    ce_blend: float = 0.85
    ce_chunk_blend: float = 0.7
    ce_deep_enabled: bool = False


@dataclass(frozen=True)
class FeatureFlagsConfig:
    write_journal: bool = False
    write_journal_fallback_sync: bool = False
    multi_agent: bool = True
    summarization: bool = True
    user_profile: bool = True
    self_directed: bool = True
    adaptive_retention: bool = True
    neural_forget_mode: str = "formula"
    neural_forget_weights: str = ""
    temporal_ssm_enabled: bool = False
    temporal_ssm_weights: str = ""
    consolidation: bool = True
    saga_enabled: bool = True
    quality_gates: bool = True
    temporal_tiers: bool = True
    crdt_enabled: bool = True
    legacy_note_crdt: bool = False
    llm_extraction: bool = True
    feature_temporal_kg: bool = True
    feature_temporal_kg_llm: bool = True
    temporal_kg_llm_tier: str = "light"
    feature_belief_layer: bool = True
    self_editing: bool = True
    knowledge_compilation: bool = True
    graph_centrality_boost: bool = True
    graph_communities: bool = True
    graph_evolution_tracking: bool = True
    ner_spacy_enabled: bool = False
    session_memory: bool = False
    session_decision_llm: bool = False
    session_cross_entity_boost: bool = True


@dataclass(frozen=True)
class UserProfileConfig:
    ctr_data_window_days: int = 90
    exploration_mode: str = "off"
    window_days: int = 90
    max_size: int = 50
    recency_half_life_days: int = 30


@dataclass(frozen=True)
class RecallConfig:
    max_tokens: int = 800
    tier1_hot_days: int = 7
    tier_fallback_threshold: int = 5


@dataclass(frozen=True)
class SemanticKGConfig:
    max_claims_semantic: int = 10000
    semantic_threshold: float = 0.65
    kg_dedup_threshold: float = 0.92


# ---------------------------------------------------------------------------
# MemoryConfig (nested)
# ---------------------------------------------------------------------------


@dataclass()  # NOT frozen — immutability enforced by custom __setattr__
class MemoryConfig:
    """Immutable, validated configuration — logically grouped into sub-configs.

    All TOML keys remain unchanged. ``[search] temporal_half_life`` still
    maps to ``cfg.search.temporal_half_life``. The flat dataclass is
    fully replaced; callers should migrate to nested access.
    """
    general: GeneralDBConfig = field(default_factory=GeneralDBConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    kg: KGConfig = field(default_factory=KGConfig)
    graph_cache: GraphCacheConfig = field(default_factory=GraphCacheConfig)
    write: WritePipelineConfig = field(default_factory=WritePipelineConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    auto_save: AutoSaveConfig = field(default_factory=AutoSaveConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    api: APIConfig = field(default_factory=APIConfig)
    quality_gates: QualityGatesConfig = field(default_factory=QualityGatesConfig)
    sharing: MemorySharingConfig = field(default_factory=MemorySharingConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    hybrid: HybridSearchConfig = field(default_factory=HybridSearchConfig)
    rerank: RerankConfig = field(default_factory=RerankConfig)
    features: FeatureFlagsConfig = field(default_factory=FeatureFlagsConfig)
    user_profile: UserProfileConfig = field(default_factory=UserProfileConfig)
    recall: RecallConfig = field(default_factory=RecallConfig)
    semantic_kg: SemanticKGConfig = field(default_factory=SemanticKGConfig)
    rate_limits: dict | None = None
    health_check: HealthCheckConfig = field(default_factory=HealthCheckConfig)

    def __init__(self, **kwargs: Any) -> None:
        """Accept legacy flat kwargs and route them to the correct nested config."""
        import dataclasses

        _FEATURE_FLAGS = frozenset({
            "write_journal", "write_journal_fallback_sync", "multi_agent", "summarization", "user_profile",
            "self_directed", "adaptive_retention", "neural_forget_mode",
            "neural_forget_weights", "temporal_ssm_enabled", "temporal_ssm_weights",
            "consolidation", "saga_enabled", "quality_gates", "temporal_tiers",
            "crdt_enabled", "legacy_note_crdt", "llm_extraction",
            "feature_temporal_kg", "feature_temporal_kg_llm", "temporal_kg_llm_tier",
            "feature_belief_layer", "self_editing", "knowledge_compilation",
            "graph_centrality_boost", "graph_communities", "graph_evolution_tracking",
            "ner_spacy_enabled", "session_memory", "session_decision_llm",
            "defer_expensive",
        })
        _FLAT_TO_NESTED: dict[str, tuple[str, str]] = {
            "temporal_half_life": ("search", "temporal_half_life"),
            "temporal_decay_mode": ("search", "temporal_decay_mode"),
            "late_interaction": ("search", "late_interaction"),
            "knowledge_graph": ("search", "knowledge_graph"),
            "graph_rag_hops": ("search", "graph_rag_hops"),
            "graph_rag_expansions": ("search", "graph_rag_expansions"),
            "embedding_score_threshold": ("search", "embedding_score_threshold"),
            "kg_llm_fallback_min_entities": ("search", "kg_llm_fallback_min_entities"),
            "rerank_weights": ("search", "rerank_weights"),
            "query_type_weights": ("search", "query_type_weights"),
            "query_cache": ("search", "query_cache"),
            "query_cache_max": ("search", "query_cache_max"),
            "forgetting_curve_half_life": ("search", "forgetting_curve_half_life"),
            "write_journal": ("write", "write_journal"),
            "quality_gates": ("write", "quality_gates"),
            "saga_enabled": ("write", "saga_enabled"),
            "defer_expensive": ("write", "defer_expensive"),
            "save_max_content_bytes": ("write", "save_max_content_bytes"),
            "save_max_tags": ("write", "save_max_tags"),
            "save_max_category_len": ("write", "save_max_category_len"),
            "save_max_slug_len": ("write", "save_max_slug_len"),
            "embedding_backend": ("embedding", "backend"),
            "embedding_model_id": ("embedding", "model_id"),
            "embedding_model_revision": ("embedding", "model_revision"),
            "sync_enabled": ("sync", "enable_server"),
            "sync_token": ("sync", "token"),
            "sync_hmac_secret": ("sync", "hmac_secret"),
            "sync_max_attempts": ("sync", "max_attempts"),
        }

        _SECTION_CLS: dict[str, type] = {
            "general": GeneralDBConfig,
            "search": SearchConfig,
            "kg": KGConfig,
            "graph_cache": GraphCacheConfig,
            "write": WritePipelineConfig,
            "embedding": EmbeddingConfig,
            "auto_save": AutoSaveConfig,
            "sync": SyncConfig,
            "api": APIConfig,
            "quality_gates": QualityGatesConfig,
            "sharing": MemorySharingConfig,
            "cache": CacheConfig,
            "llm": LLMConfig,
            "hybrid": HybridSearchConfig,
            "rerank": RerankConfig,
            "features": FeatureFlagsConfig,
            "user_profile": UserProfileConfig,
            "recall": RecallConfig,
            "semantic_kg": SemanticKGConfig,
            "health_check": HealthCheckConfig,
        }
        _NESTED_SECTION_NAMES = frozenset(list(_SECTION_CLS.keys()) + ["rate_limits"])
        section_overrides: dict[str, dict[str, Any]] = {}
        feature_overrides: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in _FEATURE_FLAGS:
                feature_overrides[key] = value
            elif key in _FLAT_TO_NESTED:
                section, field = _FLAT_TO_NESTED[key]
                section_overrides.setdefault(section, {})[field] = value
            elif key in _NESTED_SECTION_NAMES:
                section_overrides[key] = value
            else:
                raise TypeError(
                    f"MemoryConfig.__init__() got an unexpected keyword argument {key!r}"
                )

        _defaults: dict[str, Any] = {
            name: cls()
            for name, cls in _SECTION_CLS.items()
            if name != "rate_limits"
        }
        _defaults["rate_limits"] = None

        if feature_overrides:
            _defaults["features"] = dataclasses.replace(
                _defaults["features"], **feature_overrides
            )
        for section, fields in section_overrides.items():
            if dataclasses.is_dataclass(fields) and not isinstance(fields, type):
                _defaults[section] = fields
            elif section == "rate_limits" or not isinstance(fields, dict):
                _defaults[section] = fields
            else:
                _defaults[section] = _SECTION_CLS[section](**fields)

        for key, value in _defaults.items():
            object.__setattr__(self, key, value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Cannot set attribute on frozen MemoryConfig")

    def __post_init__(self) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        """Allow legacy flat access: cfg.temporal_half_life → cfg.search.temporal_half_life.

        ``features`` is checked first because its boolean flags (e.g.
        ``quality_gates``, ``saga_enabled``) share names with top-level
        nested config objects — checking features first ensures the
        boolean flag wins over the sub-config reference for all
        ``make_lazy_getattr``-based module-level constants.

        Also handles legacy prefixed names from the pre-nesting flat
        dataclass:
        - ``quality_min_content_length`` → ``quality_gates.min_content_length``
        - ``quality_max_duplicate_similarity`` → ``quality_gates.max_duplicate_similarity``
        - ``quality_min_relevance_score`` → ``quality_gates.min_relevance_score``
        - ``user_profile_window_days`` → ``user_profile.window_days``
        - ``user_profile_max_size`` → ``user_profile.max_size``
        - ``user_profile_recency_half_life_days`` → ``user_profile.recency_half_life_days``
        - ``shared_pool_ttl_days`` → ``sharing.shared_pool_ttl_days``
        - ``shared_pool_max_size`` → ``sharing.shared_pool_max_size``
        """
        # Check features first to resolve boolean flags that share names
        # with top-level nested config sub-objects (quality_gates, saga_enabled…)
        if hasattr(self.features, name):
            return getattr(self.features, name)
        # Legacy prefixed aliases for fields that moved into sub-configs
        _prefix_aliases = {
            "quality_": self.quality_gates,
            "user_profile_": self.user_profile,
        }
        for prefix, sub in _prefix_aliases.items():
            if name.startswith(prefix):
                nested_name = name[len(prefix):]
                if hasattr(sub, nested_name):
                    return getattr(sub, nested_name)
        # Composed legacy names (underscore_joined section_field → section.field)
        _composed_aliases = {
            "sync_peers": lambda self: self.sync.peers,
            "sync_enable_server": lambda self: self.sync.enable_server,
            "sync_listen_host": lambda self: self.sync.listen_host,
            "sync_listen_port": lambda self: self.sync.listen_port,
            "sync_interval_minutes": lambda self: self.sync.interval_minutes,
            "api_enable_server": lambda self: self.api.enable_server,
            "api_listen_host": lambda self: self.api.listen_host,
            "api_listen_port": lambda self: self.api.listen_port,
            "api_insecure_loopback": lambda self: self.api.insecure_loopback,
            "auto_save_max_retries": lambda self: self.auto_save.max_retries,
            "auto_save_backoff_base_seconds": lambda self: self.auto_save.backoff_base_seconds,
            "auto_save_backoff_cap_seconds": lambda self: self.auto_save.backoff_cap_seconds,
            "auto_save_circuit_breaker_seconds": lambda self: self.auto_save.circuit_breaker_seconds,
            "auto_save_failure_window_seconds": lambda self: self.auto_save.failure_window_seconds,
            "auto_save_batch_interval_seconds": lambda self: self.auto_save.batch_interval_seconds,
            "auto_save_batch_size": lambda self: self.auto_save.batch_size,
            "auto_save_daemon_idle_seconds": lambda self: self.auto_save.daemon_idle_seconds,
            "auto_save_inbox_max_bytes": lambda self: self.auto_save.inbox_max_bytes,
            "auto_save_preview_max": lambda self: self.auto_save.preview_max,
            "auto_save_params_max": lambda self: self.auto_save.params_max,
            "auto_save_health_check_minutes": lambda self: self.auto_save.health_check_minutes,
            "auto_save_keyword_routing": lambda self: self.auto_save.keyword_routing,
            "auto_save_always_sessions": lambda self: self.auto_save.always_sessions,
            "embedding_backend": lambda self: self.embedding.backend,
            "embedding_model_id": lambda self: self.embedding.model_id,
            "embedding_model_revision": lambda self: self.embedding.model_revision,
            "embedding_idle_unload_seconds": lambda self: self.embedding.idle_unload_seconds,
        }
        if name in _composed_aliases:
            return _composed_aliases[name](self)
        for sub in (
            self.general, self.search, self.kg, self.graph_cache, self.write,
            self.embedding, self.auto_save, self.sync, self.api, self.quality_gates,
            self.sharing, self.cache, self.llm, self.hybrid, self.rerank,
            self.user_profile, self.recall, self.semantic_kg,
            self.rate_limits, self.health_check,
        ):
            if hasattr(sub, name):
                return getattr(sub, name)
        raise AttributeError(f"MemoryConfig has no attribute '{name}'")


DECISION_CATEGORIES = frozenset({"decisions", "lessons", "projects", "architecture"})


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


def _apply_scope_overrides(toml_data: dict) -> dict:
    """Apply scope-based overrides to TOML data before config construction.

    When ``MEMORY_SCOPE=production`` (or ``scope.name = \"production\"`` in
    memory.toml), heavy default-on features are downgraded to keep latency
    and resource usage in check.  Env vars still win — an operator can
    re-enable any flag explicitly.
    """
    scope_name = os.environ.get("MEMORY_SCOPE") or _deep_get(toml_data, "scope.name") or "development"
    if scope_name != "production":
        return toml_data

    # Production-safe defaults: disable the heaviest optional features.
    # Core search (FTS5 BM25 + KG facts + basic reranking) stays enabled.
    # Env vars still take precedence — re-enable anything explicitly.
    prod_defaults = {
        "features.llm_extraction": False,
        "features.ner_spacy_enabled": False,
        "features.feature_temporal_kg_llm": False,
        "features.graph_centrality_boost": False,
        "features.graph_communities": False,
        "features.graph_evolution_tracking": False,
        "features.knowledge_compilation": False,
        "features.user_profile": False,
        "features.adaptive_retention": False,
        "features.consolidation": False,
        "features.summarization": False,
        "search.contextual_retrieval": False,
        "search.contextual_enrichment": False,
        "search.deep_rerank_timeout": 0,
        "search.ce_deep_enabled": False,
        "search.colbert_enabled": False,
        "search.splade_enabled": False,
        "search.answer_rerank_enabled": False,
        "search.ctr_weight_learning": False,
    }

    for key, value in prod_defaults.items():
        parts = key.split(".", 1)
        if len(parts) == 2:
            section, field = parts
            if section not in toml_data:
                toml_data[section] = {}
            # Only set if not already explicitly set in TOML
            if field not in toml_data[section]:
                toml_data[section][field] = value

    return toml_data


def _build_config_from_toml(toml_data: dict) -> MemoryConfig:
    """Build a MemoryConfig from a parsed TOML dict.

    Each TOML section maps to a nested frozen dataclass. The flat keyword-
    argument form is replaced by per-section constructors. No behavior
    change — same defaults, same env-var / TOML precedence.
    """
    toml_data = _apply_scope_overrides(toml_data)

    def _b(
        env_var: str, toml_key: str, default, cast=None, toml_data: dict | None = None
    ):
        """Shorthand for _resolve + cast."""
        v = _resolve(env_var, toml_key, default, toml_data)
        if cast is not None:
            return cast(v)
        return v

    def _abs(v):
        return _abs_db_path(str(v))

    # ---- general ----
    general = GeneralDBConfig(
        db_path=_abs(
            _b("MEMORY_DB_PATH", "general.db_path", "memory/memory.db", toml_data=toml_data)
        ),
        wal_checkpoint_startup=_b(
            "MEMORY_WAL_CHECKPOINT_STARTUP", "general.wal_checkpoint_startup", True, bool, toml_data
        ),
        wal_checkpoint_interval_s=_b(
            "MEMORY_WAL_CHECKPOINT_INTERVAL_S", "general.wal_checkpoint_interval_s", 300, int, toml_data
        ),
        mmap_size=_b(
            "MEMORY_SQLITE_MMAP_SIZE", "general.mmap_size", 268_435_456, int, toml_data
        ),
        unindexed_safety_net_limit=_b(
            "MEMORY_UNINDEXED_SAFETY_NET_LIMIT", "general.unindexed_safety_net_limit", 1000, int, toml_data
        ),
        db_pool_size=_b(
            "MEMORY_DB_POOL_SIZE", "general.db_pool_size", 24, int, toml_data
        ),
        agent_id=_b("MEMORY_AGENT_ID", "general.agent_id", "", str, toml_data),
    )

    # ---- search ----
    search = SearchConfig(
        temporal_half_life=_b(
            "MEMORY_TEMPORAL_HALF_LIFE", "search.temporal_half_life", 180.0, float, toml_data
        ),
        temporal_decay_mode=_b(
            "MEMORY_TEMPORAL_DECAY_MODE", "search.temporal_decay_mode", "exponential", str, toml_data
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
            "MEMORY_GRAPH_RAG_EXPANSIONS", "search.graph_rag_expansions", 5, int, toml_data
        ),
        embedding_score_threshold=_b(
            "MEMORY_EMBEDDING_SCORE_THRESHOLD", "search.embedding_score_threshold", 0.25, float, toml_data
        ),
        kg_llm_fallback_min_entities=_b(
            "MEMORY_KG_LLM_FALLBACK_MIN_ENTITIES", "search.kg_llm_fallback_min_entities", 2, int, toml_data
        ),
        rerank_weights=_b(
            "MEMORY_RERANK_WEIGHTS", "search.rerank_weights", "", str, toml_data
        ),
        query_type_weights=_b(
            "MEMORY_QUERY_TYPE_WEIGHTS", "search.query_type_weights", "", str, toml_data
        ),
        query_cache=_b(
            "MEMORY_QUERY_CACHE", "search.query_cache", True, bool, toml_data
        ),
        query_cache_max=_b(
            "MEMORY_QUERY_CACHE_MAX", "search.query_cache_max", 128, int, toml_data
        ),
        search_parallel_enabled=_b(
            "MEMORY_SEARCH_PARALLEL", "search.search_parallel_enabled", True, bool, toml_data
        ),
        reranker_disabled=_b(
            "MEMORY_RERANKER_DISABLED", "search.reranker_disabled", False, bool, toml_data
        ),
        llm_allow_remote_code=_b(
            "MEMORY_LLM_ALLOW_REMOTE_CODE", "features.llm_allow_remote_code", False, bool, toml_data
        ),
        deep_rerank_timeout=_b(
            "MEMORY_DEEP_RERANK_TIMEOUT", "search.deep_rerank_timeout", 30.0, float, toml_data
        ),
        contextual_retrieval=_b(
            "MEMORY_CONTEXTUAL_RETRIEVAL", "search.contextual_retrieval", True, bool, toml_data
        ),
        contextual_enrichment=_b(
            "MEMORY_CONTEXTUAL_ENRICHMENT", "search.contextual_enrichment", True, bool, toml_data
        ),
        forgetting_curve=_b(
            "MEMORY_FORGETTING_CURVE", "search.forgetting_curve", True, bool, toml_data
        ),
        forgetting_curve_half_life=_b(
            "MEMORY_FORGETTING_CURVE_HALF_LIFE", "search.forgetting_curve_half_life", 30.0, float, toml_data
        ),
        vec_rebuild_threshold=_b(
            "MEMORY_VEC_REBUILD_THRESHOLD", "search.vec_rebuild_threshold", 15, int, toml_data
        ),
        vec_rebuild_adaptive=_b(
            "MEMORY_VEC_REBUILD_ADAPTIVE", "search.vec_rebuild_adaptive", True, bool, toml_data
        ),
        ctr_data_window_days=_b(
            "MEMORY_CTR_DATA_WINDOW_DAYS", "search.ctr_data_window_days", 90, int, toml_data
        ),
        exploration_mode=_b(
            "MEMORY_EXPLORATION_MODE", "search.exploration_mode", "off", str, toml_data
        ),
        search_compute_budget_ms=_b(
            "MEMORY_SEARCH_COMPUTE_BUDGET_MS", "search.search_compute_budget_ms", 200.0, float, toml_data
        ),
    )

    # ---- kg ----
    kg = KGConfig(
        entity_min_occurrences=_b(
            "MEMORY_ENTITY_MIN_OCCURRENCES", "search.entity_min_occurrences", 2, int, toml_data
        ),
        kg_coccurr_entity_cap=_b(
            "MEMORY_KG_COCCUR_ENTITY_CAP", "search.kg_coccurr_entity_cap", 20, int, toml_data
        ),
        kg_edge_weight_increment=_b(
            "MEMORY_KG_EDGE_WEIGHT_INCREMENT", "search.kg_edge_weight_increment", 0.1, float, toml_data
        ),
        kg_edge_weight_cap=_b(
            "MEMORY_KG_EDGE_WEIGHT_CAP", "search.kg_edge_weight_cap", 10.0, float, toml_data
        ),
        ner_spacy_enabled=_b(
            "MEMORY_NER_SPACY", "features.ner_spacy_enabled", False, bool, toml_data
        ),
    )

    # ---- graph_cache ----
    graph_cache = GraphCacheConfig(
        graph_cache_max=_b(
            "MEMORY_GRAPH_CACHE_MAX", "search.graph_cache_max", 50, int, toml_data
        ),
        graph_cache_ttl_s=_b(
            "MEMORY_GRAPH_CACHE_TTL_S", "search.graph_cache_ttl_s", 60.0, float, toml_data
        ),
    )

    # ---- features ----
    features = FeatureFlagsConfig(
        write_journal=_b(
            "MEMORY_WRITE_JOURNAL_ENABLED", "features.write_journal", False, bool, toml_data
        ),
        write_journal_fallback_sync=_b(
            "MEMORY_WRITE_JOURNAL_FALLBACK_SYNC", "features.write_journal_fallback_sync", False, bool, toml_data
        ),
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
            "MEMORY_ADAPTIVE_RETENTION", "features.adaptive_retention", True, bool, toml_data
        ),
        neural_forget_mode=_b(
            "MEMORY_NEURAL_FORGET_MODE", "features.neural_forget_mode", "formula", str, toml_data
        ),
        neural_forget_weights=_b(
            "MEMORY_NEURAL_FORGET_WEIGHTS", "features.neural_forget_weights", "", str, toml_data
        ),
        temporal_ssm_enabled=_b(
            "MEMORY_TEMPORAL_SSM_ENABLED", "features.temporal_ssm_enabled", False, bool, toml_data
        ),
        temporal_ssm_weights=_b(
            "MEMORY_TEMPORAL_SSM_WEIGHTS", "features.temporal_ssm_weights", "", str, toml_data
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
        legacy_note_crdt=_b(
            "MEMORY_LEGACY_NOTE_CRDT", "features.legacy_note_crdt", False, bool, toml_data
        ),
        llm_extraction=_b(
            "MEMORY_LLM_EXTRACTION", "features.llm_extraction", True, bool, toml_data
        ),
        feature_temporal_kg=_b(
            "MEMORY_TEMPORAL_KG", "features.feature_temporal_kg", True, bool, toml_data
        ),
        feature_temporal_kg_llm=_b(
            "MEMORY_TEMPORAL_KG_LLM", "features.feature_temporal_kg_llm", True, bool, toml_data
        ),
        temporal_kg_llm_tier=_b(
            "MEMORY_TEMPORAL_KG_LLM_TIER", "features.temporal_kg_llm_tier", "light", str, toml_data
        ),
        feature_belief_layer=_b(
            "MEMORY_BELIEF_LAYER", "features.feature_belief_layer", True, bool, toml_data
        ),
        self_editing=_b(
            "MEMORY_SELF_EDITING", "features.self_editing", True, bool, toml_data
        ),
        knowledge_compilation=_b(
            "MEMORY_KNOWLEDGE_COMPILATION", "features.knowledge_compilation", True, bool, toml_data
        ),
        graph_centrality_boost=_b(
            "MEMORY_GRAPH_CENTRALITY_BOOST", "features.graph_centrality_boost", True, bool, toml_data
        ),
        graph_communities=_b(
            "MEMORY_GRAPH_COMMUNITIES", "features.graph_communities", True, bool, toml_data
        ),
        graph_evolution_tracking=_b(
            "MEMORY_GRAPH_EVOLUTION_TRACKING", "features.graph_evolution_tracking", True, bool, toml_data
        ),
        ner_spacy_enabled=_b(
            "MEMORY_NER_SPACY", "features.ner_spacy_enabled", False, bool, toml_data
        ),
        session_memory=_b(
            "MEMORY_SESSION_MEMORY", "session_memory.enabled", False, bool, toml_data
        ),
        session_decision_llm=_b(
            "MEMORY_SESSION_DECISION_LLM", "session_memory.decision_llm", False, bool, toml_data
        ),
        session_cross_entity_boost=_b(
            "MEMORY_SESSION_CROSS_ENTITY_BOOST", "session_memory.cross_entity_boost", True, bool, toml_data
        ),
    )

    # ---- write (save pipeline) ----
    write = WritePipelineConfig(
        write_journal=features.write_journal,
        quality_gates=features.quality_gates,
        saga_enabled=features.saga_enabled,
        defer_expensive=True,
        save_max_content_bytes=_b(
            "MEMORY_SAVE_MAX_CONTENT_BYTES", "save_pipeline.max_content_bytes", 50000, int, toml_data
        ),
        save_max_tags=_b(
            "MEMORY_SAVE_MAX_TAGS", "save_pipeline.max_tags", 50, int, toml_data
        ),
        save_max_category_len=_b(
            "MEMORY_SAVE_MAX_CATEGORY_LEN", "save_pipeline.max_category_len", 64, int, toml_data
        ),
        save_max_slug_len=_b(
            "MEMORY_SAVE_MAX_SLUG_LEN", "save_pipeline.max_slug_len", 128, int, toml_data
        ),
    )

    # ---- embedding ----
    embedding = EmbeddingConfig(
        backend=_b(
            "MEMORY_EMBEDDING_BACKEND", "embedding.backend", "auto", str, toml_data
        ),
        model_id=_b(
            "MEMORY_EMBEDDING_MODEL_ID", "embedding.model_id", "Potion-8M", str, toml_data
        ),
        model_revision=_b(
            "MEMORY_EMBEDDING_MODEL_REVISION", "embedding.model_revision", "", str, toml_data
        ),
        idle_unload_seconds=_b(
            "MEMORY_LLM_EXTRACTION_IDLE_UNLOAD_SECONDS", "llm_extraction.idle_unload_seconds", 600, int, toml_data
        ),
    )
    _validate_model_revision(embedding.model_revision)

    # ---- auto_save ----
    auto_save = AutoSaveConfig(
        max_retries=_b(
            "MEMORY_AUTO_SAVE_MAX_RETRIES", "auto_save.max_retries", 3, int, toml_data
        ),
        backoff_base_seconds=_b(
            "MEMORY_AUTO_SAVE_BACKOFF_BASE_SECONDS", "auto_save.backoff_base_seconds", 1.0, float, toml_data
        ),
        backoff_cap_seconds=_b(
            "MEMORY_AUTO_SAVE_BACKOFF_CAP_SECONDS", "auto_save.backoff_cap_seconds", 300.0, float, toml_data
        ),
        circuit_breaker_seconds=_b(
            "MEMORY_AUTO_SAVE_CIRCUIT_BREAKER_SECONDS", "auto_save.circuit_breaker_seconds", 300.0, float, toml_data
        ),
        failure_window_seconds=_b(
            "MEMORY_AUTO_SAVE_FAILURE_WINDOW_SECONDS", "auto_save.failure_window_seconds", 60.0, float, toml_data
        ),
        batch_interval_seconds=_b(
            "MEMORY_AUTO_SAVE_BATCH_INTERVAL_SECONDS", "auto_save.batch_interval_seconds", 5.0, float, toml_data
        ),
        batch_size=_b(
            "MEMORY_AUTO_SAVE_BATCH_SIZE", "auto_save.batch_size", 50, int, toml_data
        ),
        daemon_idle_seconds=_b(
            "MEMORY_AUTO_SAVE_DAEMON_IDLE_SECONDS", "auto_save.daemon_idle_seconds", 300, int, toml_data
        ),
        inbox_max_bytes=_b(
            "MEMORY_AUTO_SAVE_INBOX_MAX_BYTES", "auto_save.inbox_max_bytes", 500_000, int, toml_data
        ),
        preview_max=_b(
            "AUTO_SAVE_PREVIEW_MAX", "auto_save.preview_max", 200, int, toml_data
        ),
        params_max=_b(
            "AUTO_SAVE_PARAMS_MAX", "auto_save.params_max", 2000, int, toml_data
        ),
        health_check_minutes=_b(
            "MEMORY_AUTO_SAVE_HEALTH_CHECK_MINUTES", "auto_save.health_check_minutes", 15, int, toml_data
        ),
        allowlist=_b(
            "AUTO_SAVE_TOOL_ALLOWLIST",
            "auto_save.allowlist",
            "memory_save,memory_supersede,memory_delete,todowrite,task,question,write,edit",
            str,
            toml_data,
        ),
        denylist=_b(
            "AUTO_SAVE_TOOL_DENYLIST",
            "auto_save.denylist",
            "filesystem_list_allowed_directories,filesystem_list_directory,"
            "filesystem_directory_tree,filesystem_read_multiple_files,"
            "filesystem_search_files,filesystem_get_file_info,"
            "filesystem_list_directory_with_sizes,memory_session_start,"
            "memory_user_profile,memory_recall,memory_profile_access,"
            "memory_record_ctr_feedback,memory_check_concept_drift,todo,process,"
            "read_terminal",
            str,
            toml_data,
        ),
        keyword_routing=_b(
            "MEMORY_AUTO_SAVE_KEYWORD_ROUTING", "auto_save.keyword_routing", True, bool, toml_data
        ),
        always_sessions=_b(
            "MEMORY_AUTO_SAVE_ALWAYS_SESSIONS", "auto_save.always_sessions", False, bool, toml_data
        ),
    )

    # ---- recall ----
    recall = RecallConfig(
        max_tokens=_b(
            "MEMORY_RECALL_MAX_TOKENS", "recall.max_tokens", 800, int, toml_data
        ),
        tier1_hot_days=_b(
            "MEMORY_RECALL_TIER1_DAYS", "recall.tier1_hot_days", 7, int, toml_data
        ),
        tier_fallback_threshold=_b(
            "MEMORY_RECALL_TIER_FALLBACK_THRESHOLD", "recall.tier_fallback_threshold", 5, int, toml_data
        ),
    )

    # ---- user_profile ----
    user_profile = UserProfileConfig(
        ctr_data_window_days=_b(
            "MEMORY_CTR_DATA_WINDOW_DAYS", "search.ctr_data_window_days", 90, int, toml_data
        ),
        exploration_mode=search.exploration_mode,
        window_days=_b(
            "MEMORY_USER_PROFILE_WINDOW_DAYS", "user_profile.window_days", 90, int, toml_data
        ),
        max_size=_b(
            "MEMORY_USER_PROFILE_MAX_SIZE", "user_profile.max_size", 50, int, toml_data
        ),
        recency_half_life_days=_b(
            "MEMORY_USER_PROFILE_RECENCY_HALF_LIFE_DAYS", "user_profile.recency_half_life_days", 30, int, toml_data
        ),
    )

    # ---- quality_gates ----
    quality_gates_cfg = QualityGatesConfig(
        min_content_length=_b(
            "MEMORY_QUALITY_MIN_CONTENT_LENGTH", "quality_gates.min_content_length", 20, int, toml_data
        ),
        max_duplicate_similarity=_b(
            "MEMORY_QUALITY_MAX_DUPLICATE_SIMILARITY", "quality_gates.max_duplicate_similarity", 0.90, float, toml_data
        ),
        min_relevance_score=_b(
            "MEMORY_QUALITY_MIN_RELEVANCE_SCORE", "quality_gates.min_relevance_score", 0.30, float, toml_data
        ),
    )

    # ---- sharing ----
    sharing = MemorySharingConfig(
        shared_pool_ttl_days=_b(
            "MEMORY_SHARED_POOL_TTL_DAYS", "multi_agent.shared_pool_ttl_days", 30, int, toml_data
        ),
        shared_pool_max_size=_b(
            "MEMORY_SHARED_POOL_MAX_SIZE", "multi_agent.shared_pool_max_size", 1000, int, toml_data
        ),
    )

    # ---- cache ----
    cache = CacheConfig(
        fts5_cache=_b("MEMORY_FTS5_CACHE", "cache.fts5_cache", True, bool, toml_data),
        fts5_cache_ttl=_b(
            "MEMORY_FTS5_CACHE_TTL", "cache.fts5_cache_ttl", 30, int, toml_data
        ),
        vec_cache_max=_b(
            "MEMORY_VEC_CACHE_MAX", "cache.vec_cache_max", 500, int, toml_data
        ),
        vec_cache_ttl_s=_b(
            "MEMORY_VEC_CACHE_TTL_S", "cache.vec_cache_ttl_s", 300.0, float, toml_data
        ),
    )

    # ---- llm ----
    llm = LLMConfig(
        provider=_b(
            "MEMORY_LLM_PROVIDER", "llm_extraction.provider", "none", str, toml_data
        ),
        ollama_host=_b(
            "MEMORY_OLLAMA_HOST", "llm_extraction.ollama_host", "http://localhost:11434", str, toml_data
        ),
        ollama_model=_b(
            "MEMORY_OLLAMA_MODEL", "llm_extraction.ollama_model", "qwen2.5:3b", str, toml_data
        ),
        ollama_timeout_s=_b(
            "MEMORY_OLLAMA_TIMEOUT_S", "llm_extraction.ollama_timeout_s", 30.0, float, toml_data
        ),
        llama_cpp_host=_b(
            "MEMORY_LLAMA_CPP_HOST", "llm_extraction.llama_cpp_host", "http://localhost:8080", str, toml_data
        ),
        llama_cpp_model=_b(
            "MEMORY_LLAMA_CPP_MODEL", "llm_extraction.llama_cpp_model", "", str, toml_data
        ),
        llama_cpp_timeout_s=_b(
            "MEMORY_LLAMA_CPP_TIMEOUT_S", "llm_extraction.llama_cpp_timeout_s", 30.0, float, toml_data
        ),
        openai_compatible_host=_b(
            "MEMORY_OPENAI_COMPATIBLE_HOST", "llm_extraction.openai_compatible_host", "http://127.0.0.1:1234", str, toml_data
        ),
        openai_compatible_model=_b(
            "MEMORY_OPENAI_COMPATIBLE_MODEL", "llm_extraction.openai_compatible_model", "qwen/qwen3.5-9b", str, toml_data
        ),
        openai_compatible_timeout_s=_b(
            "MEMORY_OPENAI_COMPATIBLE_TIMEOUT_S", "llm_extraction.openai_compatible_timeout_s", 120.0, float, toml_data
        ),
        extraction_model_id=_b(
            "MEMORY_LLM_EXTRACTION_MODEL_ID", "llm_extraction.model_id", "Qwen/Qwen2.5-3B-Instruct", str, toml_data
        ),
        extraction_max_tokens=_b(
            "MEMORY_LLM_EXTRACTION_MAX_TOKENS", "llm_extraction.max_tokens", 256, int, toml_data
        ),
        extraction_hybrid_threshold=_b(
            "MEMORY_LLM_HYBRID_THRESHOLD", "llm_extraction.hybrid_threshold", 0.5, float, toml_data
        ),
        extraction_force=_b(
            "MEMORY_LLM_FORCE", "llm_extraction.force", False, bool, toml_data
        ),
    )

    # ---- hybrid ----
    hybrid = HybridSearchConfig(
        fts_weight=_b(
            "MEMORY_HYBRID_FTS_WEIGHT", "hybrid.fts_weight", 0.5, float, toml_data
        ),
        semantic_weight=_b(
            "MEMORY_HYBRID_SEMANTIC_WEIGHT", "hybrid.semantic_weight", 0.3, float, toml_data
        ),
        rrf_k=_b(
            "MEMORY_HYBRID_RRF_K", "hybrid.rrf_k", 60, int, toml_data
        ),
        semantic_overfetch=_b(
            "MEMORY_HYBRID_SEMANTIC_OVERFETCH", "hybrid.semantic_overfetch", 50, int, toml_data
        ),
        rank_proxy_scale=_b(
            "MEMORY_HYBRID_RANK_PROXY_SCALE", "hybrid.rank_proxy_scale", 30.0, float, toml_data
        ),
    )

    # ---- rerank ----
    rerank = RerankConfig(
        half_life_days=_b(
            "MEMORY_RERANK_HALF_LIFE_DAYS", "search.rerank_half_life_days", 180.0, float, toml_data
        ),
        cross_encoder_blend=_b(
            "MEMORY_CROSS_ENCODER_BLEND", "search.cross_encoder_blend", 0.6, float, toml_data
        ),
        late_interaction_blend=_b(
            "MEMORY_LATE_INTERACTION_BLEND", "search.late_interaction_blend", 0.3, float, toml_data
        ),
        topic_similarity_threshold=_b(
            "MEMORY_TOPIC_SIMILARITY_THRESHOLD", "search.topic_similarity_threshold", 0.15, float, toml_data
        ),
        concept_drift_threshold=_b(
            "MEMORY_CONCEPT_DRIFT_THRESHOLD", "search.concept_drift_threshold", 0.15, float, toml_data
        ),
        temporal_decay_weight=_b(
            "MEMORY_TEMPORAL_DECAY_WEIGHT", "search.temporal_decay_weight", 0.15, float, toml_data
        ),
        ce_blend=_b(
            "MEMORY_CE_BLEND", "search.ce_blend", 0.85, float, toml_data
        ),
        ce_chunk_blend=_b(
            "MEMORY_CE_CHUNK_BLEND", "search.ce_chunk_blend", 0.7, float, toml_data
        ),
        ce_deep_enabled=_b(
            "MEMORY_CE_DEEP", "search.ce_deep_enabled", False, bool, toml_data
        ),
    )

    # ---- semantic_kg ----
    semantic_kg = SemanticKGConfig(
        max_claims_semantic=_b(
            "MEMORY_MAX_CLAIMS_SEMANTIC", "semantic.max_claims_semantic", 10000, int, toml_data
        ),
        semantic_threshold=_b(
            "MEMORY_SEMANTIC_THRESHOLD", "semantic.semantic_threshold", 0.65, float, toml_data
        ),
        kg_dedup_threshold=_b(
            "MEMORY_KG_DEDUP_THRESHOLD", "kg.dedup.threshold", 0.92, float, toml_data
        ),
    )

    # ---- sync ----
    sync = SyncConfig(
        enable_server=_b(
            "MEMORY_SYNC_ENABLE_SERVER", "sync.enable_server", False, bool, toml_data
        ),
        listen_host=_b(
            "MEMORY_SYNC_LISTEN_HOST", "sync.listen_host", "127.0.0.1", str, toml_data
        ),
        listen_port=_b(
            "MEMORY_SYNC_LISTEN_PORT", "sync.listen_port", 9877, int, toml_data
        ),
        peers=_resolve_sync_peers(toml_data),
        interval_minutes=_b(
            "MEMORY_SYNC_INTERVAL_MINUTES", "sync.schedule.interval_minutes", 5, int, toml_data
        ),
    )

    # ---- api ----
    api = APIConfig(
        enable_server=_b(
            "MEMORY_API_ENABLE_SERVER", "api.enable_server", False, bool, toml_data
        ),
        listen_host=_b(
            "MEMORY_API_LISTEN_HOST", "api.listen_host", "127.0.0.1", str, toml_data
        ),
        listen_port=_b(
            "MEMORY_API_LISTEN_PORT", "api.listen_port", 9878, int, toml_data
        ),
        api_token=_b("MEMORY_API_TOKEN", "api.token", "", str, toml_data),
        insecure_loopback=_b(
            "MEMORY_API_INSECURE_LOOPBACK", "api.insecure_loopback", False, bool, toml_data
        ),
        dashboard_address=_b(
            "MEMORY_DASHBOARD_ADDRESS", "api.dashboard_address", "127.0.0.1", str, toml_data
        ),
    )

    # ---- health_check ----
    health_check = HealthCheckConfig(
        vec_index_drift_threshold=_b(
            "MEMORY_VEC_INDEX_DRIFT_THRESHOLD",
            "health_check.vec_index_drift_threshold",
            50,
            int,
            toml_data,
        ),
        disk_pct_used_threshold=_b(
            "MEMORY_DISK_PCT_USED_THRESHOLD",
            "health_check.disk_pct_used_threshold",
            95,
            int,
            toml_data,
        ),
    )

    # ---- rate_limits (raw dict — not yet mapped to RateLimitsConfig) ----
    raw_rate_limits = _b(
        "MEMORY_RATE_LIMITS", "rate_limits", None, cast=None, toml_data=toml_data
    )

    return MemoryConfig(
        general=general,
        search=search,
        kg=kg,
        graph_cache=graph_cache,
        write=write,
        embedding=embedding,
        auto_save=auto_save,
        sync=sync,
        api=api,
        quality_gates=quality_gates_cfg,
        sharing=sharing,
        cache=cache,
        llm=llm,
        hybrid=hybrid,
        rerank=rerank,
        features=features,
        user_profile=user_profile,
        recall=recall,
        semantic_kg=semantic_kg,
        rate_limits=raw_rate_limits,
        health_check=health_check,
    )


def get_config() -> MemoryConfig:
    """Return the singleton ``MemoryConfig``.

    Safe to call from ``@st.cache_resource`` or any lazy-init context.
    Thread-safe via double-checked locking.

    Decomposed 2026-06-22: the actual dataclass construction moved to
    ``_build_config_from_toml`` so this orchestrator stays readable.
    """
    global _instance
    import sys

    # NOTE: do NOT use "unittest" in sys.modules as a testing signal.
    # Production ML stacks (torch/transformers) import unittest.mock at
    # runtime, which permanently flipped this check to True in real
    # processes — defeating the singleton and forcing a full TOML parse +
    # dataclass rebuild on EVERY get_config() call (~0.4ms each, thousands
    # per search query). pytest-based tests are always covered by the two
    # remaining signals below.
    is_testing = (
        "pytest" in sys.modules
        or os.environ.get("PYTEST_CURRENT_TEST") is not None
    )
    if _instance is not None and not is_testing:
        return _instance

    with _instance_lock:
        if _instance is not None and not is_testing:
            return _instance

        toml_data = _read_toml(_TOML_PATH)
        cfg = _build_config_from_toml(toml_data)
        _instance = cfg if not is_testing else None

    # Config-drift startup enforcement (scope-aware, hatch-able).
    # Run OUTSIDE the lock to prevent re-entrant deadlock when
    # run_startup_enforcement -> build_drift_report -> get_feature_flags
    # -> get_config() re-enters the same lock on the same thread.
    try:
        from infra.config_drift_policy import run_startup_enforcement
        run_startup_enforcement()
    except SystemExit:
        raise  # propagate EX_CONFIG
    except Exception:
        logger.warning("startup enforcement skipped: non-critical error")

    return cfg


def reset_config() -> None:
    """Reset the singleton (useful in tests)."""
    global _instance
    with _instance_lock:
        _instance = None
    from infra.config_drift_policy import reset_policy_cache
    reset_policy_cache()
    from infra.config_drift import reset_flag_tiers
    reset_flag_tiers()
    import infra.config_drift_policy as _cdp
    _cdp._active_has_inited = False
    _cdp._last_resolved_toml_mtime = 0.0
    _cdp._TOML_HOT_RELOAD_SUBSCRIBED = False


def get_feature_flags() -> dict:
    """Return all feature flags with their resolved values and sources.

    Returns a dict of {flag_name: {value, env_var, toml_path, default}}.
    The feature flags are the boolean fields in the MemoryConfig
    "features", "search", and "cache" sections. Each entry carries
    the resolved value, env var name, TOML path, default, and any
    human-readable warnings about the flag's current state.
    """

    def _flag(value, env_var, toml_path, default):
        warnings = []
        if not value and default:
            if "temporal" in toml_path or "temporal" in env_var.lower():
                warnings.append(
                    "Temporal features disabled: temporal KG, contradiction "
                    "detection, and supersession are off."
                )
            elif "KG" in env_var or "kg" in toml_path:
                warnings.append(
                    "Knowledge graph disabled; graph-RAG, backlinks, and "
                    "fact extraction are off."
                )
            elif "quality" in toml_path:
                warnings.append("Quality gates disabled; noisy/degraded content may pass.")
            elif "saga" in toml_path:
                warnings.append("Saga rolled back; writes may be non-atomic on failure.")
            elif "crdt" in toml_path:
                warnings.append("CRDT disabled; concurrent writes may overwrite each other.")
            elif "summarization" in toml_path:
                warnings.append("Summarization disabled; long notes will not be condensed.")
            elif "consolidation" in toml_path:
                warnings.append("Consolidation disabled; duplicate memories may accumulate.")
            elif "tiers" in toml_path:
                warnings.append("Temporal tiers disabled; adaptive retention is off.")
            elif "user_profile" in toml_path:
                warnings.append("User profile disabled; search personalization is off.")
        return {
            "value": value,
            "env_var": env_var,
            "toml_path": toml_path,
            "default": default,
            "warnings": warnings,
        }

    cfg = get_config()
    _f = cfg.features
    return {
        "write_journal": _flag(
            _f.write_journal,
            "MEMORY_WRITE_JOURNAL_ENABLED",
            "features.write_journal",
            False,
        ),
        "multi_agent": _flag(
            _f.multi_agent, "MEMORY_MULTI_AGENT", "features.multi_agent", True
        ),
        "summarization": _flag(
            _f.summarization, "MEMORY_SUMMARIZATION", "features.summarization", True
        ),
        "user_profile": _flag(
            _f.user_profile, "MEMORY_USER_PROFILE", "features.user_profile", True
        ),
        "self_directed": _flag(
            _f.self_directed, "MEMORY_SELF_DIRECTED", "features.self_directed", True
        ),
        "adaptive_retention": _flag(
            _f.adaptive_retention,
            "MEMORY_ADAPTIVE_RETENTION",
            "features.adaptive_retention",
            True,
        ),
        "temporal_ssm_enabled": _flag(
            _f.temporal_ssm_enabled,
            "MEMORY_TEMPORAL_SSM_ENABLED",
            "features.temporal_ssm_enabled",
            False,
        ),
        "neural_forget_mode": _flag(
            _f.neural_forget_mode,
            "MEMORY_NEURAL_FORGET_MODE",
            "features.neural_forget_mode",
            "formula",
        ),
        "neural_forget_weights": _flag(
            _f.neural_forget_weights,
            "MEMORY_NEURAL_FORGET_WEIGHTS",
            "features.neural_forget_weights",
            "",
        ),
        "temporal_ssm_weights": _flag(
            _f.temporal_ssm_weights,
            "MEMORY_TEMPORAL_SSM_WEIGHTS",
            "features.temporal_ssm_weights",
            "",
        ),
        "consolidation": _flag(
            _f.consolidation,
            "MEMORY_CONSOLIDATION",
            "features.consolidation",
            True,
        ),
        "quality_gates": _flag(
            _f.quality_gates, "MEMORY_QUALITY_GATES", "features.quality_gates", True
        ),
        "saga_enabled": _flag(
            _f.saga_enabled, "MEMORY_SAGA_ENABLED", "features.saga_enabled", True
        ),
        "temporal_tiers": _flag(
            _f.temporal_tiers, "MEMORY_TEMPORAL_TIERS", "features.temporal_tiers", True
        ),
        "crdt_enabled": _flag(
            _f.crdt_enabled, "MEMORY_CRDT_ENABLED", "features.crdt_enabled", True
        ),
        "legacy_note_crdt": _flag(
            _f.legacy_note_crdt,
            "MEMORY_LEGACY_NOTE_CRDT",
            "features.legacy_note_crdt",
            False,
        ),
        "llm_extraction": _flag(
            _f.llm_extraction,
            "MEMORY_LLM_EXTRACTION",
            "features.llm_extraction",
            True,
        ),
        "feature_temporal_kg": _flag(
            _f.feature_temporal_kg,
            "MEMORY_TEMPORAL_KG",
            "features.feature_temporal_kg",
            True,
        ),
        "feature_temporal_kg_llm": _flag(
            _f.feature_temporal_kg_llm,
            "MEMORY_TEMPORAL_KG_LLM",
            "features.feature_temporal_kg_llm",
            True,
        ),
        "temporal_kg_llm_tier": _flag(
            _f.temporal_kg_llm_tier,
            "MEMORY_TEMPORAL_KG_TIER",
            "features.temporal_kg_llm_tier",
            "light",
        ),
        "feature_belief_layer": _flag(
            _f.feature_belief_layer,
            "MEMORY_BELIEF_LAYER",
            "features.feature_belief_layer",
            True,
        ),
        "self_editing": _flag(
            _f.self_editing,
            "MEMORY_SELF_EDITING",
            "features.self_editing",
            True,
        ),
        "knowledge_compilation": _flag(
            _f.knowledge_compilation,
            "MEMORY_KNOWLEDGE_COMPILATION",
            "features.knowledge_compilation",
            True,
        ),
        "graph_centrality_boost": _flag(
            _f.graph_centrality_boost,
            "MEMORY_GRAPH_CENTRALITY_BOOST",
            "features.graph_centrality_boost",
            True,
        ),
        "graph_communities": _flag(
            _f.graph_communities,
            "MEMORY_GRAPH_COMMUNITIES",
            "features.graph_communities",
            True,
        ),
        "graph_evolution_tracking": _flag(
            _f.graph_evolution_tracking,
            "MEMORY_GRAPH_EVOLUTION_TRACKING",
            "features.graph_evolution_tracking",
            True,
        ),
        "ner_spacy_enabled": _flag(
            _f.ner_spacy_enabled,
            "MEMORY_NER_SPACY",
            "features.ner_spacy_enabled",
            False,
        ),
        "session_memory": _flag(
            _f.session_memory,
            "MEMORY_SESSION_MEMORY",
            "features.session_memory",
            False,
        ),
        "session_decision_llm": _flag(
            _f.session_decision_llm,
            "MEMORY_SESSION_DECISION_LLM",
            "features.session_decision_llm",
            False,
        ),
        "fts5_cache": _flag(
            cfg.cache.fts5_cache, "MEMORY_FTS5_CACHE", "cache.fts5_cache", True
        ),
        "query_cache": _flag(
            cfg.search.query_cache, "MEMORY_QUERY_CACHE", "search.query_cache", True
        ),
        "reranker_disabled": _flag(
            cfg.search.reranker_disabled,
            "MEMORY_RERANKER_DISABLED",
            "search.reranker_disabled",
            False,
        ),
        "llm_allow_remote_code": _flag(
            cfg.llm.allow_remote_code,
            "MEMORY_LLM_ALLOW_REMOTE_CODE",
            "llm.allow_remote_code",
            False,
        ),
        "contextual_retrieval": _flag(
            cfg.search.contextual_retrieval,
            "MEMORY_CONTEXTUAL_RETRIEVAL",
            "search.contextual_retrieval",
            True,
        ),
        "defer_expensive": _flag(
            cfg.write.defer_expensive,
            "MEMORY_DEFER_EXPENSIVE",
            "write.defer_expensive",
            True,
        ),
    }


def log_feature_flags_at_startup() -> None:
    """Emit a JSON snapshot of all feature flags to the INFO log.

    Format: ``feature_flags_snapshot=<json>``.
    Called once at process startup so operators have a record of which
    flags were on/off for the lifetime of the session.
    """
    flags = get_feature_flags()
    logger.info("feature_flags_snapshot=%s", json.dumps(flags))


__all__ = [
    "MemoryConfig",
    "get_config",
    "reset_config",
    "get_feature_flags",
    "log_feature_flags_at_startup",
    "INSTALL_ROOT",
    "GLOBAL_SCRIPTS_DIR",
    "SCRIPTS_SUBDIR",
    "AGENTS_SKILLS_DIR",
    "OPENCODE_SKILLS_DIR",
    "resolve_db_path",
    "DECISION_CATEGORIES",
]
