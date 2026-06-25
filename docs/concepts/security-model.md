# Security Model

Agentic Memory is designed with **privacy-first principles**. No data leaves your machine unless you explicitly choose to share it.

## Core Security Properties

### 1. Local-First

All data stays on your machine:

- **No cloud sync** — No automatic uploads to any server
- **No telemetry** — No usage data sent anywhere
- **No API keys required** — Core system uses only local computation
- **No network by default** — MCP server binds to localhost only

```
Your Machine                    The Internet
┌─────────────────┐            ┌─────────────┐
│  Agentic Memory │            │             │
│  ┌───────────┐  │            │  (nothing)  │
│  │ memory.db │  │◄── NO ──▶│             │
│  │ *.md      │  │  NETWORK  │             │
│  └───────────┘  │            └─────────────┘
└─────────────────┘
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

## Best Practices

1. **Use file permissions** — `chmod 600 memory.db` restricts access
2. **Enable WAL mode** — Prevents corruption during concurrent access
3. **Regular backups** — `cron/cron_backup.py` automates this
4. **Review injected content** — Check `memory_injection.py` flags periodically
5. **Use `.gitignore`** — Never commit `memory.db` or `*.db` files
6. **Audit logs** — `memory_audit_log` tracks all operations

## Further Reading

- [Why Markdown](why-markdown.md) — Why markdown is the source of truth
- [Self-Hosting](../self-hosting.md) — Deploy securely on your infrastructure
