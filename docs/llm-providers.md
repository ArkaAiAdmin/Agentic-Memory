# Local LLM Provider Setup (Ollama / Llama.cpp)

Agentic Memory now supports three LLM providers for fact extraction,
entity extraction, and contradiction scoring. This guide shows how
to set each one up.

## Quick Comparison

| Provider | Install effort | Memory | First-call latency | Privacy |
|----------|----------------|--------|-------------------|---------|
| **Ollama** | One binary | 2-4 GB | ~2-5s (model load) | 100% local |
| **Llama.cpp** | One binary | 1-3 GB | ~1-3s | 100% local |
| **HuggingFace** | `pip install transformers torch` | 5-10 GB | ~3-8s | 100% local |

For most users, **Ollama is recommended** — it handles model
download, quantization, and concurrency for you.

---

## Option 1: Ollama (Recommended)

### Install Ollama

macOS:
```bash
brew install ollama
# or download from https://ollama.ai/download
```

Linux:
```bash
curl -fsSL https://ollama.ai/install.sh | sh
```

### Start the server and pull a model

```bash
# Start the server (runs on http://localhost:11434)
ollama serve &

# Pull a small but capable model (~2 GB)
ollama pull qwen2.5:3b
# Alternatives:
#   ollama pull llama3.2:3b        # Meta's 3B
#   ollama pull phi3:mini          # Microsoft's 3.8B
#   ollama pull gemma2:2b          # Google's 2B
```

### Configure agentic-memory

Add to `memory.toml`:
```toml
[llm_extraction]
provider = "ollama"
ollama_host = "http://localhost:11434"
ollama_model = "qwen2.5:3b"
ollama_timeout_s = 30.0
```

Or via environment variables:
```bash
export MEMORY_LLM_PROVIDER=ollama
export MEMORY_OLLAMA_HOST=http://localhost:11434
export MEMORY_OLLAMA_MODEL=qwen2.5:3b
```

### Verify

```bash
curl http://localhost:11434/api/tags
# Should list qwen2.5:3b
```

The first time agentic-memory runs, it will detect Ollama and use it
automatically (you'll see "LLM provider selected: ollama" in the
logs).

---

## Option 2: Llama.cpp

Best for users who want to run a specific GGUF model without
Ollama's abstractions.

### Install llama.cpp

```bash
# macOS
brew install llama.cpp

# Or build from source
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp && make
```

### Start the server

```bash
# Download a GGUF model (e.g., from HuggingFace)
llama-server -m model.gguf -c 4096 --port 8080
```

### Configure agentic-memory

```toml
[llm_extraction]
provider = "llama_cpp"
llama_cpp_host = "http://localhost:8080"
# model is empty = use the server's loaded model
```

---

## Option 3: HuggingFace (Default, Original)

The original implementation. Requires the heaviest dependencies.

```bash
pip install transformers torch accelerate
```

The default model is `Qwen/Qwen2.5-3B-Instruct` (3B parameters).
Override with `MEMORY_LLM_EXTRACTION_MODEL_ID=...`.

---

## Fallback Chain

If the preferred provider is unavailable, agentic-memory
**automatically falls back** in this order:

1. The provider named in `MEMORY_LLM_PROVIDER` (default: `huggingface`)
2. `ollama` (if not already the preferred)
3. `llama_cpp` (if not already the preferred)
4. `huggingface` (if not already the preferred)

If all providers fail, the system returns empty extraction results
and callers fall back to regex-only extraction. The system never
blocks on a missing LLM.

This means you can install Ollama later and the system will
auto-detect it without any config change.

---

## Troubleshooting

### "LLM provider selected: none" in logs
The system couldn't reach any provider. Check:
- Is the server running? (`curl http://localhost:11434/api/tags`)
- Is the host/port correct? (Check `MEMORY_OLLAMA_HOST`)
- Is there a firewall blocking the connection?

### Extraction returns empty
- Check the model is loaded: `ollama list`
- Increase `ollama_timeout_s` if the model is slow to load
- Check the prompt is being sent correctly (see `eval/test_llm_providers.py`)

### Slow first call
The first call after server startup loads the model into memory
(2-5s for 3B models). Subsequent calls are fast (~100-500ms).

### Out of memory
Try a smaller model: `ollama pull qwen2.5:1.5b` or `gemma2:2b`.

---

## Why This Matters

Before this change, fact extraction required:
- `transformers` (~500 MB pip install)
- `torch` (~2 GB pip install)
- ~5 GB of RAM for the model in memory
- Apple Silicon MPS or CUDA GPU for acceptable latency

With Ollama:
- One `brew install` (~50 MB)
- ~2 GB for a 3B model
- Runs on CPU at acceptable latency
- No Python deps for the model

The `llm_providers.py` module is ~400 lines, has zero required
dependencies beyond the stdlib, and gracefully degrades to
regex-only extraction if no provider is available.
