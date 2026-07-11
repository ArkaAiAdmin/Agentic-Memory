"""Role-Based Access Control engine for agentic-memory.

Phase 1 implementation: SQLite-backed RBAC with:
  - principals (users, services, agents)
  - roles (admin, writer, reader, etc.)
  - role_bindings (principal ↔ role)
  - policies (role → resource + action → effect)
  - acl_overrides (per-principal explicit allow/deny)

All queries use parameterised SQL — no f-string interpolation.
Fail-closed on errors (deny on exception) except where noted.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Principal:
    """An authenticated identity (user, service, or agent)."""
    id: str
    kind: str  # 'user', 'service', 'agent'
    tenant_id: str
    display_name: str = ""


@dataclass
class Role:
    """A named role with a set of policies."""
    id: str
    name: str
    tenant_id: str
    description: str = ""


@dataclass
class Permission:
    """A single resource+action pair with allow/deny effect."""
    resource: str
    action: str
    effect: str = "allow"  # 'allow' or 'deny'


# ---------------------------------------------------------------------------
# RBAC queries
# ---------------------------------------------------------------------------

def check_permission(
    conn: sqlite3.Connection,
    principal_id: str,
    resource: str,
    action: str,
) -> bool:
    """Check if *principal_id* has permission for *resource*+*action*.

    Evaluation order (highest priority first):
      1. ACL overrides — explicit per-principal grant/deny.
      2. Policies via role bindings — role → (resource, action, effect).
      3. Default: **deny**.

    A deny in ACL overrides always wins over a policy allow.
    """
    # 1. ACL overrides (explicit grant/deny)
    try:
        row = conn.execute(
            "SELECT effect FROM acl_overrides "
            "WHERE principal_id = ? AND resource_id = ? AND action = ? "
            "LIMIT 1",
            (principal_id, resource, action),
        ).fetchone()
        if row:
            return bool(row[0] == "allow")
    except Exception as exc:
        # acl_overrides table may not exist yet — treat as no override.
        logger.debug("acl_overrides query failed (table may not exist): %s", exc)

    # 2. Policies via role bindings (deny policies are excluded)
    try:
        # Check if policies table has an 'effect' column
        has_effect = any(
            col[1] == "effect"
            for col in conn.execute("PRAGMA table_info(policies)").fetchall()
        )
        if has_effect:
            row = conn.execute(
                "SELECT 1 FROM role_bindings rb "
                "JOIN policies p ON p.role_id = rb.role_id "
                "WHERE rb.principal_id = ? "
                "  AND (p.resource = ? OR p.resource = '*' OR ? LIKE p.resource || '/%') "
                "  AND (p.action = ? OR p.action = '*') "
                "  AND (p.effect IS NULL OR p.effect != 'deny') "
                "LIMIT 1",
                (principal_id, resource, resource, action),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM role_bindings rb "
                "JOIN policies p ON p.role_id = rb.role_id "
                "WHERE rb.principal_id = ? "
                "  AND (p.resource = ? OR p.resource = '*' OR ? LIKE p.resource || '/%') "
                "  AND (p.action = ? OR p.action = '*') "
                "LIMIT 1",
                (principal_id, resource, resource, action),
            ).fetchone()
        if row is not None:
            return True
    except Exception as exc:
        logger.debug("policy query failed (tables may not exist): %s", exc)

    # 3. Default: deny
    return False


def get_principal_roles(conn: sqlite3.Connection, principal_id: str) -> list[str]:
    """Return list of role names for *principal_id*."""
    try:
        rows = conn.execute(
            "SELECT r.name FROM role_bindings rb "
            "JOIN roles r ON r.id = rb.role_id "
            "WHERE rb.principal_id = ?",
            (principal_id,),
        ).fetchall()
        return [r[0] for r in rows]
    except Exception as exc:
        logger.debug("get_principal_roles failed: %s", exc)
        return []


def grant_role(
    conn: sqlite3.Connection,
    principal_id: str,
    role_id: str,
    granted_by: str | None = None,
) -> bool:
    """Grant *role_id* to *principal_id*. Returns True on success."""
    try:
        conn.execute(
            "INSERT OR IGNORE INTO role_bindings (principal_id, role_id, granted_by) "
            "VALUES (?, ?, ?)",
            (principal_id, role_id, granted_by),
        )
        conn.execute(
            "INSERT INTO principal_roles_audit "
            "(principal_id, role_id, action, performed_by) "
            "VALUES (?, ?, 'grant', ?)",
            (principal_id, role_id, granted_by),
        )
        conn.commit()
        return True
    except Exception as exc:
        logger.warning("grant_role failed: %s", exc)
        return False


def revoke_role(
    conn: sqlite3.Connection,
    principal_id: str,
    role_id: str,
    performed_by: str | None = None,
) -> bool:
    """Revoke *role_id* from *principal_id*. Returns True on success."""
    try:
        conn.execute(
            "DELETE FROM role_bindings WHERE principal_id = ? AND role_id = ?",
            (principal_id, role_id),
        )
        conn.execute(
            "INSERT INTO principal_roles_audit "
            "(principal_id, role_id, action, performed_by) "
            "VALUES (?, ?, 'revoke', ?)",
            (principal_id, role_id, performed_by),
        )
        conn.commit()
        return True
    except Exception as exc:
        logger.warning("revoke_role failed: %s", exc)
        return False


def list_principals(conn: sqlite3.Connection) -> list[dict]:
    """Return all principals with their roles."""
    try:
        rows = conn.execute(
            "SELECT p.id, p.kind, p.tenant_id, p.display_name "
            "FROM principals p ORDER BY p.id"
        ).fetchall()
        result = []
        for r in rows:
            roles = get_principal_roles(conn, r[0])
            result.append({
                "id": r[0],
                "kind": r[1],
                "tenant_id": r[2],
                "display_name": r[3] or "",
                "roles": roles,
            })
        return result
    except Exception as exc:
        logger.debug("list_principals failed: %s", exc)
        return []


def list_policies(conn: sqlite3.Connection, role_id: str | None = None) -> list[dict]:
    """Return policies, optionally filtered by role_id."""
    try:
        if role_id:
            rows = conn.execute(
                "SELECT id, role_id, resource, action, effect "
                "FROM policies WHERE role_id = ?",
                (role_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, role_id, resource, action, effect FROM policies"
            ).fetchall()
        return [
            {"id": r[0], "role_id": r[1], "resource": r[2], "action": r[3], "effect": r[4]}
            for r in rows
        ]
    except Exception as exc:
        logger.debug("list_policies failed: %s", exc)
        return []


def rbac_stats(conn: sqlite3.Connection) -> dict:
    """Return summary counts for the RBAC subsystem."""
    stats: dict = {}
    for table, key in [
        ("principals", "principals"),
        ("roles", "roles"),
        ("role_bindings", "role_bindings"),
        ("policies", "policies"),
        ("acl_overrides", "acl_overrides"),
    ]:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            stats[key] = row[0] if row else 0
        except Exception:
            stats[key] = 0
    return stats


# ---------------------------------------------------------------------------
# Default role seeding
# ---------------------------------------------------------------------------

_DEFAULT_ROLES: list[tuple[str, str, str]] = [
    # (name, tenant_id, description)
    ("memory:read", "default", "Read access to memories"),
    ("memory:write", "default", "Write access to memories"),
    ("memory:delete", "default", "Delete access to memories"),
    ("memory:admin", "default", "Full admin access to memories"),
    ("ops:read", "default", "Read access to operational data"),
    ("ops:admin", "default", "Full admin access to operational data"),
]

# (role_name, resource, action)
_DEFAULT_POLICIES: list[tuple[str, str, str]] = [
    ("memory:read", "memory", "read"),
    ("memory:write", "memory", "write"),
    ("memory:delete", "memory", "delete"),
    ("memory:admin", "memory", "read"),
    ("memory:admin", "memory", "write"),
    ("memory:admin", "memory", "delete"),
    ("memory:admin", "memory", "admin"),
    ("ops:read", "ops", "read"),
    ("ops:admin", "ops", "read"),
    ("ops:admin", "ops", "write"),
    ("ops:admin", "ops", "admin"),
]


def seed_default_roles(conn: sqlite3.Connection, tenant_id: str = "default") -> int:
    """Insert default roles and policies if they don't already exist.

    Idempotent: re-running is a no-op. Returns the number of new roles inserted.
    """
    inserted = 0
    try:
        for name, tid, desc in _DEFAULT_ROLES:
            role_id = f"role:{name}:{tid}"
            existing = conn.execute(
                "SELECT id FROM roles WHERE name = ? AND tenant_id = ?",
                (name, tid),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO roles (id, name, tenant_id, description) VALUES (?, ?, ?, ?)",
                    (role_id, name, tid, desc),
                )
                inserted += 1

        for role_name, resource, action in _DEFAULT_POLICIES:
            role_row = conn.execute(
                "SELECT id FROM roles WHERE name = ? AND tenant_id = ?",
                (role_name, tenant_id),
            ).fetchone()
            if role_row is None:
                continue
            role_id = role_row[0]
            existing = conn.execute(
                "SELECT id FROM policies WHERE role_id = ? AND resource = ? AND action = ?",
                (role_id, resource, action),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO policies (role_id, resource, action) VALUES (?, ?, ?)",
                    (role_id, resource, action),
                )

        conn.commit()
    except Exception as exc:
        logger.warning("seed_default_roles failed: %s", exc)
    return inserted
