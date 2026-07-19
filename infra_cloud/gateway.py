"""Public REST gateway for the SaaS management plane (Phase 3).

Routes customer traffic to the correct deployment WITHOUT ever touching
customer memories:

    api.agentic-memory.dev/v1/<deployment_id>/<path...>
        -> look up deployment_id in cloud_state.db
        -> proxy the request to that deployment's own REST API (api_base)
        -> return the deployment's response

Guardrails enforced here:
  - The gateway only knows routing metadata (deployment -> api_base).
  - It forwards the request with the SAME auth the deployment expects; it
    does NOT inject or read customer memory content.
  - Unknown deployment_id -> 404. Inactive deployment -> 403.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from infra_cloud.store import CloudStateStore

logger = logging.getLogger(__name__)


class GatewayRouter:
    """Routes /v1/<deployment_id>/... to the deployment's own REST API."""

    def __init__(self, store: CloudStateStore):
        self.store = store

    def route(
        self,
        deployment_id: str,
        sub_path: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        """Proxy a request to a deployment's REST API.

        Returns a uniform envelope:
            {"status": int, "headers": dict, "body": bytes|str}
        Caller (HTTP server) is responsible for writing it back.
        """
        dep = self.store.get_deployment(deployment_id)
        if dep is None:
            return _json(404, {"error": "unknown deployment", "deployment_id": deployment_id})
        if dep.get("status") != "active":
            return _json(403, {"error": "deployment inactive", "deployment_id": deployment_id})

        if self.store.check_limit_exceeded(deployment_id):
            seat_count = self.store.get_seat_count(deployment_id)
            return _json(
                402,
                {
                    "error": "Payment Required: plan limit exceeded",
                    "deployment_id": deployment_id,
                    "seat_count": seat_count,
                },
            )

        self.store.increment_usage(deployment_id, rest_calls=1)

        api_base = dep.get("api_base")
        if not api_base:
            return _json(502, {"error": "deployment has no api_base configured",
                               "deployment_id": deployment_id})

        target = f"{api_base.rstrip('/')}/{sub_path.lstrip('/')}"
        req = urllib.request.Request(target, data=body, method=method)
        for k, v in (headers or {}).items():
            # Do not let the gateway's own hop-by-hop headers leak through.
            if k.lower() in ("host", "content-length"):
                continue
            req.add_header(k, v)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            return {
                "status": resp.status,
                "headers": dict(resp.headers),
                "body": resp.read(),
            }
        except urllib.error.HTTPError as e:
            return {
                "status": e.code,
                "headers": dict(e.headers),
                "body": e.read(),
            }
        except Exception as e:  # network / DNS / timeout
            logger.warning("gateway proxy error for %s: %s", deployment_id, e)
            return _json(502, {"error": "gateway proxy failure", "detail": str(e)})


def _json(status: int, payload: dict) -> dict[str, Any]:
    return {"status": status, "headers": {"Content-Type": "application/json"}, "body": payload}
