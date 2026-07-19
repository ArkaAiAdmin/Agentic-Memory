"""infra_cloud — SaaS management plane for agentic-memory.

Separate from the open-source core. Holds ONLY provisioning + billing
metadata in cloud_state.db. Never stores memories, KG, or audit logs.

Guardrails (per SaaS migration plan v2, Phase 3):
  1. cloud_state.db is NOT a central memory store.
  2. It only knows provisioning + billing metadata.
  3. All customer-data access goes through the customer's own MCP/REST endpoint.
"""
from __future__ import annotations

from infra_cloud.gateway import GatewayRouter
from infra_cloud.store import CloudStateStore, run_cloud_migrations

__all__ = ["CloudStateStore", "GatewayRouter", "run_cloud_migrations"]
