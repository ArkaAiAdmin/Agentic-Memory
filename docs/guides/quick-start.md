# Quick Start Guide

Get agentic-memory running in 5 minutes.

## Option 1: Python SDK (Recommended)

```bash
# Install
pip install agentic-memory

# Or install from source
git clone https://github.com/ArkaAiAdmin/agentic-memory-local.git
cd agentic-memory-local
pip install -e .
```

### Basic Usage

```python
from agentic_memory import MemoryClient

# Initialize (uses default DB location)
mc = MemoryClient()

# Save a memory
note_id = mc.save("User prefers dark mode and vim keybindings")

# Search memories
results = mc.search("What does the user prefer?")
for r in results:
    print(f"[{r.score:.2f}] {r.content}")

# Get stats
stats = mc.stats()
print(f"Memories: {stats.memories}, Facts: {stats.facts}")
```

### Agent Scoping

```python
from agentic_memory import AgentMemory

# Each agent gets isolated memory
coder = AgentMemory(agent_id="coder")
coder.save("Frontend uses React with TypeScript")

designer = AgentMemory(agent_id="designer")
designer.save("Brand colors are #FF5733 and #33FF57")

# Each agent only sees their own memories
coder.search("frontend")  # finds React memory
designer.search("colors")  # finds brand colors
```

## Option 2: MCP Server

```bash
# Install and run the MCP server
cd ~/.config/agentic-memory
venv/bin/python -m memory_mcp
```

The MCP server exposes 17 tools that work with Claude, GPT, Gemini, and any MCP-compatible agent.

## Option 3: REST API

```bash
# Start the REST server
agentic-memory api --port 9878
```

```bash
# Save a memory
curl -X POST http://localhost:9878/api/v1/memories \
  -H "Content-Type: application/json" \
  -d '{"content": "User prefers dark mode"}'

# Search
curl "http://localhost:9878/api/v1/search?q=dark+mode&limit=5"
```

## Option 4: TypeScript SDK

```bash
npm install @agentic-memory/sdk
```

```typescript
import { MemoryClient } from '@agentic-memory/sdk';

const client = new MemoryClient({ baseUrl: 'http://localhost:9878' });

// Save
const id = await client.add('User prefers dark mode');

// Search
const results = await client.search('dark mode');
console.log(results);
```

## What's Next?

- [Python SDK API Reference](../api/python-sdk.md)
- [TypeScript SDK API Reference](../api/typescript-sdk.md)
- [REST API Reference](../api/rest-api.md)
- [LangChain Integration](./langchain.md)
- [CrewAI Integration](./crewai.md)
- [Architecture Overview](../architecture/overview.md)
