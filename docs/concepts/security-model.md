# Security Model

Agentic Memory is designed with **privacy-first principles**. No data leaves your machine unless you explicitly choose to share it.

## What is the Security Model?

The security model defines the **privacy guarantees, threat mitigations, and operational boundaries** of Agentic Memory. It covers data isolation (local-first architecture, per-project databases), injection detection (demoting suspicious content in search), access control (localhost-only MCP server, file permissions), and resilience (markdown as source of truth, rebuildable indices).

## Why it matters

Agentic Memory stores **private human memories** — lessons, decisions, preferences, and project context. A security lapse could expose this data to unauthorized agents, exfiltrate it over the network, or let malicious content poison the agent's behavior. The security model provides clear boundaries: what the system protects, what it does not, and how operators can harden their deployment.

## Core Security Properties

### 1. Local-First

All data stays on your machine:

- **No cloud sync** — No automatic uploads to any server
- **No telemetry** — No usage data sent anywhere
- **No API keys required** — Core system uses only local computation
- **No network by default** — MCP server binds to localhost only

```mermaid
graph LR
    A[Your Machine] --> B[Agentic Memory - memory.db + *.md]
    B -->|NO NETWORK| C[The Internet - nothing]
```

### 2. No LLM in the Write Path

Memory saves use **deterministic extraction only**:

- Entity extraction: regex patterns (no API calls)
- Fact extraction: regex patterns (no API calls)
- Search ranking: BM25 + cosine similarity (no API calls)

The only LLM-dependent feature is **semantic embeddings** (optional), which run locally via `model2vec`.

### 3. Injection Detection

The `memory_injection.py` module detects and demotes **prompt injection attempts** in memory content:

```python
# Detection patterns
INJECTION_PATTERNS = {
    "imperative": r"^(ignore|forget|disregard|override)\s+",
    "system_prompt": r"(you are|act as|pretend to be|roleplay as)",
    "tool_invocation": r"(run|execute|call|invoke)\s*\(",
    "roleplay": r"(imagine|suppose|hypothetically|what if)",
}

def scan_injection(content):
    """Returns risk assessment for content."""
    risk_score = 0
    matches = []
    for category, pattern in INJECTION_PATTERNS.items():
        if re.search(pattern, content, re.IGNORECASE):
            risk_score += 1
            matches.append(category)
    return {"is_suspicious": risk_score > 0, "risk_score": risk_score, "matches": matches}
```

Suspicious content is **demoted in search results** but not deleted — you can review and approve it.

### 4. Data Isolation

- **Per-project databases** — Each project has its own `memory.db`
- **Global memory is opt-in** — Must explicitly save with `is_global=True`
- **No cross-project access** — Projects can't read each other's memories
- **Symlink-based sharing** — Only shared via explicit `MEMORY.md` links

## Threat Model

### What We Protect Against

| Threat | Mitigation |
|--------|------------|
| **Prompt injection** | `memory_injection.py` detects and demotes suspicious content |
| **Data exfiltration** | No network by default, no telemetry |
| **Unauthorized access** | Local-only MCP server, file permissions |
| **Data corruption** | Markdown is source of truth, rebuildable index |
| **Vendor lock-in** | Plain markdown files, open format |

### What We Don't Protect Against

| Limitation | Rationale |
|------------|-----------|
| **Physical access** | If someone has your filesystem, they have your data |
| **Malicious agents** | An agent with write access can save anything |
| **Side-channel attacks** | Timing attacks on search are possible |
| **Encrypted storage** | Not implemented (use OS-level encryption) |

## MCP Server Security

The MCP server binds to **localhost only** by default:

```json
{
    "agentic-memory": {
        "command": "python3",
        "args": ["~/.config/agentic-memory/memory_mcp.py"],
        "env": { "PYTHONPATH": "~/.config/agentic-memory" }
    }
}
```

### If You Need Remote Access

Use a reverse proxy with authentication:

