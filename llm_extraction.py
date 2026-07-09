"""LLM-based fact and entity extraction for agentic-memory.

Lazy-loads a small instruction-tuned model (default: Qwen2.5-1.5B-Instruct)
on MPS to extract structured facts and entities from memory content.
Falls back gracefully to regex extraction if the model isn't available.

Opt-in via MEMORY_LLM_EXTRACTION=1.
"""

from __future__ import annotations

import logging

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional, cast

logger = logging.getLogger(__name__)

__all__ = [
    "LLMExtractor",
    "is_llm_extraction_available",
    "extract_facts_via_llm",
    "extract_entities_via_llm",
    "score_fact_contradiction_via_llm",
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """Extract facts and entities from the text below. Return ONLY a JSON object with "facts" and "entities" arrays. No explanation, no markdown — just raw JSON.

Expected format:
{{"facts":[{{"subject":"...","predicate":"...","object":"...","event_time":"YYYY-MM-DD or null","event_time_granularity":"day|month|year|unknown"}}],"entities":[{{"name":"...","type":"concept","description":"..."}}]}}

Predicate options: is_a, has_description, defines, uses, creates, stores, requires, depends_on, provides, handles, manages, processes, connects_to, triggers, implements, replaces, configures, monitors, tracks, computes, validates, extracts

Entity type options: concept, person, organization, place, technology, process, file, function, module, language, framework

Rules:
- Subject/predicate/object in facts must be concise (max 8 words each)
- Confidence 0.0-1.0 reflecting how explicitly the fact is in the text
- event_time: when the fact was true in the world (ISO date YYYY-MM-DD, or YYYY-MM for month precision, or YYYY for year, or null if no time reference)
- event_time_granularity: "day" if exact date, "month" if month precision, "year" if year only, "unknown" if no time reference or uncertain
- Entity descriptions one short sentence
- Only extract things clearly present in the text
- If nothing found, return {{"facts":[],"entities":[]}}

TEXT:
{text}"""


# ---------------------------------------------------------------------------
# Contradiction scoring prompt (T11, 2026-06-23)
# ---------------------------------------------------------------------------

# T11: Score contradiction confidence between two facts on [0.0, 1.0].
# Used by `reconcile_fact_supersession` (in fact_temporal.py) when the
# `MEMORY_TEMPORAL_KG_LLM=1` flag is set.  The deterministic rule (same S+P,
# different O, overlapping event_time) is still applied as a pre-filter;
# the LLM only sees fact pairs that already pass the deterministic check
# and disambiguates "true contradiction" from "complementary detail".
#
# Example: "Alice is_a engineer" vs "Alice is_a senior engineer" — the
# deterministic check says "different object", but the LLM should score
# this LOW (~0.1) because senior-engineer is a refinement, not a
# contradiction.
_CONTRADICTION_PROMPT = """Score the contradiction confidence between these two facts on a scale of 0.0 to 1.0.

Return ONLY a single decimal number. No explanation, no markdown.

Scoring guide:
- 1.0: direct contradiction (e.g. "lives in Berlin" vs "lives in Paris")
- 0.8-0.9: strong contradiction, likely one supersedes the other
- 0.5-0.7: ambiguous, could be contradictory or could be a refinement
- 0.2-0.4: probably not contradictory, but distinct
- 0.0-0.1: clearly compatible (e.g. "is_a engineer" vs "is_a senior engineer" — refinement, not contradiction)

Fact A: {fact_a}
Fact B: {fact_b}

Score:"""

# Regex to extract a single float from the LLM output.  Matches
# positive decimals or integers; out-of-range values are clamped
# after parsing.
_SCORE_RE = re.compile(r"([01]?\.\d+|\d+(?:\.\d*)?)")


def _parse_contradiction_score(text: str) -> float | None:
    """Parse a single float in [0.0, 1.0] from the LLM output.

    Looks for the first number in the text.  Out-of-range values are
    clamped to [0.0, 1.0].  Returns None if no valid number is found.

    Note: this is a best-effort parser.  Pathological inputs like
    ``"-0.3"`` will be parsed as ``0.3`` (the regex strips the sign);
    downstream ``clamp`` logic still ensures the return is in range.
    LLMs in practice almost never produce negative scores, so this
    edge case is acceptable.
    """
    m = _SCORE_RE.search(text.strip())
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def score_fact_contradiction_via_llm(
    subj_a: str,
    pred_a: str,
    obj_a: str,
    subj_b: str,
    pred_b: str,
    obj_b: str,
    tier: str = "heavy",
) -> float | None:
    """T11 Sprint 3: Score contradiction confidence in [0.0, 1.0] using the LLM.

    Used by ``reconcile_fact_supersession`` (in fact_temporal.py) when
    ``feature_temporal_kg_llm=true``.  Returns None on any failure
    (LLM unavailable, model not loaded, output unparseable).  The caller
    should fall back to a deterministic 1.0 score when None is returned.

    Cost: ~100-500ms per call (synchronous).  The LLM extractor caches
    model state in a module-level singleton so subsequent calls are
    faster than the first.

    The LLM is only invoked AFTER the deterministic pre-filter in
    ``detect_fact_contradiction`` has confirmed the facts share subject +
    predicate and have different objects + overlapping event_time.  This
    LLM call is the disambiguator, not the gate.

    Tiers:
      * "light" → max_new_tokens=4, cheaper, faster
      * "heavy" → max_new_tokens=8, thorough (default)
    """
    if not is_llm_extraction_available():
        return None

    extractor = _get_extractor()
    if not extractor.is_loaded and not extractor.load():
        return None

    fact_a = f"{subj_a} {pred_a} {obj_a}"
    fact_b = f"{subj_b} {pred_b} {obj_b}"
    prompt = _CONTRADICTION_PROMPT.format(fact_a=fact_a, fact_b=fact_b)
    max_new = 4 if tier == "light" else 8

    try:
        import torch

        messages = [{"role": "user", "content": prompt}]
        formatted = extractor._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = extractor._tokenizer(
            formatted, return_tensors="pt", truncation=True, max_length=1024
        )
        if extractor._device:
            inputs = {k: v.to(extractor._device) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = extractor._model.generate(
                **inputs,
                max_new_tokens=max_new,  # tier-aware: light=4, heavy=8
                do_sample=False,
                temperature=None,
                top_p=None,
                pad_token_id=extractor._tokenizer.eos_token_id,
            )
        input_len = inputs["input_ids"].shape[1]
        generated_ids = outputs[0][input_len:]
        raw_output = extractor._tokenizer.decode(
            generated_ids, skip_special_tokens=True
        )
        return _parse_contradiction_score(raw_output)
    except Exception as exc:
        logger.warning("LLM contradiction scoring failed: %s", exc)
        return None


# Regex to extract JSON from model output (handles markdown fences, stray text)
_JSON_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)

# Strip leading articles from subjects
_STRIP_ARTICLES = re.compile(
    r"^(?:The|A|An|This|That|These|Those|Its|Our|Your)\s+", re.I
)

# Max input characters sent to the model
_MAX_INPUT_CHARS = 3000

# Max output tokens (default; overridable via MEMORY_LLM_EXTRACTION_MAX_TOKENS
# or [llm_extraction].max_tokens in memory.toml — 2026-06-19 cut from 1024 to
# 256 for ~4x speedup. See memory/lessons/2026-06-19-kg-llm-extraction-speed.md)
_MAX_NEW_TOKENS = 256


def _get_max_tokens() -> int:
    """Resolve max_tokens from config or env, falling back to _MAX_NEW_TOKENS."""
    try:
        from infra._lazy_imports import get_config

        return int(get_config().llm.extraction_max_tokens)
    except Exception as e:
        logger.warning("_get_max_tokens failed: %s", e)
        v = os.environ.get("MEMORY_LLM_EXTRACTION_MAX_TOKENS")
        if v:
            try:
                return int(v)
            except ValueError:
                pass
        return _MAX_NEW_TOKENS


# Predicate normalization: map past-tense / variant forms to canonical predicates
_PREDICATE_NORMALIZE: dict[str, str] = {
    "created": "creates",
    "built": "creates",
    "generated": "creates",
    "produced": "creates",
    "stored": "stores",
    "saved": "stores",
    "cached": "stores",
    "persisted": "stores",
    "deleted": "deletes",
    "removed": "deletes",
    "dropped": "deletes",
    "purged": "deletes",
    "cleared": "deletes",
    "wrote": "writes",
    "read": "reads",
    "loaded": "loads",
    "fetched": "fetches",
    "used": "uses",
    "utilized": "uses",
    "employed": "uses",
    "leveraged": "uses",
    "called": "calls",
    "invoked": "calls",
    "required": "requires",
    "needed": "requires",
    "depended_on": "depends_on",
    "provided": "provides",
    "offered": "provides",
    "handled": "handles",
    "processed": "processes",
    "parsed": "parses",
    "analyzed": "analyzes",
    "validated": "validates",
    "verified": "verifies",
    "checked": "checks",
    "monitored": "monitors",
    "tracked": "tracks",
    "enabled": "enables",
    "disabled": "disables",
    "configured": "configures",
    "overrode": "overrides",
    "connected": "connects_to",
    "bound": "binds_to",
    "triggered": "triggers",
    "initiated": "triggers",
    "combined": "combines",
    "merged": "merges",
    "applied": "applies",
    "supported": "supports",
    "prevented": "prevents",
    "captured": "captures",
    "detected": "detects",
    "identified": "identifies",
    "evaluated": "evaluates",
    "managed": "manages",
    "computed": "computes",
    "calculated": "calculates",
    "measured": "measures",
    "ranked": "ranks",
    "sorted": "sorts",
    "filtered": "filters",
    "wrapped": "wraps",
    "extended": "extends",
    "implemented": "implements",
    "replaced": "replaces",
    "superseded": "supersedes",
    "converted": "converts",
    "transformed": "transforms",
    "deduplicated": "deduplicates",
}


# ---------------------------------------------------------------------------
# Extractor singleton
# ---------------------------------------------------------------------------


class LLMExtractor:
    """Lazy-loading LLM extractor for facts and entities.

    Thread-safe singleton. Call ``extract()`` — the first call loads the
    model (takes ~2-5s on Apple Silicon), subsequent calls are instant.
    """

    def __init__(self, model_id: str):
        self.model_id = model_id
        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str = ""
        self._load_lock = threading.Lock()
        self._load_attempted = False
        self._load_error: Optional[str] = None
        self.last_used: float = 0.0
        try:
            from infra._lazy_imports import get_config

            self._idle_unload_s = int(get_config().idle_unload_seconds)
        except Exception as e:
            logger.warning("__init__ failed: %s", e)
            self._idle_unload_s = int(
                os.environ.get("MEMORY_LLM_EXTRACTION_IDLE_UNLOAD_SECONDS", "1800")
            )
        _start_idle_unload_monitor_if_needed()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def _resolve_device(self) -> str:
        try:
            import torch

            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except Exception as e:
            logger.warning("_resolve_device failed: %s", e)
        return "cpu"

    def load(self) -> bool:
        """Load the extraction model. Idempotent and thread-safe.

        Returns True on success. On failure, sets ``_load_error`` and returns False.
        """
        if self.is_loaded:
            return True
        with self._load_lock:
            if self.is_loaded:
                return True
            if self._load_attempted:
                return False

            self._load_attempted = True
            try:
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer

                device = self._resolve_device()
                logger.info("LLMExtractor: loading %s on %s...", self.model_id, device)

                self._tokenizer = AutoTokenizer.from_pretrained(
                    self.model_id, trust_remote_code=True
                )
                self._model = AutoModelForCausalLM.from_pretrained(
                    self.model_id,
                    dtype=torch.float16,
                    trust_remote_code=True,
                )
                self._model = self._model.to(device).eval()
                self._device = device

                logger.info(
                    "LLMExtractor: %s loaded on %s",
                    self.model_id,
                    device,
                )
                return True

            except Exception as e:
                self._load_error = str(e)[:200]
                logger.warning(
                    "LLMExtractor: failed to load %s: %s",
                    self.model_id,
                    e,
                )
                self._model = None
                self._tokenizer = None
                return False

    def unload(self) -> None:
        """Free GPU memory. Next extract() will re-load."""
        if self._model is not None:
            del self._model
        if self._tokenizer is not None:
            del self._tokenizer
        self._model = None
        self._tokenizer = None
        self._load_attempted = False
        self._load_error = None
        self._device = ""
        logger.info("LLMExtractor: unloaded")

    def extract(
        self, content: str, max_tokens: int = _MAX_NEW_TOKENS
    ) -> dict[str, Any]:
        """Extract facts and entities from content.

        Returns:
            Dict with ``facts`` (list of triples with confidence) and
            ``entities`` (list of entity dicts). Empty dict on failure.
        """
        if not self.is_loaded:
            if not self.load():
                return {}
        self.last_used = time.time()

        # Truncate input
        text = content.strip()
        if len(text) > _MAX_INPUT_CHARS:
            text = text[:_MAX_INPUT_CHARS] + "\n...<truncated>"

        prompt = _EXTRACTION_PROMPT.format(text=text)

        try:
            import torch

            messages = [
                {"role": "user", "content": prompt},
            ]
            formatted = self._tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )

            inputs = self._tokenizer(
                formatted,
                return_tensors="pt",
                truncation=True,
                max_length=4096,
            )

            if self._device:
                inputs = {k: v.to(self._device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=max_tokens,
                    do_sample=False,
                    temperature=None,
                    top_p=None,
                    pad_token_id=self._tokenizer.eos_token_id,
                )

            # Decode only the new tokens
            input_len = inputs["input_ids"].shape[1]
            generated_ids = outputs[0][input_len:]
            raw_output = self._tokenizer.decode(generated_ids, skip_special_tokens=True)

            return self._parse_output(raw_output)

        except Exception as e:
            logger.warning("LLMExtractor: extraction failed: %s", e)
            return {}

    def _parse_output(self, raw: str) -> dict[str, Any]:
        """Parse model output into structured dict with facts and entities.

        Handles common model output issues: markdown fences, stray text,
        partial JSON, trailing commas.
        """
        raw = raw.strip()

        # Strip markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)

        # Try to find a JSON object
        match = _JSON_RE.search(raw)
        if not match:
            logger.debug("LLMExtractor: no JSON found in output: %s", raw[:200])
            return {}

        json_str = match.group(0)

        try:
            result = json.loads(json_str)
        except json.JSONDecodeError:
            # Try to recover common JSON errors
            json_str = self._repair_json(json_str)
            try:
                result = json.loads(json_str)
            except json.JSONDecodeError as e:
                logger.debug("LLMExtractor: JSON parse failed after repair: %s", e)
                return {}

        if not isinstance(result, dict):
            return {}

        validated: dict[str, list[Any]] = {"facts": [], "entities": []}

        # Validate facts
        facts = result.get("facts")
        if isinstance(facts, list):
            for f in facts:
                if not isinstance(f, dict):
                    continue
                subj = str(f.get("subject", "")).strip()
                pred = str(f.get("predicate", "")).strip()
                obj = str(f.get("object", "")).strip()
                conf_str = f.get("confidence", 0.8)
                try:
                    conf = float(conf_str)
                    conf = max(0.0, min(1.0, conf))
                except (ValueError, TypeError):
                    conf = 0.8

                if subj and pred and obj and len(subj) > 1 and len(obj) > 1:
                    # Normalize predicate to canonical form
                    pred_lower = pred.lower().replace(" ", "_")
                    pred = _PREDICATE_NORMALIZE.get(pred_lower, pred_lower)
                    # Strip leading articles from subjects
                    subj = _STRIP_ARTICLES.sub("", subj).strip()
                    et = f.get("event_time")
                    etg = f.get("event_time_granularity", "unknown")
                    if isinstance(et, str) and et.strip():
                        event_time = et.strip()
                    else:
                        event_time = None
                    if isinstance(etg, str) and etg.strip():
                        event_time_granularity = etg.strip().lower()
                    else:
                        event_time_granularity = "unknown"
                    validated["facts"].append((
                        subj, pred, obj, round(conf, 4),
                        event_time, event_time_granularity,
                    ))

        # Validate entities
        entities = result.get("entities")
        if isinstance(entities, list):
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                name = str(ent.get("name", "")).strip()
                etype = str(ent.get("type", "concept")).strip().lower()
                desc = str(ent.get("description", "")).strip()

                valid_types = {
                    "concept",
                    "person",
                    "organization",
                    "place",
                    "technology",
                    "process",
                    "file",
                    "function",
                    "module",
                    "language",
                    "framework",
                }
                if etype not in valid_types:
                    etype = "concept"

                if name and len(name) > 1:
                    validated["entities"].append(
                        {
                            "name": name,
                            "type": etype,
                            "description": desc[:200] if desc else "",
                        }
                    )

        return validated

    @staticmethod
    def _repair_json(json_str: str) -> str:
        """Attempt to repair common JSON formatting errors from LLM output."""
        # Remove trailing commas before closing brackets
        json_str = re.sub(r",\s*([}\]])", r"\1", json_str)
        # Fix unquoted property names (simple case: word: value)
        json_str = re.sub(
            r"([{,])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:",
            r'\1"\2":',
            json_str,
        )
        return json_str


# ---------------------------------------------------------------------------
# Idle auto-unload monitor
# ---------------------------------------------------------------------------

_idle_monitor_started = False
_idle_monitor_lock = threading.Lock()


def _start_idle_unload_monitor_if_needed() -> None:
    """Start a daemon thread that unloads the LLM model after idle timeout.

    Reads ``MEMORY_LLM_EXTRACTION_IDLE_UNLOAD_SECONDS`` (default 1800 = 30 min).
    Set to 0 to disable auto-unload.
    """
    global _idle_monitor_started
    if _idle_monitor_started:
        return
    with _idle_monitor_lock:
        if _idle_monitor_started:
            return
        check_interval = 1800
        try:
            from infra._lazy_imports import get_config

            check_interval = int(get_config().idle_unload_seconds)
        except Exception as e:
            logger.warning("_start_idle_unload_monitor_if_needed failed: %s", e)
            check_interval = int(
                os.environ.get("MEMORY_LLM_EXTRACTION_IDLE_UNLOAD_SECONDS", "1800")
            )
        if check_interval <= 0:
            _idle_monitor_started = True
            return

        def _monitor() -> None:
            while True:
                time.sleep(60)
                try:
                    global _extractor
                    if _extractor is not None and _extractor.is_loaded:
                        idle = time.time() - _extractor.last_used
                        if idle > _extractor._idle_unload_s:
                            logger.info(
                                "LLMExtractor: idle %.0fs > %ds — auto-unloading",
                                idle,
                                _extractor._idle_unload_s,
                            )
                            _extractor.unload()
                except Exception:
                    logger.debug("LLMExtractor: idle monitor error", exc_info=True)

        t = threading.Thread(target=_monitor, daemon=True, name="llm-idle-unload")
        t.start()
        _idle_monitor_started = True


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_extractor: Optional[LLMExtractor] = None
_extractor_lock = threading.Lock()

# Hook subprocess names that must never load the LLM
_HOOK_SCRIPTS = frozenset(
    {
        "auto_save.py",
        "memory-proactive-context.py",
        "memory-search-on-demand.py",
        "memory-session-start.py",
        "memory-session-end.py",
        "memory-idle-checkpoint.py",
        "memory-pre-compaction.py",
    }
)


def _is_hook_process() -> bool:
    """Return True if running in a hook subprocess (must skip LLM loading)."""
    import sys

    return Path(sys.argv[0]).name in _HOOK_SCRIPTS if sys.argv else False


def _get_extractor(model_id: str = "") -> LLMExtractor:
    """Get or create the singleton extractor.

    On first call, reads model_id from config or uses default.
    Thread-safe.
    """
    global _extractor
    if _extractor is not None:
        return _extractor
    with _extractor_lock:
        if _extractor is not None:
            return _extractor
        if not model_id:
            try:
                from infra._lazy_imports import get_config

                model_id = get_config().llm.extraction_model_id
            except Exception as e:
                logger.warning("_get_extractor failed: %s", e)
                model_id = os.environ.get(
                    "MEMORY_LLM_EXTRACTION_MODEL_ID",
                    "Qwen/Qwen2.5-1.5B-Instruct",
                )
        _extractor = LLMExtractor(model_id)
        return _extractor


def is_llm_extraction_available() -> bool:
    """Check if LLM extraction is enabled and the model can be loaded.

    Returns False in hook subprocesses — each hook is a separate process
    and loading the model in each one would cause a thundering herd.
    """
    # Hook processes must never attempt model loading
    if _is_hook_process():
        return False

    try:
        from infra._lazy_imports import get_config

        if not get_config().llm_extraction:
            return False
    except Exception as e:
        logger.warning("is_llm_extraction_available failed: %s", e)
        if os.environ.get("MEMORY_LLM_EXTRACTION") != "1":
            return False

    extractor = _get_extractor()
    if extractor.is_loaded:
        return True
    return extractor.load()


def extract_facts_via_llm(content: str) -> list[tuple[str, str, str, float, str | None, str]]:
    """Extract facts using LLM. Returns list of 6-tuples:
    (subject, predicate, object, confidence, event_time, event_time_granularity).

    event_time is an ISO date string (YYYY-MM-DD, YYYY-MM, YYYY) or None
    if the LLM could not extract a time reference.
    event_time_granularity is one of: "day", "month", "year", "unknown".

    Returns empty list if LLM extraction is unavailable or fails.
    The caller should fall back to regex extraction.

    max_tokens is resolved from config (MEMORY_LLM_EXTRACTION_MAX_TOKENS
    or memory.toml [llm_extraction].max_tokens) — defaults to 256.
    """
    if not is_llm_extraction_available():
        return []

    extractor = _get_extractor()
    result = extractor.extract(content, max_tokens=_get_max_tokens())
    facts = result.get("facts", [])
    if not facts:
        return []
    return [
        (s, p, o, c, et, etg)
        for s, p, o, c, et, etg in facts
    ]


def extract_entities_via_llm(content: str) -> list[dict[str, str]]:
    """Extract entities using LLM. Returns list of {name, type, description}.

    Returns empty list if LLM extraction is unavailable or fails.
    The caller should fall back to regex extraction.
    """
    if not is_llm_extraction_available():
        return []

    extractor = _get_extractor()
    result = extractor.extract(content)
    return cast(list[dict[str, str]], result.get("entities", []))


# ---------------------------------------------------------------------------
# S3 (2026-06-23): provider-abstracted path
# ---------------------------------------------------------------------------
# These functions use the new llm_providers module, which supports
# Ollama, llama.cpp, and HuggingFace. They are the preferred path
# for new callers; the legacy _get_extractor path is kept for
# backward compatibility with code that imports LLMExtractor
# directly.
#
# The new path is a thin wrapper that:
#   1. Reads the preferred provider from config
#   2. Calls get_provider() (which handles the fallback chain)
#   3. Sends the prompt
#   4. Parses the response using the same _EXTRACTION_PROMPT format
#
# If no provider is available, the functions return empty results
# and the caller falls back to regex extraction.


def _parse_provider_output(raw: str) -> dict[str, Any]:
    """Parse a raw provider response into a facts/entities dict.

    Same parsing logic as LLMExtractor._parse_output — extracted so
    both code paths share the same JSON repair heuristics.
    """
    if not raw:
        return {}
    text = raw.strip()
    # Strip markdown code fences if present.
    if text.startswith("```"):
        # Drop the first line (```json or ```) and the last line (```).
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    # Try direct parse first.
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            return val
    except Exception as e:
        logger.warning("_parse_provider_output failed: %s", e)
    # Try to find a JSON object in the text.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            val = json.loads(text[start : end + 1])
            if isinstance(val, dict):
                return val
        except Exception as e:
            logger.warning("_parse_provider_output failed: %s", e)
    return {}


def _extract_via_provider(content: str, max_tokens: int) -> dict[str, Any]:
    """Run extraction using the configured LLM provider.

    Returns a dict with ``facts`` and ``entities`` keys. Empty dict
    if no provider is available or the call fails.
    """
    try:
        from fact.llm_providers import get_provider
    except Exception as e:
        logger.debug("llm_extraction: llm_providers import failed: %s", e)
        return {}
    provider = get_provider()
    if provider is None:
        return {}
    text = content.strip()
    if not text:
        return {}
    # Cap input to avoid blowing up the local model context.
    if len(text) > 8000:
        text = text[:8000] + "\n...<truncated>"
    prompt = _EXTRACTION_PROMPT.format(text=text)
    try:
        raw = provider.generate(prompt, max_tokens=max_tokens, temperature=0.0)
    except Exception as e:
        logger.debug("llm_extraction: provider.generate failed: %s", e)
        return {}
    return _parse_provider_output(raw)


def is_llm_extraction_available_via_provider() -> bool:
    """S3: check if any LLM provider is available.

    This is the new public API for checking LLM availability. The
    legacy ``is_llm_extraction_available`` is kept for backward
    compatibility; new code should prefer this function.
    """
    if _is_hook_process():
        return False
    try:
        from infra._lazy_imports import get_config

        if not get_config().llm_extraction:
            return False
    except Exception as e:
        logger.warning("is_llm_extraction_available_via_provider failed: %s", e)
        if os.environ.get("MEMORY_LLM_EXTRACTION") != "1":
            return False
    try:
        from fact.llm_providers import get_provider

        return get_provider() is not None
    except Exception as e:
        logger.warning("is_llm_extraction_available_via_provider failed: %s", e)
        return False


def extract_facts_via_llm_v2(content: str) -> list[tuple[str, str, str, float, str | None, str]]:
    """S3: extract facts using the provider abstraction.

    Returns list of 6-tuples:
    (subject, predicate, object, confidence, event_time, event_time_granularity).
    event_time is an ISO date string or None.
    event_time_granularity is one of: "day", "month", "year", "unknown".

    Falls back to legacy HuggingFace path if no provider is
    available, then to regex extraction (in the caller).
    """
    if not is_llm_extraction_available_via_provider():
        # Fall back to legacy path.
        return extract_facts_via_llm(content)
    max_tokens = _get_max_tokens()
    result = _extract_via_provider(content, max_tokens)
    facts = result.get("facts", [])
    if not isinstance(facts, list):
        return []
    out: list[tuple[str, str, str, float, str | None, str]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        s = str(f.get("subject", "")).strip()
        p = str(f.get("predicate", "")).strip()
        o = str(f.get("object", "")).strip()
        if not (s and p and o):
            continue
        c = float(f.get("confidence", 0.5) or 0.5)
        et = f.get("event_time")
        etg = f.get("event_time_granularity", "unknown")
        event_time = et.strip() if isinstance(et, str) and et.strip() else None
        event_time_granularity = (
            etg.strip().lower() if isinstance(etg, str) and etg.strip() else "unknown"
        )
        out.append((s, p, o, c, event_time, event_time_granularity))
    return out


def extract_entities_via_llm_v2(content: str) -> list[dict[str, str]]:
    """S3: extract entities using the provider abstraction."""
    if not is_llm_extraction_available_via_provider():
        return extract_entities_via_llm(content)
    max_tokens = _get_max_tokens()
    result = _extract_via_provider(content, max_tokens)
    entities = result.get("entities", [])
    if not isinstance(entities, list):
        return []
    out: list[dict[str, str]] = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "type": str(e.get("type", "concept")).strip() or "concept",
                "description": str(e.get("description", "")).strip(),
            }
        )
    return out


