"""LLM provider abstraction for local model inference.

S3 (Local SLM Support): enables running fact extraction and
contradiction scoring against local Small Language Models served by
Ollama, Llama.cpp, or HuggingFace transformers. The previous
implementation only supported HuggingFace, which requires ~5GB of
dependencies and a GPU/MPS device. The new abstraction makes it
trivial to add a new provider.

Providers
---------
- ``OllamaProvider``: HTTP API to a local Ollama server (default
  ``http://localhost:11434``). Zero Python deps beyond ``urllib``.
  Recommended for most users — Ollama is a one-line install and
  handles model download/quantization.
- ``LlamaCppProvider``: HTTP API to a local llama.cpp ``server``
  binary. Same JSON schema as Ollama's ``/completion`` endpoint.
  Use when you want to run a specific GGUF model without Ollama.
- ``HuggingFaceProvider``: in-process transformers (the original
  implementation). Requires ``transformers`` + ``torch``. Best when
  you already have the deps installed and want to avoid a separate
  server process.

Configuration
--------------
- ``MEMORY_LLM_PROVIDER`` (default: ``huggingface``): which provider
  to use. One of ``ollama``, ``llama_cpp``, ``huggingface``.
- ``MEMORY_OLLAMA_HOST`` (default: ``http://localhost:11434``):
  Ollama server URL.
- ``MEMORY_LLAMA_CPP_HOST`` (default: ``http://localhost:8080``):
  llama.cpp server URL.
- ``MEMORY_OLLAMA_MODEL`` / ``MEMORY_LLAMA_CPP_MODEL``: model name to
  request. Defaults to ``qwen2.5:3b`` for Ollama and the server's
  default for llama.cpp.

API contract
------------
Each provider exposes:

  ``is_available() -> bool``
      True if the provider can be used right now (server reachable,
      model loaded, etc). Cheap to call; cached per-process.

  ``generate(prompt: str, max_tokens: int = 256, temperature: float = 0.0) -> str``
      Run a single prompt completion. Returns the generated text, or
      ``""`` on failure. Implementations must enforce a timeout
      (``timeout_s`` config) and surface failures as empty strings
      (not exceptions) so callers can fall back gracefully.

Provider selection
-------------------
``get_provider()`` returns the configured provider singleton. If
the requested provider is unavailable, it falls back through the
chain: ``ollama`` -> ``llama_cpp`` -> ``huggingface`` -> ``None``
(regex-only fallback). This is the "graceful degradation" path —
users can install Ollama later and the system will pick it up
automatically.
"""

from __future__ import annotations

import logging

import json
import os
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Pinned to an explicit commit hash (OWASP LLM03-001): never load a HuggingFace
# model from a moving branch ref like "main". SHA is the current HEAD of the
# model repo (verified via the HuggingFace Hub API "sha" field). The model id
# itself is configurable via MEMORY_LLM_EXTRACTION_MODEL_ID, but the revision
# must always be an immutable commit hash, not a branch name.
EXTRACTION_REVISION = "aa8e72537993ba99e69dfaafa59ed015b17504d1"

# Environment/config override for the pinned revision, in case an operator
# needs to pin to a different immutable commit. Empty = use EXTRACTION_REVISION.
EXTRACTION_REVISION_ENV = "MEMORY_LLM_EXTRACTION_REVISION"


def _extraction_revision() -> str:
    """Return the pinned commit hash to load the extraction model from."""
    return os.environ.get(EXTRACTION_REVISION_ENV, "") or EXTRACTION_REVISION


