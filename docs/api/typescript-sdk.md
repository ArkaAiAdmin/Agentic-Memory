# TypeScript SDK API Reference

## Overview

The TypeScript SDK (`@agentic-memory/sdk`) provides a native TypeScript interface to Agentic Memory over HTTP. Use it in Node.js applications, web frontends, and any TypeScript project that needs to persist and search memories. The SDK communicates with the memory server via REST + WebSocket.

## Installation

```bash
npm install @agentic-memory/sdk
```

## Quick Start

```typescript
import { MemoryClient } from '@agentic-memory/sdk';

const client = new MemoryClient();

// Save
const id = await client.add('User prefers dark mode');

// Search
const results = await client.search('dark mode');
console.log(results);
```

---

## Quick Reference

| Method | Description | Returns |
|--------|-------------|---------|
| `new MemoryClient(options?)` | Create a new client | `MemoryClient` |
| `.add(content, options?)` | Save a memory | `Promise<string>` |
| `.search(query, options?)` | Search memories | `Promise<SearchResult[]>` |
| `.get(id)` | Get a single memory | `Promise<MemoryResult \| null>` |
| `.list(options?)` | List recent memories | `Promise<MemoryResult[]>` |
| `.delete(id, hard?)` | Soft/hard delete | `Promise<boolean>` |
| `.restore(id)` | Restore from soft-delete | `Promise<boolean>` |
| `.stats()` | System statistics | `Promise<Stats>` |

---

## Classes

### MemoryClient

```typescript
class MemoryClient {
  constructor(options?: MemoryClientOptions);
}
```

**Constructor Options:**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `baseUrl` | `string` | `"http://127.0.0.1:9878"` | Memory server URL |
| `token` | `string` | — | API auth token (required unless server uses insecure_loopback) |

**Example:**
```typescript
// Default (localhost)
const client = new MemoryClient();

// Custom server + auth
const client = new MemoryClient({
  baseUrl: 'https://memory.example.com',
  token: process.env.MEMORY_API_TOKEN,
});
```

#### Methods

##### add()

Save a memory and return its note ID.

```typescript
async add(
  content: string,
  options?: AddMemoryOptions
): Promise<string>
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `content` | `string` | (required) | Text content to store |
| `options.category` | `string` | `"sdk"` | Memory category |
| `options.tags` | `string[]` | `[]` | Tag strings |
| `options.isGlobal` | `boolean` | `true` | Store at global scope |
| `options.pinned` | `boolean` | `false` | Boost in recall |

**Returns:** `Promise<string>` — The note ID.

**Example:**
```typescript
const id = await client.add('User prefers dark mode', {
  tags: ['ui', 'preferences'],
  category: 'preferences',
  isGlobal: true,
});
console.log(id); // "preferences/user-prefers-dark-mode"
```

##### search()

Search memories by semantic and keyword relevance.

```typescript
async search(
  query: string,
  options?: SearchOptions
): Promise<SearchResult[]>
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `string` | (required) | Natural-language search query |
| `options.limit` | `number` | `5` | Max results |
| `options.rerank` | `boolean` | `true` | Enable cross-encoder reranking |
| `options.includeGlobal` | `boolean` | `true` | Include global memories |
| `options.tags` | `string[]` | — | Filter by tags |

**Returns:** `Promise<SearchResult[]>` — Ranked results.

**Example:**
```typescript
const results = await client.search('dark mode', { limit: 10, rerank: true });
results.forEach(r => {
  console.log(`[${r.score.toFixed(2)}] ${r.content}`);
});
```

##### get()

Get a memory by note ID.

```typescript
async get(id: string): Promise<MemoryResult | null>
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | `string` | (required) | Memory note ID |

**Returns:** `Promise<MemoryResult | null>` — The memory, or null if not found.

**Example:**
```typescript
const note = await client.get('preferences/user-prefers-dark-mode');
if (note) {
  console.log(note.content);
}
```

##### list()

List recent memories.

```typescript
async list(options?: ListOptions): Promise<MemoryResult[]>
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `options.limit` | `number` | `50` | Max results |
| `options.offset` | `number` | `0` | Skip first N results |
| `options.category` | `string` | — | Filter by category |

