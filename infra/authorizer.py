"""MCP tool authorization middleware.

Phase 1: token-based principal resolution via static config mapping.
Phase 2: JWT validation via Authlib (future).

Design principles:
  - **Fail-CLOSED by default** (``MEMORY_AUTH_MODE="closed"``): an unresolved
    principal, a missing DB, or any auth-resolution error DENIES access. This is
    the compliant posture for SOC2/HIPAA deployments.
  - **Opt-in fail-open** for legacy/unauthenticated deployments: set
    ``MEMORY_AUTH_MODE="open"`` (env var) to restore the pre-RBAC behavior.
    The test suite sets this so functional tests stay green; production MUST
    leave it closed.
  - Every authorization decision is logged for audit.
  - Tenant-scoped resources (``gdpr-erase``, ``memory:delete``, ...) require the
    principal's ``tenant_id`` to match the resource tenant, unless the principal
    holds a cross-tenant admin role.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from infra.rbac import Principal, check_permission

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Principal resolution
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Static principal config from memory.toml (cached)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_principal_config() -> dict[str, str]:
    """Load the ``[api.principals]`` mapping from ``memory.toml``.

    Returns a dict of ``{token: "kind:id"}``.  Cached with ``maxsize=1``
    since the static config changes rarely.  Clear the cache manually
    via ``_load_principal_config.cache_clear()`` if hot-reload is needed.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            logger.debug("tomllib not available — skipping [api.principals]")
            return {}

    from pathlib import Path

    # Same resolution logic as infra.config._resolve_toml_path
    override = os.environ.get("MEMORY_CONFIG_PATH")
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = (Path(__file__).resolve().parent.parent / p).resolve()
        toml_path = p
    else:
        toml_path = Path(__file__).resolve().parent.parent / "memory.toml"

    if not toml_path.exists():
        return {}

    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
        principals = data.get("api", {}).get("principals", {})
        if not isinstance(principals, dict):
            return {}
        return {str(k): str(v) for k, v in principals.items()}
    except Exception:
        logger.debug("Failed to parse [api.principals] from %s", toml_path)
        return {}


# ---------------------------------------------------------------------------
# Principal resolution
# ---------------------------------------------------------------------------