def _allow_remote_code() -> bool:
    """Return whether trust_remote_code is permitted for local HF models.

    OWASP LLM03-001: enabling remote code execution runs arbitrary code from
    the model repo at load time. It is OFF by default and must be explicitly
    opted in via the ``llm_allow_remote_code`` config key (env
    MEMORY_LLM_ALLOW_REMOTE_CODE / config features.llm_allow_remote_code).
    """
    env = os.environ.get("MEMORY_LLM_ALLOW_REMOTE_CODE", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off", ""):
        if env == "":
            # Fall back to the system config flag if present.
            try:
                from config import get_config

                return bool(get_config().llm_allow_remote_code)
            except Exception as e:
                logger.warning("_allow_remote_code failed: %s", e)
                return False
        return False
    return False


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------


class BaseLLMProvider(ABC):
    """Abstract base for all LLM providers.

    Subclasses must implement ``is_available`` and ``generate``.
    Implementations should be thread-safe and idempotent — the
    singleton pattern in ``get_provider`` will call them from
    multiple threads concurrently.
    """

    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider can serve requests right now.

        Should be cheap (cached). False means the caller should
        fall back to the next provider in the chain.
        """

    @abstractmethod
    def generate(
        self, prompt: str, max_tokens: int = 256, temperature: float = 0.0
    ) -> str:
        """Run a single prompt completion.

        Args:
            prompt: The full prompt to send to the model.
            max_tokens: Maximum tokens to generate. Provider should
                respect this (or fail gracefully if it can't).
            temperature: Sampling temperature. 0.0 = deterministic.

        Returns:
            The generated text. Empty string on failure (timeout,
            connection error, bad response, etc). Implementations
            should NOT raise — call sites fall back to regex-only
            extraction when the LLM is unavailable.
        """


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


class OllamaProvider(BaseLLMProvider):
    """HTTP client for a local Ollama server.

    Ollama's API: ``POST /api/generate`` with JSON
    ``{"model": ..., "prompt": ..., "stream": false, "options": {...}}``.
    Response: ``{"response": "...", "done": true, ...}``.

    Zero Python dependencies — uses only ``urllib.request`` from the
    stdlib so this works on a minimal install.
    """

    name = "ollama"

    def __init__(self, host: str, model: str, timeout_s: float = 30.0):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self._available_cache: Optional[bool] = None
        self._available_cache_ts: float = 0.0
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        # Cache for 30s to avoid hammering the server on every
        # extraction call (extraction is in the hot path for
        # fact_temporal.py).
        with self._lock:
            now = time.time()
            if (
                self._available_cache is not None
                and (now - self._available_cache_ts) < 30.0
            ):
                return bool(self._available_cache)
        cached: bool = False
        try:
            url = f"{self.host}/api/tags"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                cached = resp.status == 200
        except Exception as e:
            logger.warning("is_available failed: %s", e)
            cached = False
        with self._lock:
            self._available_cache = cached
            self._available_cache_ts = time.time()
        return cached

    def generate(
        self, prompt: str, max_tokens: int = 256, temperature: float = 0.0
    ) -> str:
        url = f"{self.host}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            return str(data.get("response", ""))
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
            OSError,
        ) as e:
            logger.debug("OllamaProvider: generate failed: %s", e)
            # Invalidate the availability cache on failure — server
            # may have been restarted.
            with self._lock:
                self._available_cache = None
            return ""
        except Exception as e:
            logger.warning("OllamaProvider: unexpected generate error: %s", e)
            return ""


# ---------------------------------------------------------------------------
# Llama.cpp
# ---------------------------------------------------------------------------


class LlamaCppProvider(BaseLLMProvider):
    """HTTP client for a local llama.cpp ``server`` binary.

    llama.cpp's server uses the same JSON schema as Ollama for the
    ``/completion`` endpoint (the older non-OpenAI-compatible API).
    Newer llama.cpp builds also support ``/v1/chat/completions``
    (OpenAI-compatible) — we use ``/completion`` for max compat.

    Endpoints used:
      - ``GET  /health``           — liveness check
      - ``POST /completion``       — single-prompt completion
    """

    name = "llama_cpp"

    def __init__(self, host: str, model: str, timeout_s: float = 30.0):
        self.host = host.rstrip("/")
        self.model = model  # llama.cpp's /completion ignores this if no model is set
        self.timeout_s = timeout_s
        self._available_cache: Optional[bool] = None
        self._available_cache_ts: float = 0.0
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        with self._lock:
            now = time.time()
            if (
                self._available_cache is not None
                and (now - self._available_cache_ts) < 30.0
            ):
                return bool(self._available_cache)
        cached: bool = False
        try:
            url = f"{self.host}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                cached = resp.status == 200
        except Exception as e:
            logger.warning("is_available failed: %s", e)
            cached = False
        with self._lock:
            self._available_cache = cached
            self._available_cache_ts = time.time()
        return cached

    def generate(
        self, prompt: str, max_tokens: int = 256, temperature: float = 0.0
    ) -> str:
        url = f"{self.host}/completion"
        payload = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            return str(data.get("content", ""))
        except Exception as e:
            logger.debug("LlamaCppProvider: generate failed: %s", e)
            with self._lock:
                self._available_cache = None
            return ""


# ---------------------------------------------------------------------------
# HuggingFace (in-process, the original implementation)
# ---------------------------------------------------------------------------


class HuggingFaceProvider(BaseLLMProvider):
    """In-process HuggingFace transformers model.

    This is the original implementation extracted from
    ``llm_extraction.LLMExtractor``. Kept here so the abstraction
    is complete and the original code path still works for users
    who have the heavy transformers + torch deps installed.
    """

    name = "huggingface"

    def __init__(self, model_id: str, device: str = ""):
        self.model_id = model_id
        self.device = device
        self._model: Optional[Any] = None
        self._tokenizer: Optional[Any] = None
        self._resolved_device: str = ""
        self._load_lock = threading.Lock()
        self._load_attempted = False
        self._load_error: Optional[str] = None
        self._max_input_chars: int = 8000

    def is_available(self) -> bool:
        # Try to import transformers; if it fails, we're not available.
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
        except ImportError:
            return False
        if self._model is not None:
            return True
        # Lazy-load on first call.
        return self._ensure_loaded()

    def _ensure_loaded(self) -> bool:
        if self._model is not None:
            return True
        with self._load_lock:
            if self._model is not None:
                return True
            if self._load_attempted:
                return False
            self._load_attempted = True
            try:
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer

                if not self.device:
                    if (
                        hasattr(torch.backends, "mps")
                        and torch.backends.mps.is_available()
                    ):
                        self.device = "mps"
                    else:
                        self.device = "cpu"
                self._tokenizer = AutoTokenizer.from_pretrained(
                    self.model_id,
                    revision=_extraction_revision(),
                )
                model_kwargs: dict[str, Any] = {
                    "dtype": torch.float16,
                    "revision": _extraction_revision(),
                }
                # trust_remote_code executes arbitrary code from the model
                # repo; only enable it when explicitly opted in (OWASP
                # LLM03-001). Default is False.
                if _allow_remote_code():
                    model_kwargs["trust_remote_code"] = True
                model_loaded: Any = AutoModelForCausalLM.from_pretrained(
                    self.model_id, **model_kwargs
                )
                self._model = model_loaded.to(self.device).eval()
                self._resolved_device = self.device
                return True
            except Exception as e:
                logger.warning("_ensure_loaded failed: %s", e)
                self._load_error = str(e)[:200]
                self._model = None
                self._tokenizer = None
                return False

    def generate(
        self, prompt: str, max_tokens: int = 256, temperature: float = 0.0
    ) -> str:
        if not self._ensure_loaded():
            return ""
        assert self._model is not None and self._tokenizer is not None
        try:
            import torch

            text = prompt
            if len(text) > self._max_input_chars:
                text = text[: self._max_input_chars] + "\n...<truncated>"
            messages = [{"role": "user", "content": text}]
            formatted = self._tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            inputs = self._tokenizer(
                formatted,
                return_tensors="pt",
                truncation=True,
                max_length=4096,
            )
            if self._resolved_device:
                inputs = {k: v.to(self._resolved_device) for k, v in inputs.items()}
            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=max_tokens,
                    do_sample=False,
                    temperature=None,
                    top_p=None,
                    pad_token_id=self._tokenizer.eos_token_id,
                )
            input_len = inputs["input_ids"].shape[1]
            generated_ids = outputs[0][input_len:]
            return str(self._tokenizer.decode(generated_ids, skip_special_tokens=True))
        except Exception as e:
            logger.debug("HuggingFaceProvider: generate failed: %s", e)
            return ""


# ---------------------------------------------------------------------------
# Provider selection + fallback chain
# ---------------------------------------------------------------------------


# Module-level singleton. Protected by _singleton_lock for thread
# safety; the provider itself is also thread-safe via its own locks.
_provider_singleton: Optional[BaseLLMProvider] = None
_provider_singleton_name: Optional[str] = None
_singleton_lock = threading.Lock()


def _resolve_provider_name() -> str:
    """Resolve the LLM provider name from env var or MemoryConfig.

    Priority:
      1. ``MEMORY_LLM_PROVIDER`` env var (highest — allows per-run override)
      2. ``MemoryConfig.llm.provider`` (TOML config; ``"none"`` means unset)
      3. ``"huggingface"`` (hard fallback)
    """
    env_name = os.environ.get("MEMORY_LLM_PROVIDER")
    if env_name:
        name = env_name.strip().lower()
        if name not in ("ollama", "llama_cpp", "huggingface"):
            logger.warning("Unknown LLM provider %r, falling back to huggingface", name)
            return "huggingface"
        return name

    try:
        from infra.config import get_config  # lazy — avoids circular imports
        cfg = get_config()
        config_name = getattr(cfg, "provider", "none")
        if config_name and config_name != "none":
            name = config_name.strip().lower()
            if name in ("ollama", "llama_cpp", "huggingface"):
                return name
            logger.warning("Unknown LLM provider %r from config, falling back to huggingface", name)
    except Exception:
        pass

    return "huggingface"


def _make_provider(name: str) -> BaseLLMProvider:
    """Construct the requested provider from env vars."""
    if name == "ollama":
        host = os.environ.get("MEMORY_OLLAMA_HOST", "http://localhost:11434")
        model = os.environ.get("MEMORY_OLLAMA_MODEL", "qwen2.5:3b")
        timeout = float(os.environ.get("MEMORY_OLLAMA_TIMEOUT_S", "30"))
        return OllamaProvider(host=host, model=model, timeout_s=timeout)
    if name == "llama_cpp":
        host = os.environ.get("MEMORY_LLAMA_CPP_HOST", "http://localhost:8080")
        model = os.environ.get("MEMORY_LLAMA_CPP_MODEL", "")
        timeout = float(os.environ.get("MEMORY_LLAMA_CPP_TIMEOUT_S", "30"))
        return LlamaCppProvider(host=host, model=model, timeout_s=timeout)
    # huggingface (default)
    model_id = os.environ.get(
        "MEMORY_LLM_EXTRACTION_MODEL_ID", "Qwen/Qwen2.5-3B-Instruct"
    )
    return HuggingFaceProvider(model_id=model_id)


# Fallback chain order. If the requested provider is unavailable,
# try these in order before giving up.
_FALLBACK_CHAIN: list[str] = ["ollama", "llama_cpp", "huggingface"]


def get_provider() -> Optional[BaseLLMProvider]:
    """Return the configured LLM provider, or None if none are available.

    The selection logic:
      1. If MEMORY_LLM_PROVIDER is set, try that provider first.
      2. If that provider is not available, walk the fallback chain
         (ollama -> llama_cpp -> huggingface) until one works.
      3. If all providers fail, return None — callers should fall
         back to regex-only extraction.

    The result is memoized per-process: the first call to
    ``get_provider`` performs the health checks; subsequent calls
    return the same instance until the process exits.
    """
    global _provider_singleton, _provider_singleton_name
    with _singleton_lock:
        if _provider_singleton is not None:
            return _provider_singleton
        preferred = _resolve_provider_name()
        # Try the preferred provider first, then the fallback chain.
        candidates = [preferred] + [p for p in _FALLBACK_CHAIN if p != preferred]
        for name in candidates:
            try:
                provider = _make_provider(name)
            except Exception as e:
                logger.debug("Failed to construct %s provider: %s", name, e)
                continue
            if provider.is_available():
                _provider_singleton = provider
                _provider_singleton_name = name
                logger.info(
                    "LLM provider selected: %s (preferred was %s)",
                    name,
                    preferred,
                )
                return _provider_singleton
        logger.info(
            "No LLM provider available; extraction will fall back to regex-only"
        )
        return None


def reset_provider_cache() -> None:
    """Drop the cached provider. Used by tests and config reloads."""
    global _provider_singleton, _provider_singleton_name
    with _singleton_lock:
        _provider_singleton = None
        _provider_singleton_name = None


def get_provider_name() -> Optional[str]:
    """Return the name of the active provider, or None."""
    if _provider_singleton_name is not None:
        return _provider_singleton_name
    # If the singleton hasn't been constructed yet, peek at the
    # preferred name without forcing a connection.
    return _resolve_provider_name()