**Returns:** `Promise<MemoryResult[]>` — Ordered newest first.

**Example:**
```typescript
const memories = await client.list({ limit: 10 });
memories.forEach(m => {
  console.log(`${m.created_at}: ${m.content.substring(0, 50)}`);
});
```

##### delete()

Delete a memory. Default is soft-delete (recoverable for 30 days).

```typescript
async delete(id: string, hard?: boolean): Promise<boolean>
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | `string` | (required) | Memory note ID |
| `hard` | `boolean` | `false` | Permanently delete |

**Returns:** `Promise<boolean>` — True if successful.

**Example:**
```typescript
await client.delete('notes/my-note');              // soft-delete
await client.delete('notes/old-note', true);       // permanent
```

##### restore()

Restore a soft-deleted memory.

```typescript
async restore(id: string): Promise<boolean>
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | `string` | (required) | Memory note ID |

**Returns:** `Promise<boolean>` — True if successful.

**Example:**
```typescript
await client.restore('notes/my-note');
```

##### stats()

Get system statistics.

```typescript
async stats(): Promise<Stats>
```

**Returns:** `Promise<Stats>` — Aggregate counts.

**Example:**
```typescript
const s = await client.stats();
console.log(`${s.memories} memories, ${s.facts} facts`);
```

---

## Types

### MemoryClientOptions

```typescript
interface MemoryClientOptions {
  baseUrl?: string;    // Default: http://127.0.0.1:9878
  token?: string;      // API auth token
}
```

### AddMemoryOptions

```typescript
interface AddMemoryOptions {
  tags?: string[];
  category?: string;      // Default: 'sdk'
  isGlobal?: boolean;     // Default: true
  pinned?: boolean;       // Default: false
}
```

### SearchOptions

```typescript
interface SearchOptions {
  limit?: number;         // Default: 5
  rerank?: boolean;       // Default: true
  includeGlobal?: boolean; // Default: true
  tags?: string[];
}
```

### ListOptions

```typescript
interface ListOptions {
  limit?: number;         // Default: 50
  offset?: number;        // Default: 0
  category?: string;
}
```

### SearchResult

```typescript
interface SearchResult {
  id: string;
  content: string;
  score: number;
  tags: string[];
  category: string;
  created_at: string;
  pinned: boolean;
  importance: number;
  metadata: Record<string, any>;
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | `string` | Note ID |
| `content` | `string` | Text content |
| `score` | `number` | Relevance score (0-1) |
| `tags` | `string[]` | Tag strings |
| `category` | `string` | Category name |
| `created_at` | `string` | ISO 8601 timestamp |
| `pinned` | `boolean` | Pinned flag |
| `importance` | `number` | 1-5 importance weight |
| `metadata` | `Record<string, any>` | Extra metadata |

### MemoryResult

```typescript
interface MemoryResult {
  id: string;
  content: string;
  tags: string[];
  category: string;
  created_at: string;
  pinned: boolean;
  importance: number;
}
```

### Stats

```typescript
interface Stats {
  memories: number;      // Active memory count
  vector_keys: number;   // Vector index entries
  chunks: number;        // Text chunks
  facts: number;         // KG facts
  entities: number;      // KG entities
  relations: number;     // KG edges
}
```

### KGEntity

```typescript
interface KGEntity {
  id: number;
  name: string;
  entity_type: string;
  centrality: number;
}
```

### KGRelation

```typescript
interface KGRelation {
  id: number;
  source_id: number;
  target_id: number;
  relation: string;
  weight: number;
}
```

### Fact

```typescript
interface Fact {
  id: string;
  subject: string;
  predicate: string;
  obj: string;
  confidence: number;                 // 0-1
  source_memory: string;              // Source note ID
  event_time: string;                 // Event timestamp
  event_time_granularity: string;     // Granularity
  valid_at: string;                   // Valid-from
  invalid_at: string;                 // Valid-until
  superseded_by: string;              // Superseding fact ID
  supersedes: string;                 // Superseded fact ID
  contradiction_score: number;        // 0-1
  locked: boolean;
}
```

### IntegrityReport

```typescript
interface IntegrityReport {
  passed: boolean;
  errors: string[];
  warnings: string[];
  stats: Record<string, any>;
}
```

---

## Usage Examples

### Full CRUD Lifecycle

```typescript
import { MemoryClient } from '@agentic-memory/sdk';

