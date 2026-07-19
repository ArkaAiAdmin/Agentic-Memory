import json
import os
import socket
import time
import urllib.request
import urllib.parse
from pathlib import Path
import pytest

from infra_cloud import CloudStateStore, GatewayRouter, run_cloud_migrations
from infra.api_server import APIServer

def get_free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

@pytest.fixture
def test_dirs(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    # Create empty database to run migrations against
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER);")
    conn.commit()
    conn.close()
    
    cloud_db = tmp_path / "cloud_state.db"
    return db_path, cloud_db

def test_migration_002_plans_applied(test_dirs):
    db_path, cloud_db = test_dirs
    # Run migrations
    version = run_cloud_migrations(cloud_db)
    assert version == 3
    
    store = CloudStateStore(cloud_db)
    plans = store.list_plans()
    assert len(plans) == 3
    
    free_plan = store.get_plan("free")
    assert free_plan["max_mcp_calls_per_day"] == 1000
    
    pro_plan = store.get_plan("pro")
    assert pro_plan["max_mcp_calls_per_day"] == 100000

def test_usage_metering_and_gateway_blocking(test_dirs):
    db_path, cloud_db = test_dirs
    run_cloud_migrations(cloud_db)
    store = CloudStateStore(cloud_db)
    
    # Create customer and deployment
    store.create_customer("cust_a", "a@example.com")
    store.create_deployment("dep_a", "cust_a", "tenant_a", api_base="http://127.0.0.1:9999")
    
    # Check limit exceeded initially
    assert not store.check_limit_exceeded("dep_a")
    
    # Increment usage up to the free tier limit (1000 calls)
    store.increment_usage("dep_a", rest_calls=1000)
    assert store.check_limit_exceeded("dep_a")
    
    # Test Gateway Router intercepts and returns 402 Payment Required
    router = GatewayRouter(store)
    res = router.route("dep_a", "/api/v1/memories")
    assert res["status"] == 402
    assert b"daily call limit exceeded" in json.dumps(res["body"]).encode()

def test_api_endpoints_checkout_and_webhook(test_dirs):
    db_path, cloud_db = test_dirs
    run_cloud_migrations(cloud_db)

    # Set Stripe env vars before server starts so the server thread sees them
    import os
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
    os.environ["STRIPE_PRICE_PRO"] = "price_pro_test"
    os.environ["STRIPE_PRICE_ENTERPRISE"] = "price_ent_test"

    # Monkey-patch stripe.checkout.Session.create at module level (visible to server thread)
    import stripe
    import unittest.mock
    _mock_session = unittest.mock.MagicMock()
    _mock_session.id = "cs_test_123"
    _mock_session.url = "https://checkout.stripe.com/pay/cs_test_123"
    if hasattr(stripe, 'checkout') and hasattr(stripe.checkout, 'Session'):
        _orig_create = stripe.checkout.Session.create
        stripe.checkout.Session.create = unittest.mock.MagicMock(return_value=_mock_session)

    # Start APIServer
    port = get_free_port()
    token = "test-token-secret"
    server = APIServer(
        db_path=db_path,
        agent_id="test-agent",
        host="127.0.0.1",
        port=port,
        token=token
    )
    server.start()
    
    # Wait for server to start
    for _ in range(20):
        try:
            s = socket.socket()
            s.connect(("127.0.0.1", port))
            s.close()
            break
        except Exception:
            time.sleep(0.05)
            
    # Setup test customer and deployment in the cloud_state.db
    store = CloudStateStore(cloud_db)
    store.create_customer("cust_b", "b@example.com")
    store.create_deployment("dep_b", "cust_b", "tenant_b", api_base=f"http://127.0.0.1:{port}")

    # Set Stripe env vars and fix placeholder price IDs so checkout can proceed
    import os
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
    os.environ["STRIPE_PRICE_PRO"] = "price_pro_test"
    os.environ["STRIPE_PRICE_ENTERPRISE"] = "price_ent_test"
    
    base_url = f"http://127.0.0.1:{port}"
    
    try:
        # 1. Trigger Checkout Session (mock Stripe SDK)
        checkout_payload = {
            "deployment_id": "dep_b",
            "plan_id": "pro"
        }
        req = urllib.request.Request(
            f"{base_url}/api/v1/cloud/checkout",
            data=json.dumps(checkout_payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            },
            method="POST"
        )

        with urllib.request.urlopen(req) as resp:
            checkout_res = json.loads(resp.read().decode())
            
        assert checkout_res["status"] == "ok"
        assert "checkout_url" in checkout_res
        session_id = checkout_res["session_id"]
        
        # 2. Trigger Mock Stripe Webhook to activate subscription
        webhook_payload = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": "dep_b",
                    "metadata": {
                        "plan_id": "pro"
                    },
                    "subscription": f"sub_stripe_{session_id}",
                    "amount_total": 4900,
                    "current_period_end": int(time.time()) + 30 * 86400,
                }
            }
        }
        
        req = urllib.request.Request(
            f"{base_url}/api/v1/cloud/webhooks/stripe",
            data=json.dumps(webhook_payload).encode(),
            headers={
                "Content-Type": "application/json"
            },
            method="POST"
        )
        
        with urllib.request.urlopen(req) as resp:
            webhook_res = json.loads(resp.read().decode())
            
        assert webhook_res["status"] == "ok"
        
        # 3. Query Cloud Usage endpoint to verify subscription is active and has upgraded limits
        req = urllib.request.Request(
            f"{base_url}/api/v1/cloud/usage?deployment_id=dep_b",
            headers={
                "Authorization": f"Bearer {token}"
            },
            method="GET"
        )
        
        with urllib.request.urlopen(req) as resp:
            usage_res = json.loads(resp.read().decode())
            
        assert usage_res["subscription"]["status"] == "active"
        assert usage_res["plan"]["id"] == "pro"
        assert usage_res["plan"]["max_mcp_calls_per_day"] == 100000
        assert len(usage_res["invoices"]) == 1
        assert usage_res["invoices"][0]["amount_cents"] == 4900
        
    finally:
        server.stop()
        # Restore monkey-patch and env vars
        if hasattr(stripe, 'checkout') and hasattr(stripe.checkout, 'Session') and _orig_create:
            stripe.checkout.Session.create = _orig_create
        for k in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_PRO", "STRIPE_PRICE_ENTERPRISE"):
            os.environ.pop(k, None)
