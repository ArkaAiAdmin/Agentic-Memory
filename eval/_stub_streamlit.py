"""Minimal streamlit stub for tests that import dashboard.api_client.

Only the attributes touched at import time (dashboard/__init__.py) are needed:
cache_resource / cache_data decorators, and a session_state object. All other
streamlit UI calls are irrelevant to the api_client unit tests.

L3 fix: module-level __getattr__ returns no-op functions for any unknown
attribute (st.write, st.columns, st.sidebar, etc.) so the stub doesn't
break when dashboard code uses additional streamlit APIs.
"""
from __future__ import annotations


def _identity_decorator(func=None, **_kwargs):
    if func is None:
        return _identity_decorator
    return func


def _noop(*_args, **_kwargs):
    """No-op catch-all for any streamlit function not explicitly stubbed."""
    return None


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


def __getattr__(name: str):
    """Module-level fallback: return _noop for any unknown streamlit symbol."""
    return _noop
