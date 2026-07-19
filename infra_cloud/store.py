"""Cloud state store — provisioning + billing metadata for the SaaS plane.

This is a SEPARATE database (cloud_state.db) from any per-deployment
memory.db. It stores only:
  - customers
  - deployments  (deployment_id -> tenant_id mapping; the routing backbone)
  - subscriptions
  - invoices

It uses its own numbered migration set under infra_cloud/migrations/ with
down-scripts, mirroring the core's Hard Rule 4 (no ALTER TABLE in Python;
migrations only).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_CLOUD_SCHEMA_VERSION = 1


def _discover_migrations() -> list[tuple[int, Path]]:
    """Return sorted (number, path) for up-migrations (exclude .down.sql)."""
    out: list[tuple[int, Path]] = []
    if not _MIGRATIONS_DIR.exists():
        return out
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if path.name.endswith(".down.sql"):
            continue
        name = path.name.split("_", 1)[0]
        if not name.isdigit():
            continue
        out.append((int(name), path))
    return sorted(out)


def _get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def run_cloud_migrations(db_path: str | Path, dry_run: bool = False) -> int:
    """Apply pending cloud_state migrations. Returns the new schema version.

    Uses SQLite ``user_version`` to track applied migrations instead of a
    schema_version table, keeping the management DB self-describing.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        current = _get_user_version(conn)
        pending = [(n, p) for n, p in _discover_migrations() if n > current]
        if not pending:
            return current
        for num, path in pending:
            sql = path.read_text()
            logger.info("cloud migration %s: %s", num, path.name)
            if dry_run:
                continue
            conn.executescript(sql)
            _set_user_version(conn, num)
        return _get_user_version(conn)
    finally:
        conn.close()


class CloudStateStore:
    """CRUD access to the cloud_state.db provisioning/billing metadata."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        run_cloud_migrations(self.db_path)

    # ── low-level ──────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {k: row[k] for k in row.keys()}

    # ── customers ──────────────────────────────────────────────────────────

    def create_customer(self, customer_id: str, email: str, name: str | None = None) -> dict:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO customers (customer_id, email, name, created_at, status) "
                "VALUES (?, ?, ?, ?, 'active')",
                (customer_id, email, name, time.time()),
            )
        return self.get_customer(customer_id)

    def get_customer(self, customer_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM customers WHERE customer_id = ?", (customer_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_customers(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM customers ORDER BY created_at DESC").fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── deployments ──────────────────────────────────────────────────────────

    def create_deployment(
        self,
        deployment_id: str,
        customer_id: str,
        tenant_id: str,
        label: str | None = None,
        db_path: str | None = None,
        api_base: str | None = None,
    ) -> dict:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO deployments "
                "(deployment_id, customer_id, tenant_id, label, db_path, api_base, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
                (deployment_id, customer_id, tenant_id, label, db_path, api_base, time.time()),
            )
        return self.get_deployment(deployment_id)

    def get_deployment(self, deployment_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE deployment_id = ?", (deployment_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def resolve_tenant(self, deployment_id: str) -> Optional[str]:
        """Routing backbone: map a deployment_id to its tenant_id."""
        dep = self.get_deployment(deployment_id)
        return dep["tenant_id"] if dep else None

    def list_deployments(self, customer_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if customer_id:
                rows = conn.execute(
                    "SELECT * FROM deployments WHERE customer_id = ? ORDER BY created_at DESC",
                    (customer_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM deployments ORDER BY created_at DESC"
                ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def set_deployment_status(self, deployment_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE deployments SET status = ? WHERE deployment_id = ?",
                (status, deployment_id),
            )

    # ── subscriptions ──────────────────────────────────────────────────────

    def create_subscription(
        self,
        subscription_id: str,
        deployment_id: str,
        plan_id: str,
        stripe_sub_id: str | None = None,
        status: str = "active",
        current_period_end: float | None = None,
    ) -> dict:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO subscriptions "
                "(subscription_id, deployment_id, plan_id, stripe_sub_id, status, current_period_end, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (subscription_id, deployment_id, plan_id, stripe_sub_id, status,
                 current_period_end, time.time()),
            )
        return self.get_subscription(subscription_id)

    def get_subscription(self, subscription_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_subscriptions(self, deployment_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE deployment_id = ? ORDER BY created_at DESC",
                (deployment_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── invoices ───────────────────────────────────────────────────────────

    def create_invoice(
        self,
        invoice_id: str,
        customer_id: str,
        amount_cents: int,
        subscription_id: str | None = None,
        currency: str = "usd",
        status: str = "open",
    ) -> dict:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO invoices "
                "(invoice_id, customer_id, subscription_id, amount_cents, currency, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (invoice_id, customer_id, subscription_id, amount_cents, currency, status, time.time()),
            )
        return self.get_invoice(invoice_id)

    def get_invoice(self, invoice_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_invoices(self, customer_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if customer_id:
                rows = conn.execute(
                    "SELECT * FROM invoices WHERE customer_id = ? ORDER BY created_at DESC",
                    (customer_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM invoices ORDER BY created_at DESC"
                ).fetchall()
        return [self._row_to_dict(r) for r in rows]
