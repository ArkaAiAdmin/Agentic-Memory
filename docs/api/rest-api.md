# REST API Reference

## Overview

The REST API lets any HTTP client — `curl`, Postman, browser JS, scripts — interact with Agentic Memory without installing any SDK. The server runs as a standalone process and exposes endpoints for CRUD operations, search, knowledge graph traversal, system health, and maintenance. All responses are JSON.

## Quick Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/memories` | Create a new memory |
| GET | `/api/v1/memories/search?query=...` | Search memories (GET) |
| POST | `/api/v1/memories/search` | Search memories (POST, complex queries) |
| GET | `/api/v1/memories/{id}` | Get a single memory |
| GET | `/api/v1/memories` | List memories |
| GET | `/api/v1/memories/stats` | System statistics |
| DELETE | `/api/v1/memories/{id}` | Soft-delete a memory |
| POST | `/api/v1/memories/clear` | Clear all SDK-created memories |
| GET | `/api/v1/kg/nodes` | List KG entities |
| GET | `/api/v1/kg/edges` | List KG edges |
| POST | `/api/v1/maintenance/rebuild` | Rebuild FTS5 index |
| POST | `/api/v1/maintenance/compact` | Compact database |
| POST | `/api/v1/maintenance/integrity` | Run integrity check |
| POST | `/api/v1/compliance/gdpr/erase` | GDPR right-to-be-forgotten |
| GET | `/health` | Health check |
| WS | `ws://localhost:9878/ws` | Real-time events |

## Base URL

```
http://localhost:9878
```

## Authentication

All endpoints require a Bearer token (`Authorization: Bearer <token>`). The server operates securely by default (`insecure_loopback=False`). The `insecure_loopback=True` bypass is intended strictly for local development and must never be enabled in production environments.

```bash
export MEMORY_API_TOKEN=your-token-here

# Include in every request
curl -H "Authorization: Bearer $MEMORY_API_TOKEN" http://localhost:9878/api/v1/memories
```

---

## Endpoints

### Health Check

```
GET /health
```

No authentication required.

**curl:**
```bash
curl http://localhost:9878/health
```

**Response (200):**
```json
{
  "status": "healthy",
  "note_count": 1234
}
```

> SEC (LOW-2): unauthenticated `/health` intentionally does not expose
> `agent_id` or `version` — identity/metadata are stripped to avoid leaking
> deployment info to any localhost caller.

---

### Create Memory

```
POST /api/v1/memories
Content-Type: application/json
```

**Request Body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `content` | string | Yes | — | Text content to store |
| `tags` | string[] | No | `[]` | Tag strings |
| `category` | string | No | `"sdk"` | Memory category |
| `is_global` | boolean | No | `false` | Store at global scope |
| `pinned` | boolean | No | `false` | Boost in recall |

**curl:**
```bash
curl -X POST http://localhost:9878/api/v1/memories \
  -H "Authorization: Bearer $MEMORY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "User prefers dark mode",
    "tags": ["ui", "preferences"],
    "category": "preferences",
    "is_global": true,
    "pinned": false
  }'
```

**Response (201):**
```json
{
  "id": "preferences/user-prefers-dark-mode",
  "status": "success"
}
```

**Errors:**

| Status | Meaning |
|--------|---------|
| 400 | Missing `content` field or invalid JSON |
| 401 | Authorization required (no Bearer token) |
| 403 | Invalid token |
| 500 | Internal server error |

---

### Search Memories (GET)

```
GET /api/v1/memories/search?query=...&limit=10&rerank=true
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | (required) | Search query |
| `limit` | int | `10` | Max results |
| `rerank` | bool | `true` | Enable cross-encoder reranking |
| `tags` | string | — | Comma-separated tag filter |

**curl:**
```bash
curl "http://localhost:9878/api/v1/memories/search?query=dark+mode&limit=5&rerank=true" \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
```json
{
  "results": [
    {
      "id": "preferences/user-prefers-dark-mode",
      "content": "User prefers dark mode",
      "score": 0.95,
      "tags": ["ui", "preferences"],
      "category": "preferences",
      "created_at": "2026-07-10T04:00:00Z"
    }
  ]
}
```

