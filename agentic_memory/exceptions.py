"""Typed exception hierarchy for the agentic-memory SDK."""

from __future__ import annotations


class AgenticMemoryError(Exception):
    """Base exception for all agentic-memory SDK errors."""


class ConnectionError(AgenticMemoryError):
    """Database connection failed or pool exhausted."""


class NotFoundError(AgenticMemoryError):
    """Requested note, entity, or fact does not exist."""


class ValidationError(AgenticMemoryError):
    """Input validation failed (bad category, missing content, etc.)."""


class IntegrityError(AgenticMemoryError):
    """Database integrity check failed."""


class MaintenanceError(AgenticMemoryError):
    """Maintenance operation failed (rebuild, compact, etc.)."""


class SyncError(AgenticMemoryError):
    """Multi-agent sync or CRDT operation failed."""


class PermissionError(AgenticMemoryError):
    """Operation not allowed in current context."""


class CircuitBreakerOpen(AgenticMemoryError):
    """Maintenance operation blocked by open circuit breaker."""


class ConfigError(AgenticMemoryError):
    """Configuration resolution failed."""
