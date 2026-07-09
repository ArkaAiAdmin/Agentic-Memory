"""Peer-facing admin endpoint: GET-able policy_hash for fleet diff."""
from __future__ import annotations

import json

from mcp_common import _bootstrap_path  # noqa: F401
from mcp_instance import mcp  # noqa: F401
from infra.memory_common import configure_logging  # noqa: F401
from infra.infrastructure import (  # noqa: F401
    resolve_active_memory_dir,
)
from mcp_common import with_audit


@mcp.tool()
@with_audit("memory_admin_policy_hash")
def memory_admin_policy_hash(*, include_full: bool = False) -> str:
    """Return the local process's drift-policy hash. Used by fleet drift diff."""
    from infra.config_drift_policy import resolve_policy
    p = resolve_policy()
    body = {
        "policy_hash": p.policy_hash(),
        "scope": p.scope,
        "agent_id": "",
        "schema_version": 1,
    }
    if include_full:
        body["full_policy"] = p.to_dict()
    return json.dumps(body, indent=2)
