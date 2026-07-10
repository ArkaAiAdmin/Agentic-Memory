# Walkthrough: Using Agentic Memory

## Goal

Learn the core workflow of the memory system — from your first save to cross-session recall — to understand how the agent persists and retrieves knowledge.

## Prerequisites

- [ ] Agentic Memory installed and configured (see [integrate-claude-code](integrate-claude-code.md))
- [ ] An active session with access to MCP tools (`memory_save`, `memory_search`, etc.)
- [ ] Basic familiarity with the MCP tool interface

## Steps

### 1. Save Your First Memory

Use the `memory_save` tool (or CLI `memory_save.py`) to persist a note:

```
memory_save(
  content: "# Project: My Web App\n\nTech stack: Next.js 14, PostgreSQL, TailwindCSS.\nDeployed on Vercel.",
  category: "projects",
  title_slug: "my-web-app",
  tags: ["project", "web", "nextjs"]
)
```

**What happens under the hood:**
- Content is chunked for FTS5 full-text indexing
- An embedding vector is generated (model2vec, 256d)
- Knowledge graph entities and facts are extracted
- Auto-backlinks are created to related notes
- A background task is queued for entity resolution and fact consolidation

### 2. Search for It

```
memory_search(query: "what tech stack for my web app")
```

**Search pipeline:**
1. FTS5 full-text search (BM25 ranking)
2. Vector similarity search (cosine)
3. Reciprocal Rank Fusion (RRF) merges both result sets
4. Optional reranking (cross-encoder or deep reranker)
5. Quality gates filter low-relevance results
6. User profiling personalizes ranking
7. Auto-summary is included in results if available

### 3. Build Context Over Time

Save more notes about your project:

```
memory_save(content: "# Deployment: Vercel\n\nDeployed to production 2026-06-10. Edge function for auth.", category: "projects", title_slug: "my-web-app-deployment")
memory_save(content: "# Bug: Auth timeout\n\nJWT token expires after 24h but refresh token doesn't rotate. Fix: add refresh rotation.", category: "lessons", title_slug: "auth-timeout-fix")
```

The auto-backlinker will discover relationships between these notes. The knowledge graph builds connections automatically.

### 4. Recall Across Sessions

At the start of a new session, the agent calls:

```
memory_session_start()
```

This returns:
- **Pinned notes**: Important memories you've explicitly pinned
- **Recent session digests**: What happened in recent sessions
- **High-importance memories**: Frequently accessed or high-scoring notes
- **Contextual search results**: Relevant to the current query
- **User profile**: Your preferences and access patterns

### 5. Use Tags for Control

Tags control behavior:

| Tag | Effect |
|-----|--------|
| `pinned` | Note appears in session start recall |
| `auto-summarize` | Note gets auto-summarized if >2000 chars |
| `long` | Triggers summarization when content >2000 chars |
| `summarize` | Explicitly requests summarization |
| `superseded` | Note is hidden from search results |
| `valid_to` | Note expires after the specified date |

### 6. Let the System Learn

The system continuously improves:

- **Adaptive retention**: Notes you access often stay in hot tier; rarely accessed notes cool down
- **Spaced repetition**: Important facts are scheduled for review
- **Contradiction detection**: New notes are checked against existing ones for conflicts
- **Quality gates**: Low-relevance search results are filtered out

## CLI Commands

All MCP tools are also available as CLI scripts:

```bash
# Save a memory
python memory_save.py --category projects --title "my-app" --tags project,web

# Search
python search_memory.py "what tech stack" --limit 5

# Check integrity
python integrity_check.py --deep

# Run health check
python memory_health_check.py

# Daily digest
python memory_daily_digest.py

# Session start
python memory_session_start.py
```

## Configuration

Features are controlled via `memory.toml` (next to `config.py`) with environment variable overrides:

```toml
[features]
temporal_tiers = true
contextual_enrichment = true
quality_gates = true
# ... etc

[search]
rerank_strategy = "hybrid"
deep_rerank_enabled = false
```

Or via environment variables:

```bash
export MEMORY_TEMPORAL_TIERS=1
export MEMORY_CONTEXTUAL_ENRICHMENT=1
```

## Verification

After completing the walkthrough, confirm the system is working:

```bash
# Save a test memory
memory_save(content: "Walkthrough test complete", category: "lessons", title_slug: "walkthrough-test")

# Search for it
memory_search(query: "walkthrough test")

# Start a new session context
memory_session_start()
```

Expected output: The saved memory appears in search results, and `memory_session_start()` returns recent context including your walkthrough activity.

## Troubleshooting

### memory_save returns an error

**Cause**: MCP server not running or database path incorrect.
**Fix**: Check `memory_health_check` and verify `memory.db` exists at the expected path.

### Search returns no results

**Cause**: The index hasn't been built yet or is stale.
**Fix**: Run `agentic-memory-rebuild` to rebuild the FTS5 index.

## Related

- [Integrate with Claude Code](integrate-claude-code.md) — Initial setup
- [Search Pipeline](../concepts/search-pipeline.md) — How search works
- [Knowledge Graph](../concepts/knowledge-graph.md) — How entities are extracted
- [MCP Tools Reference](../reference/mcp-tools.md) — Full tool documentation
