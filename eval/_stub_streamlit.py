"""Minimal streamlit stub for tests that import dashboard.api_client.

Only the attributes touched at import time (dashboard/__init__.py) are needed:
cache_resource / cache_data decorators, and a session_state object. All other
streamlit UI calls are irrelevant to the api_client unit tests.
"""
from __future__ import annotations


def _identity_decorator(func=None, **_kwargs):
    if func is None:
        return _identity_decorator
    return func


cache_resource = _identity_decorator
cache_data = _identity_decorator


class _SessionState(dict):
    """Dict-like session_state; supports attribute-style access for convenience."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value

    def __delattr__(self, name):
        try:
            del self[name]
        except KeyError:
            raise AttributeError(name)


session_state = _SessionState()