def score_fact_contradiction_via_llm_v2(
    subj_a: str,
    pred_a: str,
    obj_a: str,
    subj_b: str,
    pred_b: str,
    obj_b: str,
    tier: str = "heavy",
) -> float | None:
    """S3 Sprint 3: score contradiction using the provider abstraction."""
    if not is_llm_extraction_available_via_provider():
        return score_fact_contradiction_via_llm(
            subj_a, pred_a, obj_a, subj_b, pred_b, obj_b,
            tier=tier,
        )
    try:
        from fact.llm_providers import get_provider

        provider = get_provider()
    except Exception as e:
        logger.warning("score_fact_contradiction_via_llm_v2 failed: %s", e)
        return None
    if provider is None:
        return None
    fact_a = f"{subj_a} {pred_a} {obj_a}"
    fact_b = f"{subj_b} {pred_b} {obj_b}"
    prompt = _CONTRADICTION_PROMPT.format(fact_a=fact_a, fact_b=fact_b)
    max_new = 4 if tier == "light" else 8
    try:
        raw = provider.generate(prompt, max_tokens=max_new, temperature=0.0)
    except Exception as e:
        logger.warning("score_fact_contradiction_via_llm_v2 failed: %s", e)
        return None
    return _parse_contradiction_score(raw)
