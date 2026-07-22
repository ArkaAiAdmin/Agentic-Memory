# Testing and Debugging Tools

The **Testing and Debugging Tools** in Agentic Memory provide inspection, auditing, unit test assertion helpers, and system state validation surfaces via MCP and internal diagnostics scripts.

## Overview

In local-first agentic environments, verifying memory mutations, vector drift, and MCP tool execution requires targeted testing tools. The testing and debugging subsystem comprises:

- **MCP Audit Operations (`mcp_audit.py`)**: Exposes tool execution logs, query timing breakdowns, and trace logs.
- **Diagnostics & Dry-Run APIs**: Enables zero-side-effect execution of save/search pipelines for test assertions.
- **Consistency Verification Utilities**: Provides integrity validation across SQLite, vector indexes, and `.md` file stores.

## Key Component Architecture

| Component | Target File | Purpose |
| :--- | :--- | :--- |
| **Audit Logger** | [mcp_audit.py](file://mcp_audit.py) | Exposes `memory_audit` and execution metrics for debugging |
| **Integrity Tester** | [memory_integrity.py](file://memory_integrity.py) | Performs deep cross-store validation (SQLite + Usearch + Markdown) |
| **Tool Registry Inspection** | [tool_registry.py](file://tool_registry.py) | Inspects CORE vs ADMIN router registrations and schema definitions |

## Usage Patterns & Code Invariants

### 1. Dry-Run Memory Inspection
When testing memory write paths, inspect the 3-store saga state without committing permanent side effects:

```python
from save.pipeline import save_memory

# Test dry-run save assertion
result = save_memory(
    content="Unit test memory payload",
    category="test",
    tags=["test", "verification"],
    defer_expensive=True
)
assert result["status"] == "success"
```

### 2. MCP Audit & Execution Diagnostics
The `memory_audit` MCP tool returns structural execution traces, identifying candidate score drops or reranker latencies:

```json
{
  "event": "memory_search",
  "latency_ms": 14.2,
  "phases": {
    "query_parsing": 0.8,
    "vector_search": 4.1,
    "bm25_search": 2.3,
    "rrf_fusion": 1.1,
    "reranking": 5.9
  }
}
```

## Troubleshooting & Debugging Workflow

1. **Check Database Lock Status**: Verify no stale `.lock` files exist in `GLOBAL_MEM_DIR`.
2. **Verify Tool Schema Registrations**: Ensure MCP tool parameters pass strict JSON schema verification.
3. **Inspect Vector Drift**: Run `scripts/cron_detect_vec_drift.py` to identify missing embeddings.
