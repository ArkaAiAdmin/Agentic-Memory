# Ecosystem Integrations — Overview

Agentic Memory ships with typed adapters for popular LLM agent frameworks.
All adapters wrap the same `MemoryClient`, so behaviour is identical
regardless of which integration surface you use.

## Available adapters

| Adapter | File | Framework versions |
|---|---|---|
| `AgenticMemoryRetriever` | `integrations/langchain/retriever.py` | LangChain ≥0.2.0 |
| `AgenticMemoryChatHistory` | `integrations/langchain/history.py` | LangChain ≥0.2.0 |
| `search_tool`, `save_tool` | `integrations/langchain/tool.py` | LangChain ≥0.2.0 |
| `AgenticMemoryCallbackHandler` | `integrations/langchain/callback.py` | LangChain ≥0.2.0 |
| `AgenticMemorySearchTool` | `integrations/crewai/tool.py` | CrewAI 0.x |
| `AgenticMemorySaveTool` | `integrations/crewai/tool.py` | CrewAI 0.x |
| `AgenticMemoryMemory` | `integrations/crewai/memory.py` | CrewAI 0.x |

## Installation

```bash
pip install agentic-memory[langchain]   # LangChain only
pip install agentic-memory[crewai]      # CrewAI only
pip install agentic-memory[all]         # Both (not yet available for py3.14)
```

> **Python version note:** The `crewai` extra requires `tiktoken`, which
> only pre-builds wheels for Python ≤3.13. CrewAI tests and the `[crewai]`
> extra are skipped on Python 3.14. Use Python 3.11–3.13 for full
> integration coverage.

## Choosing the right adapter

| Scenario | Recommended adapter |
|---|---|
| Drop a memory retriever into a LangChain `RetrievalQA` / RAG chain | `AgenticMemoryRetriever` |
| Persist an agent's conversational history | `AgenticMemoryChatHistory` |
| Give an agent tool calls for memory (ReAct pattern) | `search_tool` + `save_tool` |
| Auto-log every LLM turn without changing agent code | `AgenticMemoryCallbackHandler` |
| Memory slot on a CrewAI crew | `AgenticMemoryMemory` |
| Mount memory tools on individual CrewAI agents | `AgenticMemorySearchTool` + `AgenticMemorySaveTool` |

## Planned adapters

- `AgenticMemoryLlamaIndex` — LlamaIndex `BaseMemory` interface
- `AgenticMemoryHaystack` — Haystack document store connector
- `AgenticMemorySemanticKernel` — Semantic Kernel memory plugin

Open an issue to request another framework.
