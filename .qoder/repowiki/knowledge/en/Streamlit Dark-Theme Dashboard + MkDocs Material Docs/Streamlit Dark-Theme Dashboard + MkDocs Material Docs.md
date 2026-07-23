---
kind: frontend_style
name: Streamlit Dark-Theme Dashboard + MkDocs Material Docs
category: frontend_style
scope:
    - '**'
source_files:
    - dashboard.py
    - dashboard/__init__.py
    - mkdocs.yml
---

This repository contains two distinct frontend-facing surfaces, each with its own styling approach:

1. Operator Dashboard (Streamlit) — A dark-themed, multi-tab Streamlit application (dashboard.py + dashboard/) that provides an in-process console for browsing memories, the knowledge graph, audit logs, compliance, coordination, billing, and system health. Styling is implemented entirely via a single large inline CSS block defined as the CSS string constant in dashboard/__init__.py (lines 31-344) and injected through st.html(CSS) at app startup in dashboard.py. The theme uses a dark palette (#0e1117, #1a1d23, #2d3139, #f0f2f6, #8b5cf6 accent), custom scrollbar styling, metric cards, badges, progress tracks, log boxes, and animated tab indicators. Plotly charts are themed via px.defaults.template = "plotly_dark" plus a shared DARK dict of colors. There is no external CSS file, Tailwind, or component library — all visual customization lives in this one Python module.

2. Public Documentation Site (MkDocs Material) — The docs site (docs/ built by mkdocs.yml) uses the Material for MkDocs theme with a dual light/dark palette (default/slate schemes, indigo primary/accent). Customization is declarative in mkdocs.yml under theme.palette and theme.features; there is no custom CSS override file present.

Key files:
- dashboard.py — Streamlit entrypoint; sets page config, injects CSS, wires tabs to renderers
- dashboard/__init__.py — Central CSS string constant, DARK plotly theme dict, shared helpers
- dashboard/sidebar.py, dashboard/tab_*.py — Tab renderer modules (no per-tab CSS; they rely on shared classes like .metric-card, .card, .badge-ok, etc.)
- mkdocs.yml — MkDocs Material theme configuration with light/dark palettes

Conventions developers should follow:
- New dashboard UI elements should reuse existing CSS class names from the shared CSS block rather than defining ad-hoc styles inline.
- Keep the dark color tokens consistent with the existing palette (#0e1117 background, #1a1d23 card bg, #2d3139 borders, #f0f2f6 text, #8b5cf6 accent).
- For new plots, extend the shared DARK dict instead of overriding Plotly defaults per chart.
- Do not introduce separate CSS files or Tailwind — the project's style system is intentionally centralized in dashboard/__init__.py.