# REST API Reference

## Base URL

```
http://localhost:9878
```

## Authentication

```bash
# Set API token
export MEMORY_API_TOKEN=your-token-here

# Use in requests
curl -H "Authorization: Bearer $TOKEN" http://localhost:9878/api/v1/memories
```

## Endpoints

### Memories

#### Create Memory

```http
POST /api/v1/memories
Content-Type: application/json

{
  "content": "User prefers dark mode",
  "tags": ["ui", "preferences"],
  "category": "preferences",
  "is_global": true,
  "pinned": false,
  "importance": 3
}
```

**Response:**
```json
{
  "id": "preferences/user-prefers-dark-mode"
}
```

#### Search Memories

```http
GET /api/v1/search?q=dark+mode&limit=5&rerank=true
```

**Query Parameters:**
- `q` — Search query (required)
- `limit` — Max results (default: 5)
- `rerank` — Enable reranking (default: true)
- `include_global` — Include global memories (default: true)

**Response:**
```json
{
  "results": [
    {
      "id": "preferences/user-prefers-dark-mode",
      "content": "User prefers dark mode",
      "score": 0.95,
      "tags": ["ui", "preferences"],
      "category": "preferences",
      "created_at": "2026-07-10T04:00:00Z",
      "pinned": false,
      "importance": 3
    }
  ],
  "count": 1
}
```

#### Get Memory

```http
GET /api/v1/memories/{id}
```

#### List Memories

```http
GET /api/v1/memories?limit=50&offset=0&category=preferences
```

#### Delete Memory

```http
DELETE /api/v1/memories/{id}?hard=false
```

#### Restore Memory

```http
POST /api/v1/memories/{id}/restore
```

### Statistics

```http
GET /api/v1/stats
```

**Response:**
```json
{
  "memories": 1234,
  "vector_keys": 1200,
  "chunks": 3456,
  "facts": 89,
  "entities": 45,
  "relations": 120
}
```

### Health Check

```http
GET /api/v1/health
```

**Response:**
```json
{
  "status": "healthy",
  "db": "connected",
  "uptime": 3600
}
```

### Knowledge Graph

#### List Entities

```http
GET /api/v1/kg/entities?limit=50
```

#### List Relations

```http
GET /api/v1/kg/relations?limit=50
```

#### Get Entity

```http
GET /api/v1/kg/entities/{id}
```

### Temporal Facts

#### Search Facts

```http
GET /api/v1/temporal/facts?q=user+preferences&limit=10
```

#### Get Contradictions

```http
GET /api/v1/temporal/contradictions?limit=10
```

### System

#### Maintenance

```http
POST /api/v1/maintenance/check
```

#### System Health

```http
GET /api/v1/system/health
```

---

## WebSocket Events

Connect to `ws://localhost:9878/ws` for real-time events:

```json
{
  "type": "memory.saved",
  "data": {
    "id": "preferences/user-prefers-dark-mode",
    "content": "User prefers dark mode"
  }
}
```

**Event Types:**
- `memory.saved` — New memory created
- `memory.updated` — Memory updated
- `memory.deleted` — Memory deleted
- `kg.entity_added` — New entity extracted
- `kg.relation_added` — New relation extracted

---

## Error Responses

```json
{
  "error": "Not found",
  "message": "Memory with id 'xyz' not found",
  "status": 404
}
```

**Status Codes:**
- `200` — Success
- `201` — Created
- `400` — Bad request
- `401` — Unauthorized
- `404` — Not found
- `500` — Internal server error

---

## CORS

Configure allowed origins:

```bash
export MEMORY_API_CORS_ORIGINS="http://localhost:3000,http://localhost:8080"
```

---

## Rate Limiting

The API server includes built-in rate limiting:

```bash
# Default: 60 requests per minute per IP
export MEMORY_API_RATE_LIMIT=60
```
