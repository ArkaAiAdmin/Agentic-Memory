# CrewAI Integration

## Installation

```bash
pip install agentic-memory[crewai]
```

Requires Python 3.11–3.13 (Python 3.14 is blocked by `tiktoken` wheel
availability). Requires `crewai>=1.0`.

> ⚠️  If you see `ProcessLookupError` or a `tiktoken` build error, you
> are on Python 3.14. Use Python 3.11–3.13, or skip the CrewAI extra
> (the rest of the package works fine on 3.14).

## AgenticMemoryMemory (crew memory slot)

Persist every crew task execution as a memory entry:

```python
from crewai import Agent, Task, Crew
from agentic_memory.integrations.crewai.memory import AgenticMemoryMemory

memory = AgenticMemoryMemory(
    db_path="~/.config/agentic-memory/memory/memory.db",
    auto_tags=["production"],
)

crew = Crew(
    agents=[researcher, writer],
    tasks=[Task(description="Research X", expected_output="Summary", agent=researcher)],
    memory=memory,
    verbose=True,
)
crew.kickoff()
```

`AgenticMemoryMemory` implements both the CrewAI v0.x `save`/`search` memory
protocol and the CrewAI v1.x unified-memory `remember`/`recall` protocol.
A single class works across both versions.

After the run, all task contexts are queryable:

```python
results = memory.recall("user preferences on dark mode")
# [{"record": {...}, "score": 0.85}, ...]
```

## AgenticMemorySearchTool and AgenticMemorySaveTool

Mount native CrewAI tools on individual agents:

```python
from crewai import Agent
from agentic_memory.integrations.crewai.tool import (
    AgenticMemorySearchTool,
    AgenticMemorySaveTool,
)

senior = Agent(
    role="Senior Engineer",
    goal="Build correct systems and remember decisions",
    tools=[
        AgenticMemorySearchTool(
            db_path="~/.config/agentic-memory/memory/memory.db",
        ),
        AgenticMemorySaveTool(
            db_path="~/.config/agentic-memory/memory/memory.db",
        ),
    ],
    verbose=True,
)
```

### AgenticMemorySearchTool

| Attribute | Type | Default |
|---|---|---|
| `db_path` | `str \| None` | `None` |
| Input `query` | `str` | required |
| Input `limit` | `int` | 5 (1–50) |

Returns: `"[memory search: N results for 'query']\n1. [score=…] content\n..."`

### AgenticMemorySaveTool

| Attribute | Type | Default |
|---|---|---|
| `db_path` | `str \| None` | `None` |
| Input `content` | `str` | required |
| Input `tags` | `list[str]` | `[]` |
| Input `category` | `str` | `"sdk"` |

Returns: `"Saved as <note_id>"`

## Version compatibility

`AgenticMemoryMemory` supports both CrewAI 0.x and 1.x:

| CrewAI version | Protocol | Methods |
|---|---|---|
| 0.x (>=0.80) | `Memory` save/search | ``save(context, agent, task)``, ``search(query)`` |
| 1.x (>=1.0) | Unified `Memory` | ``remember(content, categories=...)``, ``recall(query, categories=..., limit=...)`` |

`AgenticMemoryMemory.__init__` accepts either version; no explicit version
check is needed because both method sets are implemented on the same class.
CrewAI v1's ``Field(discriminator="memory_kind")`` recognises the adapter
via the ``memory_kind: Literal["memory"]`` field.
