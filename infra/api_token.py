"""API token validation helper for agentic-memory.

Validates that API tokens meet minimum security requirements before
they are accepted by the REST server.
"""

from __future__ import annotations

import re

# Minimum entropy: 32 bytes (256 bits) of randomness.
# The default local-dev token in memory.toml is 40 chars, which is above
# this threshold. Operators should replace it with a 32+ byte random
# value for production deployments.
_MIN_TOKEN_BYTES = 32

# Allowed character set: alphanumeric plus common URL-safe symbols.
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9\-_\.]+$")


def validate_api_token(token: str) -> bool:
    """Return True if the token meets minimum security requirements.

    Checks:
    1. Non-empty
    2. Length >= _MIN_TOKEN_BYTES
    3. Contains only URL-safe characters (no whitespace, control chars)
    """
    if not token:
        return False
    if len(token) < _MIN_TOKEN_BYTES:
        return False
    if not _TOKEN_PATTERN.match(token):
        return False
    return True
