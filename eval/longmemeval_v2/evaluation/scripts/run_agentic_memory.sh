#!/usr/bin/env bash
# Run AgenticMemoryBackend on LongMemEval-V2 (small tier, both domains).
#
# Prerequisites:
#   1. Download data:  python3 data/download_data.py --data-root data/longmemeval-v2
#   2. Prepare data:   python3 data/prepare_data.py --data-root data/longmemeval-v2 --mode symlink
#   3. Validate data:  python3 data/validate_data.py --data-root data/longmemeval-v2 --tier small
#   4. Install deps:   pip install sentence-transformers
#
# Environment variables:
#   READER_BASE_URL    — OpenAI-compatible reader endpoint (required)
#   READER_MODEL       — Reader model name (default: Qwen/Qwen3.5-9B)
#   OPENAI_API_KEY     — API key for LLM judge (required for gotchas/abstention eval)
#
# Usage:
#   export READER_BASE_URL=http://localhost:8023/v1
#   export READER_MODEL=Qwen/Qwen3.5-9B
#   export OPENAI_API_KEY=sk-...
#   bash evaluation/scripts/run_agentic_memory.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

DATA_ROOT="${DATA_ROOT:-data/longmemeval-v2}"
OUTPUT_ROOT="${OUTPUT_ROOT:-runs}"
TIER="${TIER:-small}"

METHOD="agentic_memory"
METHOD_NAME="${METHOD}_${TIER}"

# Haystack file — single file covering both web and enterprise domains
HAYSTACK_FILE="${DATA_ROOT}/haystacks/lme_v2_${TIER}.json"

echo "=== LongMemEval-V2: ${METHOD} (${TIER} tier) ==="
echo "DATA_ROOT: ${DATA_ROOT}"
echo "OUTPUT_ROOT: ${OUTPUT_ROOT}"

# Validate data exists
if [ ! -f "${DATA_ROOT}/questions.jsonl" ]; then
    echo "ERROR: questions.jsonl not found in ${DATA_ROOT}. Run download_data.py first."
    exit 1
fi
if [ ! -f "${DATA_ROOT}/trajectories.jsonl" ]; then
    echo "ERROR: trajectories.jsonl not found in ${DATA_ROOT}. Run download_data.py first."
    exit 1
fi
if [ ! -f "${HAYSTACK_FILE}" ]; then
    echo "ERROR: Haystack file not found: ${HAYSTACK_FILE}"
    exit 1
fi

# Run web domain
echo ""
echo "--- Running web domain ---"
python3 evaluation/harness.py \
    --domain web \
    --questions-path "${DATA_ROOT}/questions.jsonl" \
    --haystack-path "${HAYSTACK_FILE}" \
    --trajectories-path "${DATA_ROOT}/trajectories.jsonl" \
    --memory-config-path evaluation/memory_configs/${METHOD}.json \
    --output-dir "${OUTPUT_ROOT}/${METHOD}_web_${TIER}" \
    --model "${READER_MODEL:-Qwen/Qwen3.5-9B}" \
    --base-url "${READER_BASE_URL:-}" \
    --evaluator-model "${EVALUATOR_MODEL:-}" \
    --evaluator-base-url "${EVALUATOR_BASE_URL:-}" \
    --memory-context-max-tokens 200000 \
    --max-completion-tokens 20000 \
    --reader-max-concurrent-requests 500

echo "Web domain results: ${OUTPUT_ROOT}/${METHOD}_web_${TIER}/aggregated_metrics.json"

# Run enterprise domain
echo ""
echo "--- Running enterprise domain ---"
python3 evaluation/harness.py \
    --domain enterprise \
    --questions-path "${DATA_ROOT}/questions.jsonl" \
    --haystack-path "${HAYSTACK_FILE}" \
    --trajectories-path "${DATA_ROOT}/trajectories.jsonl" \
    --memory-config-path evaluation/memory_configs/${METHOD}.json \
    --output-dir "${OUTPUT_ROOT}/${METHOD}_enterprise_${TIER}" \
    --model "${READER_MODEL:-Qwen/Qwen3.5-9B}" \
    --base-url "${READER_BASE_URL:-}" \
    --evaluator-model "${EVALUATOR_MODEL:-}" \
    --evaluator-base-url "${EVALUATOR_BASE_URL:-}" \
    --memory-context-max-tokens 200000 \
    --max-completion-tokens 20000 \
    --reader-max-concurrent-requests 500

echo "Enterprise domain results: ${OUTPUT_ROOT}/${METHOD}_enterprise_${TIER}/aggregated_metrics.json"

# Combine metrics
echo ""
echo "--- Combining metrics ---"
python3 leaderboard/combine_aggregated_metrics.py \
    "${OUTPUT_ROOT}/${METHOD}_web_${TIER}/aggregated_metrics.json" \
    "${OUTPUT_ROOT}/${METHOD}_enterprise_${TIER}/aggregated_metrics.json" \
    -o "${OUTPUT_ROOT}/${METHOD}_${TIER}_combined_metrics.json"

echo ""
echo "=== Combined results: ${OUTPUT_ROOT}/${METHOD}_${TIER}_combined_metrics.json ==="
cat "${OUTPUT_ROOT}/${METHOD}_${TIER}_combined_metrics.json"
