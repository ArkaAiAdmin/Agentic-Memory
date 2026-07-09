# TypeScript SDK API Reference

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
