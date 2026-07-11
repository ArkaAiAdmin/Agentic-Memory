"""MCP tool authorization middleware.

Phase 1: token-based principal resolution via static config mapping.
Phase 2: JWT validation via Authlib (future).

Design principles:
  - Fail-open on errors (backward compat with unauthenticated deployments).
  - Every authorization decision is logged for audit.
  - If no principal is resolved, all operations are allowed (unauthenticated mode).
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

    Returns ``None`` if:
      - No token provided.
      - Token does not map to any principal (unknown token).
      - Database is unavailable.

    Returns ``None`` for the legacy ``MEMORY_API_TOKEN`` so that
    existing single-token deployments continue to work without RBAC.
    """
    if not token:
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

        with open_db(Path(db_path), timeout=5.0) as conn:
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

def mcp_authorize(
    principal_id: str | None,
    action: str,
    resource: str = "memory",
    db_path: str | None = None,
) -> bool:
    """Check authorization for an MCP tool invocation.

    Returns ``True`` if:
      - No principal is resolved (backward compat / unauthenticated mode).
      - No db_path provided (RBAC tables unavailable).
      - The RBAC check grants access.

    Returns ``False`` only when RBAC explicitly denies.

    Fail-open: any exception during the check results in ``True``
    (allow) to avoid breaking existing deployments.
    """
    # No principal = unauthenticated mode = allow all
    if not principal_id:
        return True

    # No DB = no RBAC enforcement (backward compat)
    if not db_path:
        return True

    try:
        from pathlib import Path
        from infra.db import open_db

        with open_db(Path(db_path), timeout=5.0) as conn:
            result = check_permission(conn, principal_id, resource, action)  # type: ignore[arg-type]
            if not result:
                logger.warning(
                    "AUTH DENIED: principal=%s action=%s resource=%s",
                    principal_id,
                    action,
                    resource,
                )
            return result
    except Exception as exc:
        # Fail-open for backward compatibility
        logger.warning("mcp_authorize failed (fail-open): %s", exc)
        return True


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
) -> None:
    """Record an authorization decision in ``memory_audit_log``.

    Best-effort: failures are logged at debug level and never raised.
    """
    if not db_path:
        return
    try:
        import time
        from pathlib import Path
        from infra.db import open_db

        with open_db(Path(db_path), timeout=5.0) as conn:
            conn.execute(
                "INSERT INTO memory_audit_log "
                "(tool_name, args_json, status, duration_ms, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (
                    "rbac_authorize",
                    __import__("json").dumps({
                        "principal_id": principal_id or "anonymous",
                        "action": action,
                        "resource": resource,
                        "allowed": allowed,
                        "note_id": note_id,
                    }),
                    "ok" if allowed else "denied",
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("log_authorization_decision failed: %s", exc)


# ---------------------------------------------------------------------------
# Authorizer class (object-oriented wrapper around mcp_authorize)
# ---------------------------------------------------------------------------


class Authorizer:
    """Thin object wrapper used by callers that prefer an instance API.

    Phase 1 RBAC: resolves authorization decisions via :func:`mcp_authorize`.
    The fail-open contract of :func:`mcp_authorize` is preserved — a missing
    principal or unavailable DB always allows (backward compat).
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

        Mirrors :func:`mcp_authorize`: a ``None`` principal or ``None`` DB
        path yields ``True``; only an explicit RBAC denial returns ``False``.
        """
        return mcp_authorize(
            principal_id=principal_id,
            action=action,
            resource=resource,
            db_path=self.db_path,
        )
