---
kind: external_dependency
name: Stripe billing integration
slug: stripe
category: external_dependency
category_hints:
    - auth_protocol
scope:
    - '**'
---

Stripe is wired in for multi-deployment subscription billing. Secrets are injected via environment variables `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PRO`, `STRIPE_PRICE_ENTERPRISE`. The server creates Checkout Sessions and verifies Stripe webhooks at `/api/v1/cloud/webhooks/stripe`. The feature is guarded behind an API server mode and is exercised by tests in `eval/test_multi_deployment_billing.py`.