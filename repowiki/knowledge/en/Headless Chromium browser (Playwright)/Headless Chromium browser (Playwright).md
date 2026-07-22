---
kind: external_dependency
name: Headless Chromium browser (Playwright)
slug: playwright
category: external_dependency
category_hints:
    - client_constraint
scope:
    - '**'
---

Playwright is an optional dependency (installed via the `dashboard` extra) used to drive a headless Chromium instance for agent-side dashboard verification and e2e pages. The `.playwright-mcp/` directory contains recorded page sessions. The chromium binary must be installed separately via `python -m playwright install chromium`.