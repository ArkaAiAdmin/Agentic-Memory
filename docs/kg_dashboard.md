# Knowledge Graph Dashboard

The KG dashboard provides real-time visibility into the knowledge graph
state — entity counts, fact distribution, extraction health, and
temporal contradictions.

> **Note:** The dashboard is a CLI tool (`python mcp_maintenance.py` with
> graph ops), not a web UI. For MCP-driven graph exploration, use the
> `memory_maintenance(operation="graph_traverse")` and
> `memory_maintenance(operation="graph_shortest_path")` admin tools.

## Quick Start

```bash
# Graph statistics
./venv/bin/python -c "
from mcp_maintenance_ops import _op_memory_stats
print(_op_memory_stats())
"

# List all facts (latest 20)
./venv/bin/python -c "
from db import connection_pool
from infra.config import get_db_path
conn = connection_pool.get(get_db_path())
rows = conn.execute('SELECT id, fact, confidence FROM kg_facts ORDER BY id DESC LIMIT 20').fetchall()
for r in rows: print(r)
"
```

## Admin Ops

| Operation | Description |
|-----------|-------------|
| `graph_stats` | Entity/edge/fact counts, density |
| `facts_stats` | Fact extraction stats (total, avg confidence, per-source) |
| `temporal_contradictions` | Active contradictions between facts |
| `temporal_query` | Query KG state as-of a point in time |
| `graph_traverse` | Walk the graph from a starting entity |
| `graph_shortest_path` | Shortest path between two entities |

## Metrics Dashboard (MCP)

```python
# In agent session:
memory_maintenance(operation="graph_stats")
memory_maintenance(operation="facts_stats")
memory_maintenance(operation="temporal_contradictions")
```

See `docs/reference/mcp-tools.md` for full argument details.
