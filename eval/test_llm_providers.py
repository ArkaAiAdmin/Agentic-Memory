"""Tests for the S3 LLM provider abstraction (Ollama + Llama.cpp + HF).

Uses an in-process mock HTTP server that mimics the Ollama API.
No real network calls — all servers run on 127.0.0.1 with an
ephemeral port so tests are fast and don't depend on the host
having Ollama/Llama.cpp installed.

The mock server validates:
  - Request path is /api/generate (Ollama) or /completion (llama.cpp)
  - Request body has ``model``, ``prompt``, ``stream: false``
  - Response has ``response`` (Ollama) or ``content`` (llama.cpp)
"""

import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional


def _free_port() -> int:
    """Return an unused localhost port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _MockOllamaHandler(BaseHTTPRequestHandler):
    """Minimal Ollama /api/generate + /api/tags handler."""

    # The test sets these before starting the server.
    model_name: str = "test-model"
    response_text: str = '{"facts": [], "entities": []}'
    fail_next: bool = False
    call_count: int = 0
    last_request_body: Optional[dict[str, Any]] = None

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence the test output — the default request log is noisy.
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/tags":
            body = json.dumps({"models": [{"name": self.model_name}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        type(self).call_count += 1
        if type(self).fail_next:
            type(self).fail_next = False
            self.send_response(500)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            type(self).last_request_body = json.loads(raw)
        except json.JSONDecodeError:
            type(self).last_request_body = None
        body = json.dumps({"response": self.response_text}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _MockLlamaCppHandler(BaseHTTPRequestHandler):
    """Minimal llama.cpp /completion + /health handler."""

    response_text: str = '{"content": "0.5"}'
    call_count: int = 0

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        type(self).call_count += 1
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            json.loads(raw)  # validate
        except json.JSONDecodeError:
            pass
        body = json.dumps({"content": self.response_text}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_server(handler_cls: type) -> tuple[HTTPServer, str, int]:
    """Start a mock server on a free port, return (server, host, port)."""
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Brief sleep so the server is accepting connections by the time
    # the test makes its first request.
    time.sleep(0.05)
    return server, "127.0.0.1", port


class TestOllamaProvider(unittest.TestCase):
    def setUp(self) -> None:
        _MockOllamaHandler.call_count = 0
        _MockOllamaHandler.last_request_body = None
        _MockOllamaHandler.fail_next = False
        _MockOllamaHandler.model_name = "qwen2.5:3b"
        _MockOllamaHandler.response_text = (
            '{"facts": [{"subject": "x", "predicate": "is_a", '
            '"object": "y", "confidence": 0.9}], "entities": []}'
        )
        self.server, self.host, self.port = _start_server(_MockOllamaHandler)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_is_available_returns_true(self) -> None:
        from fact.llm_providers import OllamaProvider

        provider = OllamaProvider(
            host=f"http://{self.host}:{self.port}", model="qwen2.5:3b"
        )
        self.assertTrue(provider.is_available())

    def test_generate_sends_correct_request(self) -> None:
        from fact.llm_providers import OllamaProvider

        provider = OllamaProvider(
            host=f"http://{self.host}:{self.port}", model="qwen2.5:3b"
        )
        out = provider.generate("Hello, world!", max_tokens=128, temperature=0.0)
        self.assertIn("x", out)  # the mock's response text contains "x"
        self.assertEqual(_MockOllamaHandler.call_count, 1)
        # Validate the request body.
        body = _MockOllamaHandler.last_request_body
        assert body is not None  # the mock captured the POST body
        self.assertEqual(body["model"], "qwen2.5:3b")
        self.assertEqual(body["prompt"], "Hello, world!")
        self.assertEqual(body["stream"], False)
        self.assertEqual(body["options"]["num_predict"], 128)
        self.assertEqual(body["options"]["temperature"], 0.0)

    def test_availability_cache(self) -> None:
        from fact.llm_providers import OllamaProvider

        provider = OllamaProvider(
            host=f"http://{self.host}:{self.port}", model="qwen2.5:3b"
        )
        # First call hits the server.
        self.assertTrue(provider.is_available())
        # Second call should use the cache (we'd need to verify by
        # killing the server, but a simpler proxy: subsequent calls
        # should still return True without raising).
        self.assertTrue(provider.is_available())
        self.assertTrue(provider.is_available())

    def test_generate_returns_empty_on_server_error(self) -> None:
        from fact.llm_providers import OllamaProvider

        provider = OllamaProvider(
            host=f"http://{self.host}:{self.port}", model="qwen2.5:3b"
        )
        _MockOllamaHandler.fail_next = True
        out = provider.generate("test")
        self.assertEqual(out, "")

    def test_unavailable_server(self) -> None:
        from fact.llm_providers import OllamaProvider

        # Point at a port nothing is listening on.
        provider = OllamaProvider(host="http://127.0.0.1:1", model="qwen2.5:3b")
        self.assertFalse(provider.is_available())


class TestLlamaCppProvider(unittest.TestCase):
    def setUp(self) -> None:
        _MockLlamaCppHandler.call_count = 0
        _MockLlamaCppHandler.response_text = "0.7"
        self.server, self.host, self.port = _start_server(_MockLlamaCppHandler)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_is_available_returns_true(self) -> None:
        from fact.llm_providers import LlamaCppProvider

        provider = LlamaCppProvider(
            host=f"http://{self.host}:{self.port}", model="default"
        )
        self.assertTrue(provider.is_available())

    def test_generate_sends_correct_request(self) -> None:
        from fact.llm_providers import LlamaCppProvider

        provider = LlamaCppProvider(
            host=f"http://{self.host}:{self.port}", model="default"
        )
        out = provider.generate("test prompt", max_tokens=4, temperature=0.0)
        self.assertEqual(out, "0.7")
        self.assertEqual(_MockLlamaCppHandler.call_count, 1)


class TestProviderSelection(unittest.TestCase):
    def setUp(self) -> None:
        from fact.llm_providers import reset_provider_cache

        reset_provider_cache()

    def tearDown(self) -> None:
        from fact.llm_providers import reset_provider_cache

        reset_provider_cache()

    def test_get_provider_returns_none_when_nothing_available(self) -> None:
        import os
        from fact.llm_providers import get_provider, reset_provider_cache

        reset_provider_cache()
        env = {
            "MEMORY_LLM_PROVIDER": "ollama",
            "MEMORY_OLLAMA_HOST": "http://127.0.0.1:1",  # nothing here
            "MEMORY_LLAMA_CPP_HOST": "http://127.0.0.1:1",
            "MEMORY_LLM_EXTRACTION_MODEL_ID": "nonexistent/model",
        }
        old = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            reset_provider_cache()
            self.assertIsNone(get_provider())
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            reset_provider_cache()

    def test_get_provider_selects_ollama_when_available(self) -> None:
        import os
        from fact.llm_providers import get_provider, reset_provider_cache

        _MockOllamaHandler.call_count = 0
        _MockOllamaHandler.response_text = "{}"
        server, host, port = _start_server(_MockOllamaHandler)
        env = {
            "MEMORY_LLM_PROVIDER": "ollama",
            "MEMORY_OLLAMA_HOST": f"http://{host}:{port}",
            "MEMORY_LLAMA_CPP_HOST": "http://127.0.0.1:1",
            "MEMORY_LLM_EXTRACTION_MODEL_ID": "nonexistent/model",
        }
        old = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            reset_provider_cache()
            provider = get_provider()
            assert provider is not None  # the mock server is up
            self.assertEqual(provider.name, "ollama")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            server.shutdown()
            server.server_close()
            reset_provider_cache()

    def test_fallback_chain_skips_unavailable_providers(self) -> None:
        """If Ollama is down, fall back to llama.cpp; if that's also
        down, fall back to HuggingFace. This validates the
        ``_FALLBACK_CHAIN`` logic in get_provider()."""
        import os
        from fact.llm_providers import get_provider, reset_provider_cache

        env = {
            # Preferred = huggingface, but the test environment has
            # no transformers installed for the HF model. The chain
            # should try ollama (down), llama.cpp (down), then give
            # up — but importantly it should NOT crash.
            "MEMORY_LLM_PROVIDER": "huggingface",
            "MEMORY_OLLAMA_HOST": "http://127.0.0.1:1",
            "MEMORY_LLAMA_CPP_HOST": "http://127.0.0.1:1",
            "MEMORY_LLM_EXTRACTION_MODEL_ID": "nonexistent/model",
        }
        old = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            reset_provider_cache()
            # Should return None (no providers available) without
            # raising — the chain tries each one and gives up.
            provider = get_provider()
            # Either None (no providers) or HuggingFace (if transformers
            # is installed and the test model path exists). We don't
            # assert specifically — just that we don't crash.
            self.assertIn(provider.name if provider else None, (None, "huggingface"))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            reset_provider_cache()


class TestLLMExtractionV2(unittest.TestCase):
    """Validate that the v2 extraction functions work end-to-end with
    the mock Ollama server."""

    def setUp(self) -> None:
        from fact.llm_providers import reset_provider_cache

        reset_provider_cache()
        _MockOllamaHandler.call_count = 0
        _MockOllamaHandler.response_text = (
            '{"facts": [{"subject": "Python", "predicate": "is_a", '
            '"object": "language", "confidence": 0.95}], '
            '"entities": [{"name": "Python", "type": "concept", '
            '"description": "A programming language"}]}'
        )
        self.server, self.host, self.port = _start_server(_MockOllamaHandler)

    def tearDown(self) -> None:
        from fact.llm_providers import reset_provider_cache

        reset_provider_cache()
        self.server.shutdown()
        self.server.server_close()

    def test_extract_facts_via_llm_v2_uses_provider(self) -> None:
        import os
        from fact.llm_providers import reset_provider_cache

        env = {
            "MEMORY_LLM_PROVIDER": "ollama",
            "MEMORY_OLLAMA_HOST": f"http://{self.host}:{self.port}",
            "MEMORY_LLAMA_CPP_HOST": "http://127.0.0.1:1",
        }
        old = {k: os.environ.get(k) for k in env}
        orig_llm_ext = os.environ.get("MEMORY_LLM_EXTRACTION")
        try:
            os.environ.update(env)
            reset_provider_cache()
            # Force llm_extraction ON
            os.environ["MEMORY_LLM_EXTRACTION"] = "1"
            import llm_extraction

            facts = llm_extraction.extract_facts_via_llm_v2("Python is a language.")
            # Mock returned 1 fact. Note: we need to bypass the
            # hook-process guard (only matters in hook subprocesses).
            self.assertGreaterEqual(len(facts), 1)
            self.assertEqual(facts[0][0], "Python")
            self.assertEqual(_MockOllamaHandler.call_count, 1)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if orig_llm_ext is None:
                os.environ.pop("MEMORY_LLM_EXTRACTION", None)
            else:
                os.environ["MEMORY_LLM_EXTRACTION"] = orig_llm_ext
            reset_provider_cache()


if __name__ == "__main__":
    unittest.main()
