# CrewAI Integration

Agentic-memory provides native CrewAI `BaseTool` wrappers for search and save operations.

## Installation

```bash
pip install agentic-memory[crewai]
```

## Usage

### As CrewAI Tools

```python
from agentic_memory.integrations.crewai.tool import (
    AgenticMemorySearchTool,
    AgenticMemorySaveTool,
)
from crewai import Agent, Task, Crew

# Create tools
search_tool = AgenticMemorySearchTool(db_path="/path/to/memory.db")
save_tool = AgenticMemorySaveTool(db_path="/path/to/memory.db")

# Create agent with memory tools
researcher = Agent(
    role="Research Analyst",
    goal="Find and remember important information",
    tools=[search_tool, save_tool],
    verbose=True,
)

# Create task
task = Task(
    description="Research the latest AI trends and save key findings",
    agent=researcher,
)

# Run
crew = Crew(agents=[researcher], tasks=[task])
result = crew.kickoff()
```

### As Memory

```python
from agentic_memory.integrations.crewai.memory import AgenticMemory

# Use as CrewAI's memory system
memory = AgenticMemory(agent_id="researcher")

# Save memories
memory.save("Key finding: transformer models dominate NLP")

# Search memories
results = memory.search("transformer models")
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
from agentic_memory.integrations.crewai.tool import (
    AgenticMemorySearchTool,
    AgenticMemorySaveTool,
)
from crewai import Agent, Task, Crew, Process

# Setup tools
search_tool = AgenticMemorySearchTool()
save_tool = AgenticMemorySaveTool()

# Create specialized agents
researcher = Agent(
    role="Senior Researcher",
    goal="Conduct thorough research on given topics",
    backstory="You are an expert researcher with deep domain knowledge.",
    tools=[search_tool, save_tool],
    verbose=True,
)

writer = Agent(
    role="Technical Writer",
    goal="Write clear, concise technical documentation",
    backstory="You are a skilled writer who makes complex topics accessible.",
    tools=[save_tool],
    verbose=True,
)

# Create tasks
research_task = Task(
    description="Research the current state of AI memory systems and save key findings",
    agent=researcher,
)

writing_task = Task(
    description="Write a summary of the research findings",
    agent=writer,
)

# Run crew
crew = Crew(
    agents=[researcher, writer],
    tasks=[research_task, writing_task],
    process=Process.sequential,
    verbose=True,
)

result = crew.kickoff()
print(result)
```
