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
_CLOUD_SCHEMA_VERSION = 4


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

    def create_customer(self, customer_id: str, email: str, name: str | None = None) -> Optional[dict]:
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
    ) -> Optional[dict]:
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
    ) -> Optional[dict]:
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
    ) -> Optional[dict]:
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

    # ── plans & usage (Phase 5) ────────────────────────────────────────────

    def list_plans(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM plans ORDER BY max_storage_mb ASC").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_plan(self, plan_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def get_stripe_price_id(self, plan_id: str) -> Optional[str]:
        """Return the Stripe Price ID for a plan, or None if not configured."""
        plan = self.get_plan(plan_id)
        if plan:
            return plan.get("stripe_price_id")
        return None

    def increment_usage(
        self,
        deployment_id: str,
        mcp_calls: int = 0,
        rest_calls: int = 0,
        storage_bytes: int = 0,
        audit_log_bytes: int = 0,
    ) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO usage_records (deployment_id, day, mcp_calls, rest_calls, storage_bytes, audit_log_bytes) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(deployment_id, day) DO UPDATE SET "
                "mcp_calls = mcp_calls + excluded.mcp_calls, "
                "rest_calls = rest_calls + excluded.rest_calls, "
                "storage_bytes = MAX(storage_bytes, excluded.storage_bytes), "
                "audit_log_bytes = MAX(audit_log_bytes, excluded.audit_log_bytes)",
                (deployment_id, day, mcp_calls, rest_calls, storage_bytes, audit_log_bytes),
            )
            conn.commit()

    def check_limit_exceeded(self, deployment_id: str) -> bool:
        """Check if the deployment exceeds plan limits (calls + storage + seats)."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT p.max_mcp_calls_per_day, p.max_storage_mb, p.max_seats "
                "FROM subscriptions s "
                "JOIN plans p ON s.plan_id = p.id "
                "WHERE s.deployment_id = ? AND s.status = 'active' "
                "LIMIT 1",
                (deployment_id,),
            ).fetchone()

            max_calls = row[0] if row else 1000
            max_storage_mb = row[1] if row else 50
            max_seats = row[2] if row else 5

            # Check daily call limit
            usage = conn.execute(
                "SELECT COALESCE(mcp_calls, 0) + COALESCE(rest_calls, 0) "
                "FROM usage_records "
                "WHERE deployment_id = ? AND day = ?",
                (deployment_id, day),
            ).fetchone()
            current_calls = usage[0] if usage else 0
            if current_calls >= max_calls:
                return True

            # Check storage limit
            usage_row = conn.execute(
                "SELECT COALESCE(storage_bytes, 0) "
                "FROM usage_records "
                "WHERE deployment_id = ? AND day = ?",
                (deployment_id, day),
            ).fetchone()
            current_storage_mb = (usage_row[0] if usage_row else 0) / (1024 * 1024)
            if current_storage_mb > max_storage_mb:
                return True

            # Check seat limit (count role_bindings in per-deployment DB)
            dep = self.get_deployment(deployment_id)
            if dep and dep.get("db_path"):
                db_path = dep["db_path"]
                if Path(db_path).exists():
                    import sqlite3 as _sqlite3
                    try:
                        with _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as dep_conn:
                            has_table = dep_conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table' AND name='role_bindings'"
                            ).fetchone()
                            if has_table:
                                seat_count = dep_conn.execute(
                                    "SELECT COUNT(DISTINCT principal_id) FROM role_bindings"
                                ).fetchone()[0]
                                if seat_count > max_seats:
                                    return True
                    except Exception:
                        pass  # seat check is best-effort

            return False

    def get_usage(self, deployment_id: str, day: Optional[str] = None) -> list[dict] | Optional[dict]:
        day_str = day or time.strftime("%Y-%m-%d", time.gmtime())
        with self._connect() as conn:
            if day:
                row = conn.execute(
                    "SELECT * FROM usage_records WHERE deployment_id = ? AND day = ?",
                    (deployment_id, day_str),
                ).fetchone()
                return self._row_to_dict(row) if row else None
            else:
                rows = conn.execute(
                    "SELECT * FROM usage_records WHERE deployment_id = ? ORDER BY day DESC",
                    (deployment_id,),
                ).fetchall()
                return [self._row_to_dict(r) for r in rows]

    def get_seat_count(self, deployment_id: str) -> int:
        """Count distinct principals in role_bindings for a deployment."""
        dep = self.get_deployment(deployment_id)
        if not dep or not dep.get("db_path"):
            return 0
        db_path = dep["db_path"]
        if not Path(db_path).exists():
            return 0
        import sqlite3 as _sqlite3
        try:
            with _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
                has_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='role_bindings'"
                ).fetchone()
                if not has_table:
                    return 0
                row = conn.execute(
                    "SELECT COUNT(DISTINCT principal_id) FROM role_bindings"
                ).fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except Exception:
            return 0

    def sync_usage_from_audit_log(self, audit_db_path: str | Path) -> dict:
        """Read memory_audit_log and sync MCP call counts to usage_records.

        Reads the audit log (which tracks every MCP tool call with timestamps),
        groups by day, and increments ``mcp_calls`` in usage_records for each
        deployment that has an active subscription.

        Returns a summary of what was synced.
        """
        import sqlite3 as _sqlite3
        audit_db = _sqlite3.connect(f"file:{audit_db_path}?mode=ro", uri=True, timeout=10)
        try:
            # Get calls per day (audit log stores ts as unix timestamp)
            rows = audit_db.execute(
                "SELECT DATE(ts, 'unixepoch') as day, COUNT(*) as cnt "
                "FROM memory_audit_log "
                "WHERE ts >= strftime('%s', 'now', '-2 days') "
                "GROUP BY day"
            ).fetchall()
        except _sqlite3.OperationalError:
            # audit_log table doesn't exist yet — nothing to sync
            return {"synced_days": 0, "total_calls": 0}
        finally:
            audit_db.close()

        if not rows:
            return {"synced_days": 0, "total_calls": 0}

        # Map days to call counts
        day_counts = {r[0]: r[1] for r in rows if r[0]}

        # For each deployment with an active subscription, update usage
        total_synced = 0
        with self._connect() as conn:
            active_deps = conn.execute(
                "SELECT DISTINCT deployment_id FROM subscriptions WHERE status = 'active'"
            ).fetchall()
            for (dep_id,) in active_deps:
                for day, cnt in day_counts.items():
                    conn.execute(
                        "INSERT INTO usage_records (deployment_id, day, mcp_calls) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(deployment_id, day) DO UPDATE SET "
                        "mcp_calls = mcp_calls + excluded.mcp_calls",
                        (dep_id, day, cnt),
                    )
                    total_synced += cnt
                conn.commit()

        return {"synced_days": len(day_counts), "total_calls": total_synced}
