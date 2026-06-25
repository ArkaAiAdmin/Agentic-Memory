#!/usr/bin/env bash
# Local MCP server runner for the agentic-memory live config.
# Independent of the shippable package at ~/Desktop/agentic-memory-shippable/.
#
# Routes:
#  * cli.py:               local server entry point
#  * mcp_instance/mcp_tools/memory_mcp:  all loaded from CWD (root-level files, bare imports)
set -euo pipefail

CONFIG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${CONFIG_ROOT}/venv/bin/python"

if [[ ! -x "${VENV_PY}" ]]; then
    echo "FATAL: live-config venv python not found at ${VENV_PY}" >&2
    exit 1
fi

export MEMORY_DB_PATH="${MEMORY_DB_PATH:-${CONFIG_ROOT}/memory/memory.db}"
export MEMORY_LOCAL_DIR="${MEMORY_LOCAL_DIR:-${CONFIG_ROOT}/memory}"
export PYTHONPATH="${CONFIG_ROOT}:${PYTHONPATH:-}"

exec "${VENV_PY}" "${CONFIG_ROOT}/cli.py" "server"