**Errors:**

| Status | Meaning |
|--------|---------|
| 400 | Missing `query` parameter |
| 401 | Authorization required |
| 500 | Search pipeline failure |

---

### Search Memories (POST)

```
POST /api/v1/memories/search
Content-Type: application/json
```

Use for complex queries with body parameters instead of query strings.

**Request Body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | Yes | — | Search query |
| `limit` | int | No | `10` | Max results |
| `rerank` | boolean | No | `true` | Enable reranking |
| `tags` | string[] | No | — | Tag filter |

**curl:**
```bash
curl -X POST http://localhost:9878/api/v1/memories/search \
  -H "Authorization: Bearer $MEMORY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "dark mode", "limit": 10, "rerank": true, "tags": ["ui"]}'
```

**Response (200):**
```json
{
  "results": [
    {
      "id": "preferences/user-prefers-dark-mode",
      "content": "User prefers dark mode",
      "score": 0.95,
      "tags": ["ui", "preferences"],
      "category": "preferences",
      "created_at": "2026-07-10T04:00:00Z"
    }
  ]
}
```

---

### Get Memory

```
GET /api/v1/memories/{id}
```

**curl:**
```bash
curl http://localhost:9878/api/v1/memories/preferences/user-prefers-dark-mode \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
```json
{
  "id": "preferences/user-prefers-dark-mode",
  "content": "User prefers dark mode",
  "tags": ["ui", "preferences"],
  "category": "preferences",
  "created_at": "2026-07-10T04:00:00Z",
  "updated_at": null,
  "deleted_at": null
}
```

**Errors:**

| Status | Meaning |
|--------|---------|
| 401 | Authorization required |
| 404 | Memory not found |
| 410 | Memory soft-deleted (recoverable) |

---

### List Memories

```
GET /api/v1/memories?limit=50&offset=0
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | `50` | Max results per page |
| `offset` | int | `0` | Skip first N results |

**curl:**
```bash
curl "http://localhost:9878/api/v1/memories?limit=10&offset=0" \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
```json
{
  "memories": [
    {
      "id": "preferences/user-prefers-dark-mode",
      "content": "User prefers dark mode",
      "tags": ["ui", "preferences"],
      "category": "preferences",
      "created_at": "2026-07-10T04:00:00Z",
      "pinned": false,
      "importance": 3
    }
  ]
}
```

**Errors:**

| Status | Meaning |
|--------|---------|
| 401 | Authorization required |
| 500 | Database query failure |

---

### Delete Memory

```
DELETE /api/v1/memories/{id}
```

Performs a soft-delete by default. The note is recoverable for 30 days.

**curl:**
```bash
curl -X DELETE http://localhost:9878/api/v1/memories/preferences/user-prefers-dark-mode \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
```json
{
  "success": true
}
```

**Errors:**

| Status | Meaning |
|--------|---------|
| 401 | Authorization required |
| 404 | Memory not found or already deleted |

---

### Clear Memories

```
POST /api/v1/memories/clear
```

Deletes all memories created via the SDK (source_file LIKE `sdk-%`).

**curl:**
```bash
curl -X POST http://localhost:9878/api/v1/memories/clear \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
```json
{
  "cleared": 42
}
```

---

### Statistics

```
GET /api/v1/memories/stats
```

**curl:**
```bash
curl http://localhost:9878/api/v1/memories/stats \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
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

---

### Knowledge Graph — Nodes

```
GET /api/v1/kg/nodes?limit=100
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | `100` | Max entities to return |

**curl:**
```bash
curl "http://localhost:9878/api/v1/kg/nodes?limit=50" \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
```json
{
  "nodes": [
    {
      "id": 1,
      "name": "dark_mode",
      "type": "preference",
      "properties": {"source": "user"}
    }
  ]
}
```

