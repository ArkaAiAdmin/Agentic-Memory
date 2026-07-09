# Self-Hosting Guide

Deploy Agentic Memory on your own infrastructure.

## Docker (Recommended)

### Quick Start

```bash
git clone https://github.com/ArkaAiAdmin/Agentic-Memory.git
cd Agentic-Memory
docker compose up -d
```

This starts:
- MCP server on `http://localhost:8080`
- Persistent volume at `/data/memory.db`

### With Dashboard

```bash
docker compose --profile dashboard up -d
```

Adds a web dashboard on `http://localhost:8081`.

### Configuration

Override defaults via environment variables:

```yaml
services:
  mcp-server:
    environment:
      - MEMORY_DB_PATH=/data/memory.db
      - MEMORY_CONSOLIDATION=1
      - MEMORY_KNOWLEDGE_GRAPH=1
      - MEMORY_EMBEDDINGS=1
```

## From Source

### Prerequisites

- Python 3.11+
- Git

### Installation

```bash
git clone https://github.com/ArkaAiAdmin/Agentic-Memory.git
cd Agentic-Memory

python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

pip install -e ".[all]"
```

### Running

```bash
# Start MCP server
agentic-memory-server

# Or run individual commands
agentic-memory-rebuild               # Rebuild FTS5 index
agentic-memory-worker                # Process background tasks
agentic-memory-integrity             # Health check
```

### Systemd Service

Create `/etc/systemd/system/agentic-memory.service`:

```ini
[Unit]
Description=Agentic Memory MCP Server
After=network.target

[Service]
Type=simple
User=agentic
WorkingDirectory=/opt/agentic-memory
ExecStart=/opt/agentic-memory/venv/bin/agentic-memory-server
Restart=always
RestartSec=10
Environment=MEMORY_DB_PATH=/data/memory.db

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable agentic-memory
sudo systemctl start agentic-memory
```

## Cron Jobs

Agentic Memory uses a consolidated scheduler that runs every 5 minutes:

```bash
# Install the scheduler (replaces 39 individual cron entries)
bash cron/install_crontab.sh
```

The scheduler checks which jobs are due by frequency tier and runs them sequentially. Configure in `cron/jobs.py`.

## Reverse Proxy (Nginx)

```nginx
server {
    listen 443 ssl;
    server_name memory.example.com;

    ssl_certificate /etc/letsencrypt/live/memory.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/memory.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Backup

### Automated

```bash
# Run daily backup
python cron/cron_backup.py

# Backups stored at: ~/.config/agentic-memory/backups/
```

### Manual

```bash
cp ~/.config/agentic-memory/memory.db /backup/memory-$(date +%Y%m%d).db
```

### Restore

```bash
systemctl stop agentic-memory
cp /backup/memory-20260611.db ~/.config/agentic-memory/memory.db
systemctl start agentic-memory
```

## Monitoring

### Health Check

```bash
agentic-memory-integrity
```

### Database Stats

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/memory.db')
print(f'Memories: {conn.execute(\"SELECT COUNT(*) FROM memories\").fetchone()[0]}')
print(f'Chunks: {conn.execute(\"SELECT COUNT(*) FROM memory_chunks\").fetchone()[0]}')
print(f'KG Entities: {conn.execute(\"SELECT COUNT(*) FROM kg_entities\").fetchone()[0]}')
print(f'KG Facts: {conn.execute(\"SELECT COUNT(*) FROM kg_facts\").fetchone()[0]}')
"
```

## Troubleshooting

### Database locked

If you see "database is locked":
1. Ensure only one writer process is running
2. Check that WAL mode is enabled: `PRAGMA journal_mode=WAL`
3. Increase busy timeout: `PRAGMA busy_timeout=5000`

### Slow search

1. Rebuild the index: `agentic-memory-rebuild`
2. Check FTS5 integrity: `agentic-memory-integrity`
3. Verify embeddings are computed: check `memory_embeddings` table

### Missing memories

1. Check the markdown source files exist
2. Rebuild the index from markdown: `python rebuild_index.py`
3. Check file permissions
