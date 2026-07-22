# Memory Sharing and Collaboration Tools

The **Memory Sharing and Collaboration Tools** provide MCP operations for cross-agent memory visibility, shared memory spaces, role-based access controls, and multi-tenant memory distribution.

## Module Structure

- **MCP Sharing Surface ([mcp_sharing.py](file://mcp_sharing.py))**: Exposes `share_memory`, `unshare_memory`, and `list_shared_spaces`.
- **Memory Sharing Backend ([memory_sharing.py](file://memory_sharing.py))**: Handles cross-tenant access rule evaluation and shared spaces metadata.

## Core Operations

### `share_memory`
Grants read or read/write access for specific memory IDs to another agent or public tenant pool:

```json
{
  "memory_id": "mem_8f9210a",
  "target_agent_id": "agent_reviewer",
  "permission": "read"
}
```

### `list_shared_spaces`
Lists accessible shared memory spaces across the active cluster or session instance.

## Security & Isolation Invariants

- **Tenant Boundaries**: Memories remain private to their creating `agent_id` by default unless explicitly published to a shared space or queried with `include_global=True`.
- **Audit Logging**: All sharing and unsharing events emit outbox entries for security compliance and audit trails.
