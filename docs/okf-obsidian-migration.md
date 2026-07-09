# Exporting agentic-memory to Obsidian / Logseq

Your agent's memory lives in plain-text Markdown files. You already
own your data — this guide shows you how to make other tools aware of it.

## Step 1: Export via OKF

### Via MCP Tool (Recommended)

```python
from agentic_memory import MemoryClient
mc = MemoryClient()
mc.okf_export("~/ObsidianVault/agent-memory")
```

Or via MCP:

```
memory_okf_export(output_dir="~/ObsidianVault/agent-memory")
```

### Via CLI

```bash
cd ~/.config/agentic-memory
venv/bin/python okf_export.py \
  memory/memory.db \
  ~/ObsidianVault/agent-memory
```

`okf_export.py` takes two positional arguments:

1. `<memory.db>` — path to the SQLite database (e.g. `memory/memory.db`)
2. `<target_dir>` — where to write the exported Markdown bundle

Optional flags:
  `--include-deleted`   include soft-deleted memories
  `--overwrite`         replace existing files in the target directory
  `--no-validate`       skip OKF conformance check after export

This produces:
  ~/ObsidianVault/agent-memory/
    index.md          ← table of contents (OKF bundle index)
    lessons/          ← one .md per lesson note
    decisions/        ← one .md per decision note
    projects/         ← one .md per project note
    preferences/      ← one .md per preference note
    sessions/         ← one .md per session summary
    ... (one folder per category)

Each `.md` file has YAML frontmatter (tags, importance, created,
superseded_by, etc.) plus the full note body in Markdown.

Frontmatter keys written per Google OKF v0.1 §4:

  type, title, description, resource, tags, pinned, timestamp,
  related, valid_from, valid_to, superseded_by,
  created, updated, observed_at, category, title_slug

## Step 2: Import into Obsidian

1. Open Obsidian → Settings → Community Plugins
2. Install "Obsidian Git" (for sync) and "Dataview" (for querying)
3. Copy the exported folder into your vault: `~/ObsidianVault/agent-memory/`
4. Create a note that indexes everything using Dataview:

```dataview
TABLE importance, created
FROM "agent-memory"
WHERE file.name != "index"
SORT importance DESC
```

## Step 3: Import into Logseq

1. Open Logseq → Settings → General → Import
2. Select "Markdown files"
3. Point to the exported directory
4. Logseq will import each `.md` as a page with frontmatter as properties

## Step 4: Keep in sync

Run the export command periodically (add to cron for hands-free sync):

```bash
# Add to crontab (daily at 2am):
0 2 * * * cd ~/.config/agentic-memory && venv/bin/python okf_export.py memory/memory.db ~/ObsidianVault/agent-memory >> /tmp/okf-sync.log 2>&1
```

To re-import modified notes back into agentic-memory:

```bash
cd ~/.config/agentic-memory
venv/bin/python okf_import.py ~/ObsidianVault/agent-memory   # dry-run (preview)
venv/bin/python okf_import.py ~/ObsidianVault/agent-memory --dry-run   # explicit
# omit --dry-run to actually write:
venv/bin/python okf_import.py ~/ObsidianVault/agent-memory --overwrite   # overwrite existing
```

`okf_import.py` reads every `.md` (excluding `index.md`), parses the
OKF frontmatter, and re-saves each note through `save_memory`. It
round-trips cleanly with `okf_export.py` — category, tags, timestamps,
pinned status, valid_from/to, and superseded_by are all preserved.