---

### Knowledge Graph — Edges

```
GET /api/v1/kg/edges?limit=100
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | `100` | Max edges to return |

**curl:**
```bash
curl "http://localhost:9878/api/v1/kg/edges?limit=50" \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
```json
{
  "edges": [
    {
      "source": 1,
      "target": 2,
      "relation": "PREFERS",
      "weight": 0.9,
      "properties": {}
    }
  ]
}
```

---

### Maintenance — Rebuild

```
POST /api/v1/maintenance/rebuild
```

Rebuilds the FTS5 full-text search index from scratch.

**curl:**
```bash
curl -X POST http://localhost:9878/api/v1/maintenance/rebuild \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
```json
{
  "success": true
}
```

**Errors:**

| Status | Meaning |
|--------|---------|
| 500 | Index rebuild failed |

---

### Maintenance — Compact

```
POST /api/v1/maintenance/compact
```

Compacts the database (VACUUM + index optimization).

**curl:**
```bash
curl -X POST http://localhost:9878/api/v1/maintenance/compact \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
```json
{
  "success": true,
  "result": "Compacted: 1200 pages freed"
}
```

---

### Maintenance — Integrity Check

```
POST /api/v1/maintenance/integrity
```

Runs a database integrity check.

**curl:**
```bash
curl -X POST http://localhost:9878/api/v1/maintenance/integrity \
  -H "Authorization: Bearer $MEMORY_API_TOKEN"
```

**Response (200):**
```json
{
  "success": true,
  "errors": []
}
```

**Response with errors (200):**
```json
{
  "success": false,
  "errors": ["FTS5 index out of sync", "vec_key count mismatch"]
}
```

---

### GDPR Erase

```
POST /api/v1/compliance/gdpr/erase
Content-Type: application/json
```

GDPR Right-to-Be-Forgotten. The target tenant is resolved from the authenticated principal, not the request body.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `data_subject_sub` | string | Yes | External subject identifier |

**curl:**
```bash
curl -X POST http://localhost:9878/api/v1/compliance/gdpr/erase \
  -H "Authorization: Bearer $MEMORY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"data_subject_sub": "user-12345"}'
```

**Response (200):**
```json
{
  "success": true,
  "erased_count": 15
}
```

**Errors:**

| Status | Meaning |
|--------|---------|
| 400 | Missing `data_subject_sub` field |
| 401 | Authorization required |
| 403 | Requires `compliance:gdpr-erase` role |

---

## WebSocket Events

Connect to `ws://localhost:9878/ws` for real-time events:

```bash
# Connect — token via RFC 6455 Sec-WebSocket-Protocol subprotocol
# (browser-safe: the chosen subprotocol is echoed in the 101 handshake)
curl -i -N \
  -H "Upgrade: websocket" \
  -H "Connection: Upgrade" \
  -H "Sec-WebSocket-Protocol: $MEMORY_API_TOKEN" \
  "http://localhost:9878/ws"

# Or via Authorization header (programmatic clients)
curl -i -N \
  -H "Upgrade: websocket" \
  -H "Connection: Upgrade" \
  -H "Authorization: Bearer $MEMORY_API_TOKEN" \
  "http://localhost:9878/ws"
```

Auth-in-URL (`?token=`) is **not** supported for WebSocket — credentials must
never appear in request URLs (access logs, proxies). If a subprotocol is
offered but none match the configured token, the handshake fails closed
with `401`.

**Event format:**
```json
{
  "event": "memory_event",
  "data": {
    "id": 1,
    "event_type": "saved",
    "note_id": "preferences/user-prefers-dark-mode",
    "payload": {"content": "User prefers dark mode"},
    "created_at": "2026-07-10T04:00:00Z"
  }
}
```

