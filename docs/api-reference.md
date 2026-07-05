# API Reference

## Python API

The `agentic_memory` package exports a **Mem0-compatible SDK** with two
classes: `Memory` and `AgentMemory`. Individual functions like
`save_memory()`, `search_memories()`, `get_memory()`, etc. are not
part of the public API — they live in internal modules and should
be called via the `Memory` / `AgentMemory` classes below, or via
the MCP server (`memory_save`, `memory_search`, `memory_recall_context`,
etc. — see [MCP Tools](reference/mcp-tools.md)).

### `Memory` class

The primary SDK entry point. Manages a single memory store (the
local memory dir resolved by `resolve_active_memory_dir`).

```python
from agentic_memory import Memory

m = Memory()
memory_id = m.add("User prefers dark mode")

results = m.search("What does the user prefer?", limit=10)
for r in results:
    print(r["id"], r["content"][:80])

# List, delete, clear
all_memories = m.list(limit=50)
m.delete(memory_id)
count = m.clear()  # returns number of memories removed
```

#### Constructor

```python
Memory(
    db_path: str | None = None,
    memory_dir: str | None = None,
    use_global: bool = True,
    auto_start: bool = False,
    config: Any = None,
)
```

**Parameters:**
- `db_path` (str, optional) — Override the SQLite path. Defaults to the
  resolved active memory dir + `memory.db`.
- `memory_dir` (str, optional) — Override the memory directory.
- `use_global` (bool, optional) — Include the global memory store in
  `search()` results (default: True).
- `auto_start` (bool, optional) — Start the background daemon at
  construction time (default: False).
- `config` (Any, optional) — Inject a pre-built `Config` instance
  (default: `Config.from_toml()`).

#### Methods

- **`add(content, tags=None) → str`**
  Save a memory. Returns the canonical note id.
- **`search(query, limit=10, rerank=True) → list[dict]`**
  Hybrid BM25 + semantic search. Returns a list of result dicts
  with `id`, `content`, `score`, `category`, `title`, `tags`.
- **`delete(note_id) → bool`**
  Soft-delete a memory. Returns `True` on success.
- **`list(limit=50, offset=0) → list[dict]`**
  Return memories in insertion order, paginated.
- **`clear() → int`**
  Remove all memories. Returns the count removed.

---

### `AgentMemory` class

The same surface as `Memory` but scoped to a single agent id
(useful for multi-agent scenarios where you want to keep each
agent's writes logically separate). Under the hood, every
`AgentMemory.add()` sets the `agent_id` metadata on the note.

```python
from agentic_memory import AgentMemory

am = AgentMemory(agent_id="coder-1")
am.add("Frontend uses React with TypeScript")
results = am.search("frontend")
```

#### Constructor

```python
AgentMemory(
    agent_id: str,
    db_path: str | None = None,
    memory_dir: str | None = None,
    use_global: bool = True,
)
```

**Parameters:**
- `agent_id` (str, required) — The agent identifier (stored in the
  note's metadata for downstream filtering / CRDT routing).
- `db_path`, `memory_dir`, `use_global` — same as `Memory`.

#### Methods

Same surface as `Memory`: `add()`, `search()`, `delete()`,
`list()`, `clear()`. Notes written by an `AgentMemory` carry
the `agent_id` automatically.

---

### `main(argv=None)` function

The `agentic-memory` console-script entry point. Provides a small
CLI so the package is usable end-to-end without the MCP server.

```python
from agentic_memory import main

# Equivalent to `agentic-memory add "..."` on the command line
main(["add", "User prefers dark mode"])
```

**Usage (from the shell):**

```bash
agentic-memory add <text> [tags...]
agentic-memory search <query> [--limit N]
agentic-memory list [--limit N]
agentic-memory stats
```

---

## Internal Functions (Not Public API)

The following are documented for maintainers. They live in internal
modules and may change without notice. Use the `Memory` / `AgentMemory`
classes above (or the MCP server) instead.

### `enqueue_task(conn, task_type, payload=None, priority=0)`

**Real signature** (from `background_queue.py:71`):

```python
from background_queue import enqueue_task
import sqlite3

conn = sqlite3.connect("/path/to/memory.db")
task_id = enqueue_task(
    conn,
    task_type="entity_resolution",
    payload={"memory_id": "lessons/foo"},
    priority=1,
)
```

**Parameters:**
- `conn` (`sqlite3.Connection`, required) — Open DB connection. The
  caller owns the transaction; this function does not commit.
- `task_type` (str, required) — One of: `entity_resolution`,
  `fact_consolidation`, `contradiction_check`, `cross_session_learn`,
  `duplicate_detection`.
- `payload` (dict, optional) — JSON-serializable task payload (default: None).
- `priority` (int, optional) — Higher = picked first (default: 0).

**Returns:** The task id (int). If an identical pending task already
exists, the existing id is returned (dedup).

---

## MCP Tools

See [MCP Tools Reference](reference/mcp-tools.md) for the full list
of 102 registered MCP tools (15 CORE + 84 ADMIN + 3 DEPRECATED).

## CLI

The repo ships standalone CLI wrappers for the most common ops.
Each script uses `sys.argv[1]` for the primary argument (no
subcommand parser), and supports a small set of positional args
and `--` flags.

```bash
# Search memories (positional query, optional positional limit, optional --no-global)
python search_memory.py "my query" 10
python search_memory.py "my query" 5 --no-global

# Rebuild index (positional source dir)
python rebuild_index.py /path/to/memory

# Backup database
python cron/cron_backup.py

# Check integrity (--fix to repair)
python memory_integrity.py --fix
```

For the full CLI surface, use the installed `agentic-memory-*`
console scripts (see `pyproject.toml [project.scripts]`).