const client = new MemoryClient({ token: process.env.MEMORY_API_TOKEN });

// Create
const id = await client.add('User prefers dark mode', {
  tags: ['ui', 'preferences'],
  category: 'preferences',
});
console.log('Created:', id);

// Read
const note = await client.get(id);
console.log('Content:', note?.content);

// Update (delete + re-create)
await client.delete(id);
const newId = await client.add('User now prefers light mode', {
  tags: ['ui', 'preferences'],
  category: 'preferences',
});

// List
const all = await client.list({ category: 'preferences' });

// Stats
const stats = await client.stats();
console.log('System:', stats);
```

### Batch Import

```typescript
import { MemoryClient } from '@agentic-memory/sdk';

const client = new MemoryClient();

const notes = [
  { content: 'User prefers React', tags: ['frontend'] },
  { content: 'User uses PostgreSQL', tags: ['backend'] },
  { content: 'User prefers dark mode', tags: ['ui'] },
];

for (const note of notes) {
  const id = await client.add(note.content, { tags: note.tags });
  console.log(`Saved: ${id}`);
}
```

### Search with Filtering

```typescript
import { MemoryClient } from '@agentic-memory/sdk';

const client = new MemoryClient();

// Basic search
const results = await client.search('frontend preferences');

// Filtered search
const filtered = await client.search('preferences', {
  limit: 5,
  tags: ['ui'],
  rerank: true,
});

// Process results
for (const r of filtered) {
  if (r.score > 0.5 && r.importance >= 3) {
    console.log(`High confidence: ${r.content}`);
  }
}
```

### Pagination

```typescript
import { MemoryClient } from '@agentic-memory/sdk';

const client = new MemoryClient();

let offset = 0;
const pageSize = 20;
let hasMore = true;

while (hasMore) {
  const page = await client.list({ limit: pageSize, offset });
  for (const m of page) {
    console.log(`${m.created_at}: ${m.content.substring(0, 80)}`);
  }
  hasMore = page.length === pageSize;
  offset += pageSize;
}
```

---

## Configuration

The TypeScript SDK connects to a running memory server. Configuration is passed to the constructor:

```typescript
const client = new MemoryClient({
  baseUrl: 'http://127.0.0.1:9878',  // Default
  token: 'your-api-token',            // Optional auth
});
```

Environment variables on the server side control the server's behavior (see [Configuration Reference](../reference/configuration.md) for the full list).

---

## WebSocket

The SDK supports real-time updates via WebSocket:

```typescript
import { MemoryClient } from '@agentic-memory/sdk';

const client = new MemoryClient({ baseUrl: 'http://localhost:9878' });

// Listen for real-time events
client.onMemorySaved((memory) => {
  console.log('New memory:', memory);
});

client.onMemoryUpdated((memory) => {
  console.log('Memory updated:', memory);
});
```

**WebSocket Actions (client → server):**
```json
{"action": "ping"}
{"action": "search", "query": "dark mode", "limit": 10}
```

**Event Types:**
- `memory_event` — CRUD event (saved, updated, deleted)
- `entity_added` — New KG entity extracted
- `relation_added` — New KG relation extracted

---

## Troubleshooting

### `ECONNREFUSED` on client creation

**Cause**: The memory server is not running.

**Fix**: Start the server: `agentic-memory-server` or `python memory_mcp.py`.

### `401 Unauthorized` on API calls

**Cause**: The API token doesn't match the server's `MEMORY_API_TOKEN`.

**Fix**: Verify the token in your client constructor matches the server's env var.

### WebSocket connection drops after a few seconds

**Cause**: The server may be behind a proxy that doesn't support WebSocket upgrades, or the server's CORS config blocks the client origin.

**Fix**: Check `MEMORY_API_CORS_ORIGINS` and ensure `ws://` protocol is allowed through the proxy.

---

## Related

- [Python SDK](python-sdk.md) — Python equivalent
- [REST API](rest-api.md) — Raw HTTP interface
- [MCP Tools Reference](../reference/mcp-tools.md) — MCP tool equivalents
