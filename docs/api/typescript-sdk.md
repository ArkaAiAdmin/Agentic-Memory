# TypeScript SDK API Reference

## Overview

The TypeScript SDK (`@agentic-memory/sdk`) provides a native TypeScript interface to Agentic Memory over HTTP. Use it in Node.js applications, web frontends, and any TypeScript project that needs to persist and search memories. The SDK communicates with the memory server via REST + WebSocket.

## Quick Reference

| Method | Description | Returns |
|--------|-------------|---------|
| `new MemoryClient(options)` | Create a new client | Client instance |
| `.add(content, options)` | Save a memory | Promise\<string\> |
| `.search(query, options)` | Search memories | Promise\<SearchResult[]\> |
| `.get(id)` | Get a single memory | Promise\<MemoryResult \| null\> |
| `.list(options)` | List recent memories | Promise\<MemoryResult[]\> |
| `.delete(id, hard)` | Soft/hard delete | Promise\<boolean\> |
| `.restore(id)` | Restore from soft-delete | Promise\<boolean\> |
| `.stats()` | System statistics | Promise\<Stats\> |

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

## Classes

### MemoryClient

```typescript
class MemoryClient {
  constructor(options?: {
    baseUrl?: string;  // Default: http://127.0.0.1:9878
    token?: string;    // API auth token
  });
}
```

#### Methods

##### add()

Save a memory.

```typescript
async add(
  content: string,
  options?: {
    tags?: string[];
    category?: string;     // Default: 'sdk'
    isGlobal?: boolean;    // Default: true
    pinned?: boolean;      // Default: false
  }
): Promise<string>
```

**Returns:** Memory ID

**Example:**
```typescript
const id = await client.add('User prefers dark mode', {
  tags: ['ui', 'preferences'],
  category: 'preferences',
});
```

##### search()

Search memories.

```typescript
async search(
  query: string,
  options?: {
    limit?: number;        // Default: 5
    rerank?: boolean;      // Default: true
    includeGlobal?: boolean;
    tags?: string[];
  }
): Promise<SearchResult[]>
```

**Returns:** Array of search results

**Example:**
```typescript
const results = await client.search('dark mode', { limit: 10 });
results.forEach(r => {
  console.log(`[${r.score}] ${r.content}`);
});
```

##### get()

Get a memory by ID.

```typescript
async get(id: string): Promise<MemoryResult | null>
```

##### list()

List recent memories.

```typescript
async list(options?: {
  limit?: number;         // Default: 50
  offset?: number;        // Default: 0
  category?: string;
}): Promise<MemoryResult[]>
```

##### delete()

Delete a memory.

```typescript
async delete(id: string, hard?: boolean): Promise<boolean>
```

##### restore()

Restore a soft-deleted memory.

```typescript
async restore(id: string): Promise<boolean>
```

##### stats()

Get system statistics.

```typescript
async stats(): Promise<Stats>
```

---

## Types

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
  memories: number;
  vector_keys: number;
  chunks: number;
  facts: number;
  entities: number;
  relations: number;
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

## Troubleshooting

### Symptom: `ECONNREFUSED` on client creation

**Cause**: The memory server is not running.

**Fix**: Start the server: `agentic-memory-server` or `python memory_mcp.py`.

### Symptom: `401 Unauthorized` on API calls

**Cause**: The API token doesn't match the server's `MEMORY_API_TOKEN`.

**Fix**: Verify the token in your client constructor matches the server's env var.

### Symptom: WebSocket connection drops after a few seconds

**Cause**: The server may be behind a proxy that doesn't support WebSocket upgrades, or the server's CORS config blocks the client origin.

**Fix**: Check `MEMORY_API_CORS_ORIGINS` and ensure `ws://` protocol is allowed through the proxy.

## Related

- [Python SDK](python-sdk.md) — Python equivalent
- [REST API](rest-api.md) — Raw HTTP interface
- [MCP Tools Reference](../reference/mcp-tools.md) — MCP tool equivalents

## WebSocket

The SDK also supports real-time updates via WebSocket:

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
