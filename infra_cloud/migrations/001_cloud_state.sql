-- Migration 001: Cloud state — management plane schema
-- Separate database (cloud_state.db) from per-deployment memory.db.
-- Stores ONLY provisioning + billing metadata. Never memories, KG, or audit logs.

CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    name TEXT,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);

CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    label TEXT,
    db_path TEXT,
    api_base TEXT,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);
CREATE INDEX IF NOT EXISTS idx_deployments_customer ON deployments(customer_id);
CREATE INDEX IF NOT EXISTS idx_deployments_tenant ON deployments(tenant_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    stripe_sub_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    current_period_end REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY (deployment_id) REFERENCES deployments(deployment_id)
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_deployment ON subscriptions(deployment_id);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    subscription_id TEXT,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'usd',
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id);
