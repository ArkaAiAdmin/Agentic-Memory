# Build the Documentation Site

## Goal

Build and deploy the public documentation site using Material for MkDocs from the `docs/` tree.

## Prerequisites

- [ ] Agentic Memory installed with docs extras (`pip install -e ".[docs]"`)
- [ ] Python 3.10+
- [ ] Write access to the repository (for deployment)

The public docs at [agentic-memory.ar...(TBD)](https://...)
are built with [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/)
from the `docs/` tree + `mkdocs.yml` at the repo root.

## Steps

### 1. Install

```sh
pip install -e ".[docs]"
```

This pulls in `mkdocs` and `mkdocs-material`. Other plugins we
use (`pymdownx.*`) ship with Material.

### 2. Local preview

```sh
mkdocs serve
```

Opens `http://127.0.0.1:8000` with live reload — edit a `.md`
file and the browser updates instantly.

### 3. Build static site

```sh
mkdocs build --clean
```

Output goes to `site/`. This is the directory you serve from
GitHub Pages, Netlify, S3, etc.

### 4. Deploy to GitHub Pages

```sh
mkdocs gh-deploy
```

Pushes `site/` to the `gh-pages` branch. The site goes live at
`https://<user>.github.io/Agentic-Memory/`.

## Verification

```sh
# Check the built site
mkdocs build --clean
echo "Site built to site/ with $(ls site/ | wc -l) top-level entries"

# Or for live preview
mkdocs serve &
sleep 2
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000
```

Expected output: `mkdocs build --clean` exits with exit code 0 and no errors. The live preview returns HTTP 200.

## Authoring conventions

The docs follow the [Diataxis](https://diataxis.fr/) structure:

| Section       | Purpose                                   |
|---------------|-------------------------------------------|
| `concepts/`   | Understand the why                        |
| `how-to/`     | Real-world workflows                      |
| `reference/`  | API / config / schema lookup              |
| `explanation/`| Deeper rationale, history, comparisons    |

When you add a new page, also add it to the `nav:` block in
`mkdocs.yml` so it shows up in the sidebar.

## Build hooks

`docs/_hooks/copy_docker_readme.py` symlinks `docker/README.md`
into `docs/docker_README.md` so the nav can reference it. The
hook runs on `pre_build` and unlinks on `post_build`, so the
working tree stays clean unless a build is in progress.

## Related

- [Add an MCP Tool](add-an-mcp-tool.md) — How to add new tool docs
- [Architecture](../architecture.md) — System architecture overview
- [MCP Tools Reference](../reference/mcp-tools.md) — Full tool documentation

## Troubleshooting

* **`mkdocs serve` warns "unresolved tag: python/name"** —
  those are the YAML `!!python/name:` tags for the emoji
  extension. They're a PyYAML feature mkdocs needs; the linter
  doesn't understand them but mkdocs does. Safe to ignore.
* **Broken link warning** — some docs link to maintainer files
  (`AGENTS.md`, `memory_workflow.md`, `lessons/*.md`) which are
  not in the public site. We build with `strict: false` so the
  build succeeds and emits a warning. Fix by either:
    * Removing the link if it's maintainer-only context
    * Adding the file to `nav:` if it should be public
