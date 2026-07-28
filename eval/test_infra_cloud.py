"""Tests for the Phase 3 management plane (infra_cloud)."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from infra_cloud import CloudStateStore, GatewayRouter, run_cloud_migrations
from infra_cloud.store import _CLOUD_SCHEMA_VERSION


@pytest.fixture
def cloud_db(tmp_path: Path) -> Path:
    db = tmp_path / "cloud_state.db"
    return db


def test_migrations_apply_and_idempotent(cloud_db: Path):
    v1 = run_cloud_migrations(cloud_db)
    assert v1 == _CLOUD_SCHEMA_VERSION
    # Re-running does not error and stays at same version.
    v2 = run_cloud_migrations(cloud_db)
    assert v2 == _CLOUD_SCHEMA_VERSION


def test_customer_deployment_roundtrip(cloud_db: Path):
    store = CloudStateStore(cloud_db)
    cust = store.create_customer("cust_1", "ops@example.com", "Ops Co")
    assert cust["customer_id"] == "cust_1"
    assert cust["email"] == "ops@example.com"

    dep = store.create_deployment(
        "dep_1", "cust_1", "tenant_aa", label="prod", api_base="http://127.0.0.1:9999"
    )
    assert dep["deployment_id"] == "dep_1"
    assert dep["tenant_id"] == "tenant_aa"
    # Routing backbone works.
    assert store.resolve_tenant("dep_1") == "tenant_aa"

    fetched = store.get_deployment("dep_1")
    assert fetched is not None
    assert store.get_deployment("nope") is None

    assert store.list_deployments("cust_1")[0]["deployment_id"] == "dep_1"
    assert store.list_customers()[0]["customer_id"] == "cust_1"


def test_subscription_and_invoice(cloud_db: Path):
    store = CloudStateStore(cloud_db)
    store.create_customer("cust_2", "billing@example.com")
    store.create_deployment("dep_2", "cust_2", "tenant_bb", api_base="http://x")
    sub = store.create_subscription("sub_1", "dep_2", "plan_pro", stripe_sub_id="sub_stripe_9")
    assert sub["plan_id"] == "plan_pro"
    assert store.list_subscriptions("dep_2")[0]["subscription_id"] == "sub_1"

    inv = store.create_invoice("inv_1", "cust_2", 4900, subscription_id="sub_1")
    assert inv["amount_cents"] == 4900
    assert store.list_invoices("cust_2")[0]["invoice_id"] == "inv_1"


class _FakeDeployment(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._send(200, b'{"proxied": true, "path": "' + self.path.encode() + b'"}')

    def log_message(self, *args):  # silence test server logs
        pass


@pytest.fixture
def fake_deployment():
    server = HTTPServer(("127.0.0.1", 0), _FakeDeployment)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_gateway_unknown_deployment(cloud_db: Path):
    store = CloudStateStore(cloud_db)
    router = GatewayRouter(store)
    out = router.route("ghost", "memories/stats")
    assert out["status"] == 404


def test_gateway_inactive_deployment(cloud_db: Path):
    store = CloudStateStore(cloud_db)
    store.create_customer("c3", "c3@x.com")
    store.create_deployment("dep3", "c3", "t3", api_base="http://127.0.0.1:1")
    store.set_deployment_status("dep3", "suspended")
    router = GatewayRouter(store)
    out = router.route("dep3", "memories/stats")
    assert out["status"] == 403


def test_gateway_proxies_to_deployment(cloud_db: Path, fake_deployment: str):
    store = CloudStateStore(cloud_db)
    store.create_customer("c4", "c4@x.com")
    store.create_deployment("dep4", "c4", "t4", api_base=fake_deployment)
    router = GatewayRouter(store)
    out = router.route("dep4", "memories/stats", method="GET")
    assert out["status"] == 200
    assert b"proxied" in out["body"]
