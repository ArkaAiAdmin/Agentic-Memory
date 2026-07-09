# LangChain Integration

Agentic-memory provides native LangChain `StructuredTool` wrappers for search and save operations.

## Installation

```bash
pip install agentic-memory[langchain]
```

## Usage

### As Tools

```python
from agentic_memory.integrations.langchain.tool import search_tool, save_tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_react_agent, AgentExecutor

# Create agent with memory tools
llm = ChatOpenAI(model="gpt-4")
agent = create_react_agent(
    llm,
    tools=[search_tool, save_tool],
    prompt="You are a helpful assistant with persistent memory.",
)

executor = AgentExecutor(agent=agent, tools=[search_tool, save_tool])
result = executor.invoke({"input": "What do we know about the user?"})
```

### As Retriever

```python
from agentic_memory.integrations.langchain.retriever import AgenticMemoryRetriever

# Use as a LangChain retriever for RAG
retriever = AgenticMemoryRetriever(
    search_kwargs={"limit": 5, "rerank": True}
)

docs = retriever.invoke("What are the project requirements?")
for doc in docs:
    print(doc.page_content)
```

### With Callbacks

```python
from agentic_memory.integrations.langchain.callback import AgenticMemoryCallback

# Track all memory operations
callback = AgenticMemoryCallback()
executor = AgentExecutor(
    agent=agent,
    tools=[search_tool, save_tool],
    callbacks=[callback],
)

# After execution, check what was tracked
print(callback.saved_memories)
print(callback.searched_queries)
```

### As Chat History

```python
from agentic_memory.integrations.langchain.history import AgenticMemoryChatMessageHistory

# Persistent chat history backed by agentic-memory
history = AgenticMemoryChatMessageHistory(session_id="user-123")
history.add_user_message("What's the status?")
history.add_ai_message("All systems operational.")

# Later sessions can recall
messages = history.messages
```

## Configuration

The integration uses `MemoryClient` under the hood. Configure via environment variables:

```bash
# Database path (default: ~/.config/agentic-memory/memory/memory.db)
export MEMORY_DB_PATH=/path/to/memory.db

# Agent ID for scoping
export MEMORY_AGENT_ID=my-agent
```

## Complete Example

```python
from agentic_memory.integrations.langchain.tool import search_tool, save_tool
from agentic_memory.integrations.langchain.retriever import AgenticMemoryRetriever
from langchain_openai import ChatOpenAI
from langchain.agents import create_react_agent, AgentExecutor
from langchain_core.prompts import ChatPromptTemplate

# Setup
llm = ChatOpenAI(model="gpt-4")
prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful assistant with persistent memory. "
     "Use the search tool to recall past context and save tool to remember important information."),
    ("placeholder", "{chat_history}"),
    ("human", "{input}"),
])

# Create agent
agent = create_react_agent(llm, tools=[search_tool, save_tool], prompt=prompt)
executor = AgentExecutor(agent=agent, tools=[search_tool, save_tool])

# Use
result = executor.invoke({"input": "What do we know about the deployment?"})
print(result["output"])
```