def resolve_principal(
    db_path: str | None = None,
    token: str | None = None,
) -> Principal | None:
    """Resolve a bearer token to a :class:`Principal`.

    Phase 1 (current):
      - Checks ``MEMORY_API_TOKEN`` for backward compat (returns None —
        the legacy token is not tied to a principal).
      - Checks static config mapping from ``memory.toml`` ``[api.principals]``.
      - Falls through to ``principals`` table lookup via
        ``principal_identities.external_sub``.
      - Falls back to ``MEMORY_AGENT_ID`` env var for local/stdio
        deployments where no token is configured.

    Returns ``None`` if:
      - No token provided AND no MEMORY_AGENT_ID is set.
      - Token does not map to any principal (unknown token).
      - Database is unavailable.

    Returns ``None`` for the legacy ``MEMORY_API_TOKEN`` so that
    existing single-token deployments continue to work without RBAC.
    """
    if not token:
        # Local/stdio fallback: auto-resolve a default principal from
        # MEMORY_AGENT_ID so closed-mode works without token config.
        local_id = os.environ.get("MEMORY_AGENT_ID", "")
        if local_id:
            return Principal(
                id=local_id.lower(),
                kind="local",
                tenant_id="default",
                display_name=local_id,
            )
        return None

    # Backward compat: if the token matches the legacy API token,
    # return None so RBAC is not enforced.  The legacy token grants
    # full access (same as before RBAC existed).
    legacy_token = os.environ.get("MEMORY_API_TOKEN", "")
    if legacy_token and token == legacy_token:
        return None

    # --- Static config mapping (Phase 1 RBAC — no SSO) ---
    mapping = _load_principal_config()
    entry = mapping.get(token)
    if entry:
        kind, _, pid = entry.partition(":")
        if kind and pid:
            logger.debug(
                "resolve_principal: config-mapped token -> %s:%s", kind, pid
            )
            return Principal(
                id=pid,
                kind=kind,
                tenant_id="default",
                display_name=pid,
            )
        else:
            logger.debug(
                "resolve_principal: malformed config entry %r, "
                "expected 'kind:id'",
                entry,
            )

    if not db_path:
        return None

    try:
        from pathlib import Path
        from infra.db import open_db

        with open_db(Path(db_path), timeout=5.0, write=False) as conn:
            row = conn.execute(
                "SELECT p.id, p.kind, p.tenant_id, p.display_name "
                "FROM principals p "
                "JOIN principal_identities pi ON pi.principal_id = p.id "
                "WHERE pi.external_sub = ? "
                "LIMIT 1",
                (token,),
            ).fetchone()
            if row:
                return Principal(
                    id=row[0],
                    kind=row[1],
                    tenant_id=row[2],
                    display_name=row[3] or "",
                )
    except Exception as exc:
        # Table may not exist yet (pre-migration). Treat as no principal.
        logger.debug("resolve_principal failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Authorization check
# ---------------------------------------------------------------------------

def _auth_mode() -> str:
    """Return the configured auth mode: ``"closed"`` (default, secure) or ``"open"``."""
    return os.environ.get("MEMORY_AUTH_MODE", "closed").strip().lower()


# ---------------------------------------------------------------------------
# Action / resource normalization
# ---------------------------------------------------------------------------
# The MCP verb layer speaks a richer vocabulary (search, share, save, ...) than
# the RBAC policy schema, whose ``policies.action`` column is constrained to
# ``CHECK(action IN ('read','write','delete','admin','export'))``. Verb actions
# such as ``search`` or ``share`` can therefore never be stored as a policy and
# would be denied for every principal. Normalizing verb actions/resources to the
# canonical RBAC vocabulary here (the single enforcement choke point) lets the
# verb layer authorize correctly without weakening RBAC: canonical inputs pass
# through unchanged, so direct callers (adversarial tests) are unaffected.

_CANONICAL_ACTIONS = frozenset({"read", "write", "delete", "admin", "export"})

_ACTION_ALIASES = {
    "search": "read",
    "recall": "read",
    "list": "read",
    "get": "read",
    "save": "write",
    "update": "write",
    "patch": "write",
    "supersede": "write",
    "revert_supersede": "write",
    "share": "write",
    "purge": "delete",
}

_RESOURCE_ALIASES = {
    "maintenance": "ops",
}


def _normalize_action(action: str) -> str:
    """Map a verb-layer action to the canonical RBAC action vocabulary."""
    a = (action or "").strip().lower()
    if a in _CANONICAL_ACTIONS:
        return a
    return _ACTION_ALIASES.get(a, a)


def _normalize_resource(resource: str) -> str:
    """Map a verb-layer resource to the canonical RBAC resource vocabulary."""
    r = (resource or "").strip().lower()
    return _RESOURCE_ALIASES.get(r, r)


def _is_cross_tenant_admin(conn, principal_id) -> bool:
    """True if *principal_id* holds a role granting cross-tenant admin.

    Used to allow compliance/ops administrators to act outside their own
    tenant (e.g. a global compliance officer performing a GDPR erase for any
    tenant). Without this, tenant scoping would wrongly block legitimate
    administrators.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM role_bindings rb "
            "JOIN roles r ON r.id = rb.role_id "
            "WHERE rb.principal_id = ? "
            "  AND r.name IN ('memory:admin', 'ops:admin') "
            "LIMIT 1",
            (principal_id,),
        ).fetchone()
        return row is not None
    except Exception as exc:
        logger.debug("_is_cross_tenant_admin failed: %s", exc)
        return False


def _principal_tenant(conn, principal_id) -> str:
    """Return the tenant_id for *principal_id*, defaulting to ``"default"``."""
    try:
        row = conn.execute(
            "SELECT tenant_id FROM principals WHERE id = ? LIMIT 1",
            (principal_id,),
        ).fetchone()
        return row[0] if row else "default"
    except Exception as exc:
        logger.debug("_principal_tenant failed: %s", exc)
        return "default"


def resolve_tenant_for_principal(principal_id: str | None, db_path: str | None = None) -> str:
    """Resolve tenant_id from principal_id for MCP tool use.

    Sprint 3.2: Used by MCP tools to bind the correct tenant_id
    based on the authenticated principal. Falls back to "default"
    when no principal is resolved or RBAC is disabled.
    """
    if not principal_id:
        return "default"
    if db_path is None:
        from memory_config import resolve_db_path
        db_path = str(resolve_db_path())
    try:
        from pathlib import Path
        from infra.db import open_db
        with open_db(Path(db_path), timeout=5.0) as conn:
            return _principal_tenant(conn, principal_id)
    except Exception:
        return "default"


def mcp_authorize(
    principal_id: str | None,
    action: str,
    resource: str = "memory",
    db_path: str | None = None,
    *,
    tenant_id: str | None = None,
) -> bool:
    """Check authorization for an MCP tool invocation.

    **Fail-closed by default** (``MEMORY_AUTH_MODE="closed"``):
      - No principal resolved  -> DENY (unauthenticated deployments must opt in
        with ``MEMORY_AUTH_MODE="open"``).
      - No db_path / RBAC tables unavailable -> DENY.
      - Any exception during the check -> DENY.

    In ``"open"`` mode (legacy/unauthenticated), the pre-RBAC behavior is
    preserved: missing principal/DB or errors -> ALLOW.

    Tenant scoping: when *tenant_id* (the resource's tenant) is provided and the
    principal's tenant differs, access is denied unless the principal holds a
    cross-tenant admin role (see :func:`_is_cross_tenant_admin`).
    """
    open_mode = _auth_mode() == "open"
    action = _normalize_action(action)
    resource = _normalize_resource(resource)

    # No principal (None): deny in closed mode, allow in open mode for
    # backward compat with legacy single-token deployments.
    if principal_id is None:
        if open_mode:
            return True
        logger.warning("AUTH DENIED: no principal resolved (mode=closed)")
        return False

    # Empty-string principal_id is never valid — deny unconditionally.
    if not principal_id:
        logger.warning("AUTH DENIED: empty principal_id")
        return False

    # No DB: cannot enforce RBAC -> deny in closed mode.
    if not db_path:
        if open_mode:
            return True
        logger.warning("AUTH DENIED: no db_path for RBAC enforcement (mode=closed)")
        return False

    try:
        from pathlib import Path
        from infra.db import open_db

        # Auth checks are read-only — use write=False to acquire a shared
        # lock instead of competing with the write queue's exclusive flock.
        with open_db(Path(db_path), timeout=5.0, write=False) as conn:
            from infra.db import ProxyConnection
            # ProxyConnection relays .execute() through the write-queue at runtime,
            # so check_permission works against it; mypy just can't see the alias.
            if isinstance(conn, ProxyConnection):
                allowed = check_permission(conn, principal_id, resource, action)  # type: ignore[arg-type]
            else:
                allowed = check_permission(conn, principal_id, resource, action)
            if not allowed:
                logger.warning(
                    "AUTH DENIED: principal=%s action=%s resource=%s",
                    principal_id,
                    action,
                    resource,
                )
                return False

            # Tenant scoping for tenant-bound resources.
            if tenant_id is not None:
                p_tenant = _principal_tenant(conn, principal_id)
                if p_tenant != tenant_id and not _is_cross_tenant_admin(conn, principal_id):
                    logger.warning(
                        "AUTH DENIED (tenant scope): principal=%s tenant=%s != resource=%s",
                        principal_id,
                        p_tenant,
                        tenant_id,
                    )
                    return False
            return True
    except Exception as exc:
        # Fail-closed (or fail-open in opt-in mode) on resolution errors.
        logger.warning(
            "mcp_authorize failed (fail-%s): %s", "open" if open_mode else "closed", exc
        )
        return open_mode


# ---------------------------------------------------------------------------
# Audit logging helper
# ---------------------------------------------------------------------------

def log_authorization_decision(
    principal_id: str | None,
    action: str,
    resource: str,
    allowed: bool,
    *,
    note_id: str | None = None,
    db_path: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Record an authorization decision in ``memory_audit_log``.

    Best-effort: failures are logged at debug level and never raised.
    """
    if not db_path:
        return
    try:
        import json
        import time
        from pathlib import Path
        from infra.db_write_queue import sqlite_write_queue

        future = sqlite_write_queue.enqueue_write(
            Path(db_path),
            "INSERT INTO memory_audit_log "
            "(tool_name, args_json, status, duration_ms, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (
                "rbac_authorize",
                json.dumps({
                    "principal_id": principal_id or "anonymous",
                    "tenant_id": tenant_id or "default",
                    "action": action,
                    "resource": resource,
                    "allowed": allowed,
                    "note_id": note_id,
                }),
                "ok" if allowed else "denied",
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
        )
        future.result(timeout=5.0)
    except Exception as exc:
        logger.debug("log_authorization_decision failed: %s", exc)


# ---------------------------------------------------------------------------
# Authorizer class (object-oriented wrapper around mcp_authorize)
# ---------------------------------------------------------------------------


class Authorizer:
    """Thin object wrapper used by callers that prefer an instance API.

    Phase 1 RBAC: resolves authorization decisions via :func:`mcp_authorize`.
    The fail-closed contract is enforced — a missing principal or
    unavailable DB always denies (no silent allow on error).
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path

    def check(
        self,
        *,
        principal_id: str | None,
        resource: str,
        action: str,
    ) -> bool:
        """Return ``True`` if *principal_id* may perform *action* on *resource*.

        Mirrors :func:`mcp_authorize`: fail-closed — a ``None`` principal
        or missing DB denies (no silent allow). Only an explicit RBAC
        denial (policy check fails or tenant mismatch) returns ``False``.
        """
        return mcp_authorize(
            principal_id=principal_id,
            action=action,
            resource=resource,
            db_path=self.db_path,
        )
