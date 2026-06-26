# CrewAI Integration

## Installation

```bash
pip install agentic-memory[crewai]
```

Requires Python 3.11–3.13 (Python 3.14 is blocked by `tiktoken` wheel
availability). Requires `crewai>=0.80,<1.0`.

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

After the run, all task contexts are queryable:

```python
results = memory.search("user preferences on dark mode")
# [{"content": "...", "score": 0.85, "tags": ["crew", "researcher", "task-1"]}, ...]
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

CrewAI 0.x has the `Memory` protocol with `save(context, agent, task)` and
`search(query) -> list[dict]`. CrewAI 1.x replaced this with
`LongTermMemory` / `ShortTermMemory`.

`AgenticMemoryMemory.__init__` runs a version check and raises a clear
`ImportError` on CrewAI 1.x with instructions to pin `crewai<1.0`.
