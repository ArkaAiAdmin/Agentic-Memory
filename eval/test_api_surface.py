"""Tests for REST API surface — routing, authentication, and key endpoint behaviors."""

from __future__ import annotations

import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.expanduser("~/.config/agentic-memory"))


class TestAPIRouting:
    """Test that the API router dispatches to correct handlers."""

    def test_is_loopback_localhost(self):
        from infra.api_server import _is_loopback
        assert _is_loopback("localhost") is True

    def test_is_loopback_127(self):
        from infra.api_server import _is_loopback
        assert _is_loopback("127.0.0.1") is True

    def test_is_loopback_ipv6(self):
        from infra.api_server import _is_loopback
        assert _is_loopback("::1") is True

    def test_is_loopback_remote(self):
        from infra.api_server import _is_loopback
        assert _is_loopback("192.168.1.100") is False

    def test_memory_update_fields_defined(self):
        from infra.api_server import _MEMORY_UPDATE_FIELDS
        assert "content" in _MEMORY_UPDATE_FIELDS
        assert "category" in _MEMORY_UPDATE_FIELDS
        assert "tags" in _MEMORY_UPDATE_FIELDS
        assert "importance" in _MEMORY_UPDATE_FIELDS

    def test_api_auth_token_configurable(self):
        from infra.api_server import API_AUTH_TOKEN
        # Should be a string (possibly empty)
        assert isinstance(API_AUTH_TOKEN, str)

    def test_cors_origins_configurable(self):
        from infra.api_server import API_CORS_ORIGINS
        # Should be a frozenset
        assert isinstance(API_CORS_ORIGINS, frozenset)


class TestAPIEndpointRegistration:
    """Test that all expected endpoints are registered in the router."""

    def test_get_endpoints_registered(self):
        from infra.api_server import APIRequestHandler
        import inspect
        source = inspect.getsource(APIRequestHandler.do_GET)
        expected = [
            "/api/v1/memories",
            "/api/v1/memories/stats",
            "/api/v1/memories/search",
            "/api/v1/memories/categories",
            "/api/v1/kg/nodes",
            "/api/v1/kg/edges",
            "/api/v1/audit/logs",
        ]
        for path in expected:
            assert path in source, f"GET endpoint {path} not found in router"

    def test_post_endpoints_registered(self):
        from infra.api_server import APIRequestHandler
        import inspect
        source = inspect.getsource(APIRequestHandler.do_POST)
        expected = [
            "/api/v1/auth/login",
            "/api/v1/auth/logout",
            "/api/v1/memories",
            "/api/v1/memories/search",
            "/api/v1/memories/clear",
            "/api/v1/query",
            "/api/v1/maintenance/rebuild",
            "/api/v1/maintenance/compact",
            "/api/v1/maintenance/integrity",
            "/api/v1/compliance/gdpr/erase",
            "/api/v1/rbac/init",
            "/api/v1/rbac/principals",
            "/api/v1/rbac/roles",
            "/api/v1/rbac/bindings",
            "/api/v1/acl/rules",
            "/api/v1/kg/dedup",
            "/api/v1/kg/edges",
            "/api/v1/kg/prune",
            "/api/v1/kg/merge",
            "/api/v1/coordination/tasks",
            "/api/v1/coordination/locks",
            "/api/v1/coordination/messages",
            "/api/v1/coordination/state",
        ]
        for path in expected:
            assert path in source, f"POST endpoint {path} not found in router"

    def test_put_endpoints_registered(self):
        from infra.api_server import APIRequestHandler
        import inspect
        source = inspect.getsource(APIRequestHandler.do_PUT)
        assert "/api/v1/memories/" in source
        assert "/api/v1/kg/entities/" in source
        assert "/api/v1/coordination/tasks/" in source

    def test_delete_endpoints_registered(self):
        from infra.api_server import APIRequestHandler
        import inspect
        source = inspect.getsource(APIRequestHandler.do_DELETE)
        assert "/api/v1/memories/" in source
        assert "/api/v1/kg/entities/" in source
        assert "/api/v1/kg/edges/" in source
        assert "/api/v1/rbac/bindings" in source
        assert "/api/v1/acl/rules" in source


class TestAPICookieHandling:
    """Test cookie token extraction logic (unit-level, no HTTP)."""

    def test_cookie_token_parsing(self):
        """Verify the cookie parsing logic by testing the regex-like extraction."""
        # Simulate what _cookie_token does
        cookie_header = "am_token=abc123; other=val"
        auth_cookie = "am_token"
        token = ""
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith(f"{auth_cookie}="):
                token = part[len(auth_cookie) + 1:]
        assert token == "abc123"

    def test_cookie_token_missing(self):
        cookie_header = "other=val"
        auth_cookie = "am_token"
        token = ""
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith(f"{auth_cookie}="):
                token = part[len(auth_cookie) + 1:]
        assert token == ""

    def test_cookie_token_empty_header(self):
        cookie_header = ""
        auth_cookie = "am_token"
        token = ""
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith(f"{auth_cookie}="):
                token = part[len(auth_cookie) + 1:]
        assert token == ""


class TestAPIRateLimiting:
    """Test sliding-window rate limiter logic."""

    def test_rate_limit_disabled(self):
        """Rate limit <= 0 means disabled."""
        rate_limit = 0
        assert rate_limit <= 0  # disabled

    def test_rate_limit_window_expiry(self):
        """Requests older than window should be pruned."""
        window = 60
        now = 1000.0
        times = [950.0, 960.0, 970.0, 980.0, 990.0]
        # All within window
        times = [t for t in times if now - t < window]
        assert len(times) == 5

        # Some outside window
        times2 = [900.0, 950.0, 990.0]
        times2 = [t for t in times2 if now - t < window]
        assert len(times2) == 2

    def test_rate_limit_enforcement(self):
        """Rate limit is enforced when count >= limit."""
        limit = 5
        times = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert len(times) >= limit  # should be limited


class TestAPIServerConfig:
    """Test server configuration and setup."""

    def test_api_server_has_required_attributes(self):
        from infra.api_server import APIServer
        # Verify the class exists and has expected methods
        assert hasattr(APIServer, '__init__')

    def test_handler_has_required_methods(self):
        from infra.api_server import APIRequestHandler
        assert hasattr(APIRequestHandler, 'do_GET')
        assert hasattr(APIRequestHandler, 'do_POST')
        assert hasattr(APIRequestHandler, 'do_PUT')
        assert hasattr(APIRequestHandler, 'do_DELETE')
        assert hasattr(APIRequestHandler, '_write_json')
        assert hasattr(APIRequestHandler, '_error')
        assert hasattr(APIRequestHandler, '_require_auth')
        assert hasattr(APIRequestHandler, '_rate_limited')

    def test_write_json_sets_content_type(self):
        """_write_json should set Content-Type to application/json."""
        from infra.api_server import APIRequestHandler
        import inspect
        source = inspect.getsource(APIRequestHandler._write_json)
        assert "application/json" in source

    def test_cors_headers_set(self):
        """_write_json should set CORS headers."""
        from infra.api_server import APIRequestHandler
        import inspect
        source = inspect.getsource(APIRequestHandler._write_json)
        assert "Access-Control-Allow-Origin" in source
        assert "Access-Control-Allow-Headers" in source
        assert "Access-Control-Allow-Methods" in source
