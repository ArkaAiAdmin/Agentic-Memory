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

## End-to-end example

A complete crew that persists every task run and exposes search/save tools to an
agent:

```python
from crewai import Agent, Task, Crew
from agentic_memory.integrations.crewai.memory import AgenticMemoryMemory
from agentic_memory.integrations.crewai.tool import (
    AgenticMemorySearchTool,
    AgenticMemorySaveTool,
)

# 1. Crew-wide memory slot — persists every task context.
memory = AgenticMemoryMemory(
    db_path="~/.config/agentic-memory/memory/memory.db",
    auto_tags=["production"],
)

researcher = Agent(
    role="Researcher",
    goal="Find facts and remember what you learn",
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

writer = Agent(
    role="Writer",
    goal="Summarise findings using recalled memory",
    verbose=True,
)

crew = Crew(
    agents=[researcher, writer],
    tasks=[
        Task(
            description="Research the user's UI preferences",
            expected_output="A summary of preferences",
            agent=researcher,
        ),
        Task(
            description="Write up the preferences",
            expected_output="Markdown brief",
            agent=writer,
        ),
    ],
    memory=memory,
    verbose=True,
)
crew.kickoff()

# 2. Recall anything the crew persisted.
results = memory.recall("user preferences on dark mode")
for m in results:
    print(m["score"], m["record"]["content"])
```

## Troubleshooting

### `ProcessLookupError` / `tiktoken` build error during install
**Symptom:** `pip install agentic-memory[crewai]` fails to build a wheel, or
the interpreter raises `ProcessLookupError` at import time.
**Cause:** You are on Python 3.14. `tiktoken` has no prebuilt wheel for 3.14,
so it tries (and fails) to compile from source.
**Fix:** Use Python 3.11–3.13:
```bash
pyenv install 3.12 && pyenv local 3.12
pip install "agentic-memory[crewai]"
```

### `AgenticMemoryMemory` is rejected by `Crew(memory=...)`
**Symptom:** Under CrewAI v1, passing the adapter raises a Pydantic validation
error about the `memory` field / discriminated union.
**Cause:** CrewAI v1 identifies the memory variant via the `memory_kind`
discriminator. The adapter exposes `memory_kind: Literal["memory"]`
(`memory.py:46`); if it is missing or renamed the union cannot match.
**Fix:** Use `AgenticMemoryMemory` directly (it already carries the field). Do
not subclass it without preserving `memory_kind`.

### `MemoryClient` reads the wrong database
**Symptom:** Memories saved by a crew are invisible to other tools, or vice
versa.
**Cause:** `db_path` was omitted. `_resolve_db_path()` falls back to
`AGENTIC_MEMORY_DB_PATH`, then `MEMORY_DB_PATH` (`memory.py:63`). If both are
unset, `MemoryClient()` uses the default path, which may differ from the DB
other processes read.
**Fix:** Set the path explicitly or via env var:
```python
memory = AgenticMemoryMemory(db_path="~/.config/agentic-memory/memory/memory.db")
```
```bash
export MEMORY_DB_PATH="$HOME/.config/agentic-memory/memory/memory.db"
```

### `NotImplementedError: ... does not implement forget()`
**Symptom:** A call to `memory.forget(...)` (e.g. triggered by a v1 reset flow)
raises `NotImplementedError`.
**Cause:** `forget()` is intentionally unimplemented (`memory.py:282`). CrewAI
does not call `forget()` during normal task execution, so the adapter omits it.
**Fix:** Delete memories directly via
`agentic_memory.MemoryClient.delete(note_id)`; do not rely on `forget()`.

### CrewAI installed but the adapters import as plain stubs
**Symptom:** `AgenticMemorySearchTool`/`AgenticMemorySaveTool` work but are not
`issubclass`/`isinstance` of `crewai.tools.BaseTool`.
**Cause:** `crewai` was not importable when the module loaded, so the tool
classes bind to a plain `object` sentinel and degrade to duck-typed stubs
(`tool.py:154`). They still function, but CrewAI's own tool validation may treat
them as generic callables.
**Fix:** Ensure `crewai>=1.0` is importable in the same environment, then
re-import the adapters.
