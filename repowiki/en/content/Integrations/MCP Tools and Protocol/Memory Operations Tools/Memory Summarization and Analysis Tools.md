# Memory Summarization and Analysis Tools

The **Memory Summarization and Analysis Tools** subsystem provides automated memory consolidation, periodic digest creation, LLM-based abstractive summarization, and cluster topic extraction.

## Core Capabilities

- **Automatic Memory Digesting ([cron_auto_summarize.py](file://cron/cron_auto_summarize.py))**: Aggregates related session memories into higher-level summary nodes.
- **MCP Summarization Surface ([mcp_summarization.py](file://mcp_summarization.py))**: Exposes `summarize_memories`, `extract_key_insights`, and `generate_topic_tree`.
- **LLM Summarizer Integration ([infra/llm_providers.py](file://infra/llm_providers.py))**: Uses configured LLM providers for abstractive synthesis with fallback logic.

## Summary Generation Workflow

1. **Query & Filter**: Select candidate memories by tag, timerange, or agent namespace.
2. **Cluster & Deduplicate**: Group semantically overlapping items using vector distance thresholds.
3. **Abstractive Synthesis**: Generate structured markdown summary with entity cross-links.
4. **Hierarchical Save**: Persist summary as a meta-memory node linked to child memory IDs.

```python
from mcp_summarization import summarize_memories

summary = summarize_memories(
    tags=["architecture", "decisions"],
    time_window="7d",
    max_summary_length=500
)
print(summary["summary_text"])
```
