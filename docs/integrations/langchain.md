# LangChain Integration

## Installation

```bash
pip install agentic-memory[langchain]
```

Requires Python ≥3.11, `langchain-core>=0.2.0`, `langchain-community>=0.2.0`.

## AgenticMemoryRetriever

Wraps `MemoryClient.search()` as a LangChain `BaseRetriever`. Drop in anywhere
LangChain expects a retriever:

```python
from langchain_anthropic import ChatAnthropic
from langchain.chains import RetrievalQA

from agentic_memory.integrations.langchain.retriever import (
    AgenticMemoryRetriever,
)

retriever = AgenticMemoryRetriever(
    db_path="~/.config/agentic-memory/memory/memory.db",
    search_kwargs={"limit": 5, "rerank": True},
)

llm = ChatAnthropic(model="claude-sonnet-4-20250514")
chain = RetrievalQA.from_chain_type(llm, retriever=retriever)
chain.invoke("What does the user prefer for UI?")
```

### Constructor arguments

| Argument | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str \| None` | `None` | SQLite DB path; env `AGENTIC_MEMORY_DB_PATH` fallback |
| `search_kwargs` | `dict` | `{"limit": 5, "rerank": True}` | Forwarded to `MemoryClient.search()` |

The retriever is also usable as a `Runnable` — call `.invoke(query)` or
`.ainvoke(query)` synchronously or asynchronously.

## AgenticMemoryChatHistory

Stores LangChain `BaseMessage` objects as tagged session memories:

```python
from langchain_core.messages import HumanMessage, AIMessage
from agentic_memory.integrations.langchain.history import (
    AgenticMemoryChatHistory,
)

history = AgenticMemoryChatHistory(
    db_path="~/.config/agentic-memory/memory/memory.db",
    session_id="conversation-42",
)

history.add_message(HumanMessage(content="My name is Arka"))
history.add_message(AIMessage(content="Nice to meet you, Arka."))

messages = history.messages  # in-memory cache
```

Each message is saved to SQLite with a role tag (`human`, `ai`, `system`)
and the session ID tag.

## Structured Tools

Expose save and search as LangChain `StructuredTool` instances for use
in ReAct agents:

```python
from langchain.agents import create_react_agent, AgentExecutor
from agentic_memory.integrations.langchain.tool import search_tool, save_tool

agent = create_react_agent(
    llm=ChatAnthropic(model="claude-sonnet-4-20250514"),
    tools=[search_tool, save_tool],
    prompt="You have access to persistent memory. Search before answering.",
)
executor = AgentExecutor(agent=agent, tools=[search_tool, save_tool])
executor.invoke({"input": "What does the user prefer for UI?"})
```

### `search_tool` schema

```python
class SearchMemoryInput(BaseModel):
    query: str          # required — semantic search query
    limit: int = 5      # optional — 1 to 50
```

Returns a compact string the LLM can read directly.

### `save_tool` schema

```python
class SaveMemoryInput(BaseModel):
    content: str            # required
    category: str = "sdk"   # lessons | projects | decisions | preferences | sessions | sdk
    tags: list[str] | None  # optional tag strings
```

Returns `"Saved as <note_id>"`.

## AgenticMemoryCallbackHandler

Auto-persist every LLM prompt and response:

```python
from agentic_memory.integrations.langchain.callback import (
    AgenticMemoryCallbackHandler,
)
from langchain_anthropic import ChatAnthropic

handler = AgenticMemoryCallbackHandler(
    db_path="~/.config/agentic-memory/memory/memory.db",
    save_prompts=False,   # set True to log raw inputs too
    save_responses=True,
    auto_tags=["auto-saved"],
)

llm = ChatAnthropic(model="claude-sonnet-4-20250514").bind(callbacks=[handler])
chain.invoke({"input": "Remember: user likes dark mode"})
# The response is automatically tagged and saved as a session memory
```

| Attribute | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str \| None` | `None` | DB path; env fallback |
| `save_prompts` | `bool` | `False` | Persist raw LLM input |
| `save_responses` | `bool` | `True` | Persist LLM output text |
| `auto_tags` | `list[str]` | `["auto-saved"]` | Tags appended to every entry |