```nginx
server {
    listen 443 ssl;
    server_name memory.example.com;
    
    ssl_certificate /etc/letsencrypt/live/memory.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/memory.example.com/privkey.pem;
    
    # Add your auth layer here
    auth_basic "Memory Server";
    auth_basic_user_file /etc/nginx/.htpasswd;
    
    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}
```

## Docker Security

The Dockerfile runs as a **non-root user**:

```dockerfile
RUN adduser --disabled-password --gecos "" agentic
USER agentic
WORKDIR /app
```

The volume mount persists data outside the container:

```yaml
volumes:
    - agentic-data:/data
```

## Key behaviors

- **No LLM in the write path**: Entity extraction, fact extraction, and search ranking use deterministic regex and BM25 — no API calls. The only LLM-dependent feature is semantic embeddings, which run locally via `model2vec`.
- **Injection detection demotes, does not delete**: Suspicious content is flagged and demoted in search results but preserved in the database. An operator can review and approve it via `memory_scan_injection`.
- **Per-project isolation**: Each project has its own `memory.db`. Global memory (`is_global=True`) is opt-in. Cross-project access requires explicit configuration.
- **Audit logging** (Accountable Operations): The `memory_audit_log` table records every MCP tool invocation with timestamp, arguments, error state, and latency. Audit logs are append-only.
- **Sync server security**: The MCP sync server binds to `127.0.0.1` by default. Remote access requires TLS, optional mTLS, HMAC signing token, and CORS configuration.
- **Markdown rebuild guarantees**: If the database is compromised, the markdown files remain as the trusted source of truth. Run `rebuild_index.py` to recover.

## Configuration

The security model is configuration-free in its default state (no env vars to set for local-first operation). For remote access, configure the sync server:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_SYNC_TOKEN` | — | Required auth token for sync server |
| `MEMORY_SYNC_HMAC_SECRET` | — | Optional HMAC signing secret |
| `MEMORY_SYNC_TLS_CERT` | — | TLS certificate path |
| `MEMORY_SYNC_TLS_KEY` | — | TLS key path |
| `MEMORY_SYNC_TLS_CLIENT_CA` | — | mTLS client CA path |
| `MEMORY_SYNC_CORS_ORIGINS` | — | CORS allowed origins (empty = no CORS) |

See [Configuration Reference](../reference/configuration.md) and [Self-Hosting](../self-hosting.md) for full details.

## Best Practices

1. **Use file permissions** — `chmod 600 memory.db` restricts access
2. **Enable WAL mode** — Prevents corruption during concurrent access
3. **Regular backups** — `cron/cron_backup.py` automates this
4. **Review injected content** — Check `memory_injection.py` flags periodically
5. **Use `.gitignore`** — Never commit `memory.db` or `*.db` files
6. **Audit logs** — `memory_audit_log` tracks all operations

## Troubleshooting

### Suspicious content is being demoted

`memory_injection.py` scans for imperative, system-prompt, tool-invocation, and roleplay patterns. A legitimate memory that triggers these patterns is demoted (not deleted). Review and approve it via `memory_scan_injection`, or refine the patterns. See [Reference: MCP Tools](../reference/mcp-tools.md).

### Need to expose the server remotely

The MCP/sync server is localhost-only by default. For remote access, use a reverse proxy with authentication and TLS, or configure the sync server's `MEMORY_SYNC_*` TLS/HMAC settings. See [Self-Hosting](../self-hosting.md).

### Database corrupted

Markdown files are the source of truth. Delete `memory.db` and run `rebuild_index.py` to recover from the `.md` files. Nothing is lost. See [Why Markdown](why-markdown.md) and [Durability](../durability.md).

## Related

- [Why Markdown](why-markdown.md) — Why markdown is the source of truth (data corruption resistance)
- [Multi-Agent Sync](multi-agent-sync.md) — Sync server security and TLS configuration
- [How to Set Up Cron Jobs](../how-to/cron-setup.md) — Backup and integrity-check cron jobs
- [Configuration Reference](../reference/configuration.md) — All env vars including sync security
- [Self-Hosting](../self-hosting.md) — Deploy securely on your infrastructure
- [MCP Tools Reference](../reference/mcp-tools.md) — Audit tool surface and error handling
