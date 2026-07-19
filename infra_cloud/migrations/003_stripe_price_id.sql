-- Migration 003: Add Stripe Price ID to plans for real checkout integration

ALTER TABLE plans ADD COLUMN stripe_price_id TEXT;

-- Seed with placeholder values; operators override via STRIPE_PRICE_* env vars
-- or update these rows directly after configuring their Stripe dashboard.
UPDATE plans SET stripe_price_id = 'price_free_placeholder' WHERE id = 'free';
UPDATE plans SET stripe_price_id = 'price_pro_placeholder' WHERE id = 'pro';
UPDATE plans SET stripe_price_id = 'price_enterprise_placeholder' WHERE id = 'enterprise';