**Supported WebSocket actions (client → server):**
```json
{"action": "ping"}
{"action": "search", "query": "dark mode", "limit": 10}
```

**Event Types:**
- `saved` — New memory created
- `updated` — Memory updated
- `deleted` — Memory deleted
- `entity_added` — New KG entity extracted
- `relation_added` — New KG relation extracted

---

## Error Responses

All error responses follow this format:

```json
{
  "error": "Not found",
  "status": 404
}
```

**Status Codes:**

| Code | Meaning |
|------|---------|
| 200 | Success |
| 201 | Created |
| 400 | Bad request (missing fields, invalid JSON) |
| 401 | Authorization required (no Bearer token) |
| 403 | Forbidden (invalid token or insufficient role) |
| 404 | Resource not found |
| 410 | Resource soft-deleted (recoverable) |
| 500 | Internal server error |

---

## Configuration

The API server reads these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_API_TOKEN` | (none) | Bearer token for auth |
| `MEMORY_API_CORS_ORIGINS` | `*` | Allowed CORS origins (comma-separated) |
| `MEMORY_DB_PATH` | `./memory.db` | Database path |
| `API_LISTEN_HOST` | `127.0.0.1` | Bind address |
| `API_LISTEN_PORT` | `9878` | Bind port |

## CORS

Configure allowed origins:

```bash
export MEMORY_API_CORS_ORIGINS="http://localhost:3000,http://localhost:8080"
```

## Stripe Webhooks

The API server supports receiving Stripe webhook events at `/api/v1/stripe/webhook`.

### Webhook Verification Policy (Fail-Closed)
Webhook signature verification is strictly enforced across all environments. Every incoming webhook request must provide the `Stripe-Signature` header and match the configured `STRIPE_WEBHOOK_SECRET`. Requests with missing secrets, missing signatures, or invalid signatures are rejected with HTTP 400.

### Local Development & Testing with Stripe CLI
To test Stripe webhook integration locally with signature verification:

1. Install the Stripe CLI (`brew install stripe/stripe-cli/stripe` or from stripe.com).
2. Forward webhook events to your local API server:
   ```bash
   stripe listen --forward-to localhost:9879/api/v1/stripe/webhook
   ```
3. Copy the webhook signing secret output by the CLI (format: `whsec_...`):
   ```
   > Ready! Your webhook signing secret is whsec_1234567890abcdef...
   ```
4. Set `STRIPE_WEBHOOK_SECRET` in your environment before running the server:
   ```bash
   export STRIPE_WEBHOOK_SECRET=whsec_1234567890abcdef...
   ```
5. In another terminal, trigger a test event:
   ```bash
   stripe trigger checkout.session.completed
   ```

## Rate Limiting

The API server includes built-in rate limiting (if configured):

```bash
export MEMORY_API_RATE_LIMIT=60  # requests per minute per IP
```

## Troubleshooting

### `401 Unauthorized` on every request

**Cause**: The server requires a token but the client isn't sending it.

**Fix**: Include the header: `curl -H "Authorization: Bearer $MEMORY_API_TOKEN" http://localhost:9878/api/v1/memories`.

### CORS errors from browser JavaScript

**Cause**: `MEMORY_API_CORS_ORIGINS` is empty or doesn't include your origin.

**Fix**: `export MEMORY_API_CORS_ORIGINS="http://localhost:3000"` and restart the server.

### Empty search results when data exists

**Cause**: FTS5 or vector index out of sync.

**Fix**: `curl -X POST http://localhost:9878/api/v1/maintenance/rebuild -H "Authorization: Bearer $MEMORY_API_TOKEN"`

---

## Related

- [Python SDK](python-sdk.md) — Programmatic Python access
- [TypeScript SDK](typescript-sdk.md) — TypeScript/Node.js client
- [MCP Tools Reference](../reference/mcp-tools.md) — MCP tool equivalents
- [Configuration Reference](../reference/configuration.md) — All server config options
