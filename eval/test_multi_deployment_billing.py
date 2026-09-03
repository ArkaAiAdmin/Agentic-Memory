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
    assert version == 4
    
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
    assert b"plan limit exceeded" in json.dumps(res["body"]).encode()
    assert b"seat_count" in json.dumps(res["body"]).encode()

def test_api_endpoints_checkout_and_webhook(test_dirs):
    db_path, cloud_db = test_dirs
    run_cloud_migrations(cloud_db)

    pytest.importorskip("stripe")
    import stripe
    import unittest.mock

    # Preserve existing environment variables before overriding
    saved_env = {k: os.environ.get(k) for k in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_PRO", "STRIPE_PRICE_ENTERPRISE", "STRIPE_WEBHOOK_SECRET")}
    _orig_create = None
    _orig_construct = None
    server = None
    try:
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
        os.environ["STRIPE_PRICE_PRO"] = "price_pro_test"
        os.environ["STRIPE_PRICE_ENTERPRISE"] = "price_ent_test"
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_secret"

        # Monkey-patch stripe.checkout.Session.create at module level (visible to server thread)
        _mock_session = unittest.mock.MagicMock()
        _mock_session.id = "cs_test_123"
        _mock_session.url = "https://checkout.stripe.com/pay/cs_test_123"
        if hasattr(stripe, 'checkout') and hasattr(stripe.checkout, 'Session'):
            _orig_create = stripe.checkout.Session.create
            stripe.checkout.Session.create = unittest.mock.MagicMock(return_value=_mock_session)
        _orig_construct = getattr(stripe.Webhook, "construct_event", None)

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
        base_url = f"http://127.0.0.1:{port}"
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
        
        # 2a. Negative test: Missing Stripe-Signature header -> 400
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
        req_no_sig = urllib.request.Request(
            f"{base_url}/api/v1/cloud/webhooks/stripe",
            data=json.dumps(webhook_payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            urllib.request.urlopen(req_no_sig)
            assert False, "Expected 400 for missing Stripe-Signature"
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # 2b. Negative test: Invalid signature verification error -> 400
        stripe.Webhook.construct_event = unittest.mock.MagicMock(
            side_effect=stripe.error.SignatureVerificationError("Invalid sig", "sig_header")
        )
        req_bad_sig = urllib.request.Request(
            f"{base_url}/api/v1/cloud/webhooks/stripe",
            data=json.dumps(webhook_payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "t=123,v1=invalid_signature",
            },
            method="POST"
        )
        try:
            urllib.request.urlopen(req_bad_sig)
            assert False, "Expected 400 for invalid signature"
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # 2c. Valid Stripe Webhook activates subscription
        stripe.Webhook.construct_event = unittest.mock.MagicMock(return_value=webhook_payload)
        req = urllib.request.Request(
            f"{base_url}/api/v1/cloud/webhooks/stripe",
            data=json.dumps(webhook_payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "t=123,v1=test_signature",
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
        if server is not None:
            server.stop()
        # Restore monkey-patch and env vars non-destructively
        if hasattr(stripe, 'checkout') and hasattr(stripe.checkout, 'Session') and _orig_create:
            stripe.checkout.Session.create = _orig_create
        if _orig_construct:
            stripe.Webhook.construct_event = _orig_construct
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


# ── Phase 5: Dedicated plan enforcement tests ────────────────────────────

def test_plan_enforcement_call_limit(test_dirs):
    """Gateway returns 402 when daily call limit is exceeded."""
    db_path, cloud_db = test_dirs
    run_cloud_migrations(cloud_db)
    store = CloudStateStore(cloud_db)

    store.create_customer("cust_limit", "limit@example.com")
    store.create_deployment("dep_limit", "cust_limit", "tenant_limit")
    store.create_subscription("sub_limit", "dep_limit", "free", status="active")

    # Under the limit should pass
    assert store.check_limit_exceeded("dep_limit") is False

    # Exhaust the free plan's 1000 daily calls
    store.increment_usage("dep_limit", rest_calls=1000)
    assert store.check_limit_exceeded("dep_limit") is True


def test_plan_enforcement_storage_limit(test_dirs):
    """Gateway returns 402 when storage exceeds plan limit."""
    db_path, cloud_db = test_dirs
    run_cloud_migrations(cloud_db)
    store = CloudStateStore(cloud_db)

    store.create_customer("cust_storage", "storage@example.com")
    store.create_deployment("dep_storage", "cust_storage", "tenant_storage")
    store.create_subscription("sub_storage", "dep_storage", "free", status="active")

    # Under limit initially
    assert store.check_limit_exceeded("dep_storage") is False

    # Free plan: 50 MB storage limit — exceed it
    store.increment_usage("dep_storage", storage_bytes=60 * 1024 * 1024)
    assert store.check_limit_exceeded("dep_storage") is True


def test_plan_enforcement_seat_limit(test_dirs):
    """Pro plan allows more seats than free."""
    db_path, cloud_db = test_dirs
    run_cloud_migrations(cloud_db)
    store = CloudStateStore(cloud_db)

    free = store.get_plan("free")
    pro = store.get_plan("pro")
    enterprise = store.get_plan("enterprise")

    # Seat limits: free < pro < enterprise
    assert free["max_seats"] < pro["max_seats"]
    assert pro["max_seats"] < enterprise["max_seats"]


def test_plan_limits_pro_tier(test_dirs):
    """Pro plan has higher limits than free."""
    db_path, cloud_db = test_dirs
    run_cloud_migrations(cloud_db)
    store = CloudStateStore(cloud_db)

    pro = store.get_plan("pro")
    free = store.get_plan("free")
    assert pro["max_mcp_calls_per_day"] > free["max_mcp_calls_per_day"]
    assert pro["max_storage_mb"] > free["max_storage_mb"]
    assert pro["max_seats"] > free["max_seats"]


def test_signup_provisions_memory_db(test_dirs):
    """Signup creates customer, deployment, and provisions memory.db."""
    db_path, cloud_db = test_dirs
    run_cloud_migrations(cloud_db)
    store = CloudStateStore(cloud_db)

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mem_db = Path(td) / "memory.db"
        store.create_customer("cust_signup", "signup@example.com", "Test User")
        store.create_deployment(
            "dep_signup", "cust_signup", "tenant_signup",
            db_path=str(mem_db), api_base="http://127.0.0.1:9878",
        )

        dep = store.get_deployment("dep_signup")
        assert dep is not None
        assert dep["customer_id"] == "cust_signup"
        assert dep["tenant_id"] == "tenant_signup"

        # Verify deployment is listed
        deps = store.list_deployments("cust_signup")
        assert len(deps) == 1
        assert deps[0]["deployment_id"] == "dep_signup"
