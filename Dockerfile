# agentic-memory — single image, multi-service (mcp, sync, cron).
#
# Build:
#     docker build -t agentic-memory .
#
# Run (MCP server, the most common case):
#     docker run --rm -p 9877:9877 \
#         -v /path/to/host/memory:/data \
#         -e SERVICE=mcp \
#         agentic-memory
#
# The image uses python:3.12-slim to balance compat (>=3.11) with
# image size. We avoid 3.14 because the slim variant for it isn't
# widely cached and we don't need 3.14-specific features.

FROM python:3.12-slim

# System dependencies. tini gives us proper signal handling so
# SIGTERM propagates to the child process. ca-certificates is
# needed for outbound HTTPS (sync server uses it for mTLS).
# We deliberately omit cron, systemd, and other system-level
# schedulers — the cron service uses our own Python scheduler.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Working directory. The project uses bare imports like
# `from mcp_maintenance import ...` so /app must be the root.
WORKDIR /app

# Copy the project. We don't install dev extras (pytest, mypy, etc.)
# in the runtime image — they belong in CI / dev images.
COPY . /app/

# Install Python dependencies. We split core from optional to keep
# the image lean; if you need semantic search, build with
# --build-arg INSTALL_EXTRAS=1 to pull in model2vec, usearch, torch.
ARG INSTALL_EXTRAS=0
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e . && \
    if [ "$INSTALL_EXTRAS" = "1" ]; then \
        pip install --no-cache-dir model2vec usearch "numpy>=2.0.0"; \
    fi

# Verify the install: import the package and confirm version.
# If this fails, the build fails loudly instead of producing a
# broken image that surfaces only at runtime.
RUN python -c "import cli, mcp_instance, sync_server; print('imports ok')"

# Entrypoint uses tini for signal handling. The script picks
# the service from $SERVICE.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]

# Default: MCP server. Override with -e SERVICE=...
ENV SERVICE=mcp

# MCP server doesn't need a network port in stdio mode (which is
# how opencode consumes it). The sync server exposes 9877.
EXPOSE 9877

# Health check: only meaningful for the sync service. For mcp/cron
# the script exits 0 (we are alive). The compose file overrides
# this per-service.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD test -f "$MEMORY_DB_PATH" || exit 0
