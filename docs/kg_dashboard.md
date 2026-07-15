# Knowledge Graph Dashboard

The KG dashboard is a **Streamlit web app** (`dashboard.py`) that provides
real-time visibility into the knowledge graph state — entity counts,
fact distribution, extraction health, temporal contradictions, backup
management, and CTR analytics.

> **Note:** This is a web UI, not a CLI tool. Run it with:
> ```bash
> streamlit run dashboard.py
> ```
> For MCP-driven graph exploration (no browser), use the admin tools below.

## Quick Start

```bash
# Launch the dashboard
streamlit run dashboard.py

# Or query graph stats via MCP (no browser needed)
./venv/bin/python -c "
from mcp_maintenance_ops import _op_memory_stats
print(_op_memory_stats())
"
```

## Admin Ops (MCP)

| Operation | Description |
|-----------|-------------|
| `graph_stats` | Entity/edge/fact counts, density |
| `facts_stats` | Fact extraction stats (total, avg confidence, per-source) |
| `temporal_contradictions` | Active contradictions between facts |
| `temporal_query` | Query KG state as-of a point in time |
| `graph_traverse` | Walk the graph from a starting entity |
| `graph_shortest_path` | Shortest path between two entities |

## MCP Quick Access

```python
# In agent session:
memory_maintenance(operation="graph_stats")
memory_maintenance(operation="facts_stats")
memory_maintenance(operation="temporal_contradictions")
```

See `docs/MCP_SURFACE.md` for full argument details.
