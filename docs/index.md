# Documentation

Welcome to the Agentic Memory documentation.

## Get Started

| Page | Description |
|------|-------------|
| [Quick Start](quick-start.md) | Get running in 5 minutes (includes all install methods: PyPI, source, Docker) |

## Concepts

Understand how Agentic Memory works and why it's designed this way.

| Page | Description |
|------|-------------|
| [Why Markdown](concepts/why-markdown.md) | Why markdown files are the source of truth |
| [Search Pipeline](concepts/search-pipeline.md) | How hybrid BM25 + semantic + KG search works |
| [Knowledge Graph](concepts/knowledge-graph.md) | Entity extraction, relationships, and deduplication |
| [Temporal Knowledge Graph](concepts/temporal-kg.md) | Bi-temporal facts: event_time, contradiction detection, edit invalidation, time-aware queries |
| [Tier System](concepts/tier-system.md) | Hot/warm/cold memory lifecycle |
| [Background Tasks](concepts/background-tasks.md) | Async processing queue and workers |
| [Security Model](concepts/security-model.md) | Injection detection, data isolation, no-telemetry |

## How-To Guides

Real-world workflows and recipes.

| Page | Description |
|------|-------------|
| [Integrate with Claude Code](how-to/integrate-claude-code.md) | Set up MCP server for Claude Code |
| [Multi-Project Sharing](how-to/multi-project.md) | Share memories across projects |
| [Extend Entity Types](how-to/custom-entity-types.md) | Add domain-specific NER patterns |
| [Debug Search](how-to/debug-search.md) | Troubleshoot search quality issues |
| [Set Up Cron Jobs](how-to/cron-setup.md) | Background processing schedule |
| [Add an MCP Tool](how-to/add-an-mcp-tool.md) | Maintainer: add a new tool to the MCP server |
| [Add a Cron Job](how-to/add-a-cron-job.md) | Maintainer: add a new background job |
| [Add a Claude Code Hook](how-to/add-a-claude-code-hook.md) | Maintainer: add a new lifecycle hook |
| [Run a Schema Migration](how-to/run-a-migration.md) | Maintainer: schema change procedure |

## Reference

Complete API and configuration documentation.

| Page | Description |
|------|-------------|
| [API Reference](api-reference.md) | All public functions and classes |
| [MCP Tools](reference/mcp-tools.md) | 79 tools (15 CORE + 64 ADMIN) for agent integration |
| [Configuration](reference/configuration.md) | Environment variables and `memory.toml` |
| [Database Schema](reference/schema.md) | Tables, indexes, and migration history (current: v20) |

## For Maintainers

If you're working *on* the agentic-memory system itself (not just *using* it), see:

- [`AGENTS.md`](../AGENTS.md) — the maintainer contract: rules, hard constraints, session protocol
- [`memory_workflow.md`](../memory_workflow.md) — system reference: pipelines, hooks, config, troubleshooting
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — coding conventions, test patterns, PR rules
- [`skills/`](../skills/) — maintainer-specific skills (memory-architecture, add-an-mcp-tool, add-a-cron-job, add-a-claude-code-hook)

## Explanation

Deeper context and rationale.

| Page | Description |
|------|-------------|
| [Design Decisions](explanation/design-decisions.md) | Why we chose SQLite, markdown, BM25, etc. |
| [Boot Sequence](explanation/boot-sequence.md) | What happens when you open a terminal, run opencode, and create a new session — traced from `hooks.json` |
| [Comparison](explanation/comparison.md) | How Agentic Memory compares to alternatives |
