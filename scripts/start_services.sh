#!/usr/bin/env bash
# Start the agentic-memory REST API server + Streamlit dashboard for local dev.
# For reboot-stable operation, use scripts/install_launchagents.sh instead
# (installs launchd plists so both come back up at login).
set -euo pipefail
cd "$(dirname "$0")/.."

export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
export HF_HUB_OFFLINE=1
export PYTHONPATH="$(pwd)"
export MEMORY_DB_PATH="${MEMORY_DB_PATH:-$(pwd)/memory/memory.db}"

echo "Starting REST API server on :9879 ..."
nohup venv/bin/python cli.py api --port 9879 --host 127.0.0.1 > memory/rest_api.manual.log 2>&1 &
echo "Starting Streamlit dashboard on :8501 ..."
nohup venv/bin/python venv/bin/streamlit run dashboard.py \
    --server.port 8501 --server.headless true > memory/dashboard.manual.log 2>&1 &

echo "Done. Dashboard: http://localhost:8501  (auto-logs in via memory/.api_token)"
