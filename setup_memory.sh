#!/usr/bin/env bash
# setup_memory.sh — Bootstraps the Agentic Memory System in any repository.
set -euo pipefail

GLOBAL_DIR="$HOME/.config/agentic-memory"
LOCAL_DIR="memory"

echo "=== Initializing Agentic Memory ==="

# 1. Ensure Global Memory exists
if [ ! -d "$GLOBAL_DIR" ]; then
    echo "Global memory not found at $GLOBAL_DIR"
    echo "Please run the global setup first or ensure ~/.config/agentic-memory exists"
    exit 1
fi

# 2. Install the package via pip if the CLI commands aren't available
if ! command -v agentic-memory-server &>/dev/null; then
    echo "agentic-memory CLI not found. Installing via pip..."
    if command -v pip3 &>/dev/null; then
        pip3 install --quiet agentic-memory
    elif command -v pip &>/dev/null; then
        pip install --quiet agentic-memory
    else
        echo "WARNING: pip not found. CLI commands will not be available."
        echo "  Install manually: pip install agentic-memory"
    fi
fi

# 3. Create Local Directory Structure
echo "Creating local memory folders..."
mkdir -p "$LOCAL_DIR"/{projects,lessons,preferences,sessions}

# 4. Create the Symbolic Link to Global Memory
ln -sf "$GLOBAL_DIR" "$LOCAL_DIR/global"
echo "Created symbolic link: $LOCAL_DIR/global -> $GLOBAL_DIR"

# 5. Create the Local Memory Index (MEMORY.md)
cat << 'EOF' > "$LOCAL_DIR/MEMORY.md"
---
created: 2026-01-01T00:00:00
updated: 2026-01-01T00:00:00
observed_at: 2026-01-01T00:00:00
tags: [index, memory-system]
pinned: true
importance: 5
decay: none
related: []
---

# Agentic Memory Index

## Active Projects

## Architecture Decisions (ADRs)

## Hard-Won Lessons

## User Preferences
EOF

# 6. Write Default Topic Files (only create if missing)
if [ ! -f "$LOCAL_DIR/preferences/workflow.md" ]; then
    cat << 'EOF' > "$LOCAL_DIR/preferences/workflow.md"
---
created: 2026-01-01T00:00:00
updated: 2026-01-01T00:00:00
observed_at: 2026-01-01T00:00:00
tags: [workflow, preferences]
pinned: true
importance: 4
decay: none
related: []
---

# User preferences: Development Workflow

## Architecture & Code Quality
- **MVVM Architecture**: Always prioritize clean Model-View-ViewModel (MVVM) separation for mobile applications.
- **No Mocks**: Avoid mock frameworks in testing. Test native behavior and logic rather than mocking dependencies.
- **Refinement**: Value detailed, proactive code reviews. Push back on poor structural ideas or shortcuts rather than rubber-stamping.

## Memory Upkeep
- **Durable Memory**: Always update the local and global memory indexes when new conventions or learnings arise.
- **Compact Often**: When requested to "compact the memory", distill the current session events into a clean, permanent summary to keep context window efficient.
EOF
else
    echo "  Skipping preferences/workflow.md (already exists, preserving user content)."
fi

# 7. Create skills directory if it doesn't exist
mkdir -p "$HOME/.agents/skills"

# 8. Append Agent Briefing to AGENTS.md (idempotent)
MARKER_START="# >>> Agentic Memory System >>>"
MARKER_END="# <<< Agentic Memory System <<<"
echo "Writing agent instructions to AGENTS.md..."

# Remove old block if marker exists
if grep -q "$MARKER_START" AGENTS.md 2>/dev/null; then
    sed "/$MARKER_START/,/$MARKER_END/d" AGENTS.md > AGENTS.md.tmp && mv AGENTS.md.tmp AGENTS.md
fi

cat << EOF >> AGENTS.md

$MARKER_START
# Agentic Memory System

## Context
This project uses the **Agentic Memory System** for persistent, cross-session context.
Global config: $GLOBAL_DIR
Local memory: $LOCAL_DIR/

## Setup
Run \`pip install agentic-memory\`.

## Bootstrapping — MANDATORY memory search at session start
At session start, agents MUST:
1. Read $LOCAL_DIR/MEMORY.md (the index)
2. Read $LOCAL_DIR/preferences/tools.md (tool preferences)
3. Search relevant memories using: agentic-memory-search <query>

## MANDATORY Memory Save Rules
You are REQUIRED to save memories. This is not optional. After ANY of these events:

### Save Immediately (don't wait, don't ask):
- You solved a bug → save a lesson with the fix and root cause
- You made an architectural decision → save an ADR in decisions/
- You learned a new API pitfall → save to global lessons/
- You established a coding convention → save to global preferences/
- You fixed a tricky issue → save the pattern for future reference
- You discovered how a library/API works → save the knowledge

### Save at Session End (before yielding):
- Run: python3 -m agentic_memory.session_reflect "$LOCAL_DIR"
- This prompts you to save anything you learned during the session
- If the script finds unsaved context, YOU MUST save it before finishing

### How to Save:
Use the MCP tool: memory_save(content, category, title_slug, tags, pinned, is_global)
Or CLI: agentic-memory-search <query>

### Global vs Local:
- Lessons, pitfalls, preferences → is_global=true (shared across all projects)
- Project-specific decisions, context → is_global=false (local to this project)

## Memory Upkeep
- **Search**: agentic-memory-search <query>
- **Rebuild index**: agentic-memory-rebuild $LOCAL_DIR $LOCAL_DIR/memory.db
- **Compact**: agentic-memory-compact
- **Session reflect**: python3 -m agentic_memory.session_reflect $LOCAL_DIR

## Cross-Project Knowledge
- Global memories (in $GLOBAL_DIR) are symlinked at $LOCAL_DIR/global/
- They are searchable and shared across ALL projects
- Local memories stay in the repo (git-tracked)

$MARKER_END
EOF

# 9. Append Instructions to CLAUDE.md (idempotent)
MARKER_START="# >>> Agentic Memory System >>>"
MARKER_END="# <<< Agentic Memory System <<<"
echo "Writing agent instructions to CLAUDE.md..."

# Remove old block if marker exists
if grep -q "$MARKER_START" CLAUDE.md 2>/dev/null; then
    sed "/$MARKER_START/,/$MARKER_END/d" CLAUDE.md > CLAUDE.md.tmp && mv CLAUDE.md.tmp CLAUDE.md
fi

cat << EOF >> CLAUDE.md

$MARKER_START
# Agentic Memory System

## Setup
Run \`pip install agentic-memory\`.

## Agent Workflow
At session start — MANDATORY memory search:
  Before answering ANY question, search for relevant memories:
  \`\`\`bash
  agentic-memory-search "topic keywords"
  \`\`\`
  This takes <100ms and gives you context from past sessions.

During work — MANDATORY memory saves:
  - Solved a bug → memory_save(fix + root cause, "lessons", slug, tags, pinned=true, is_global=true)
  - Made a decision → memory_save(decision + rationale, "decisions", slug, tags, pinned=false, is_global=false)
  - Found a pitfall → memory_save(pitfall, "lessons", slug, tags, pinned=true, is_global=true)
  - Learned a pattern → memory_save(pattern, "lessons", slug, tags, pinned=false, is_global=true)

At session end — REQUIRED reflection:
  \`\`\`bash
  python3 -m agentic_memory.session_reflect $LOCAL_DIR
  \`\`\`
  Save any unsaved learnings before yielding.

Maintenance (weekly):
  - agentic-memory-compact - Full tier migration + consolidation + rebuild
  - memory_audit()         - Health check report
  - Review compaction-proposal.md in memory/sessions/

$MARKER_END
EOF

echo "=== Agentic Memory Initialized Successfully ==="
echo ""
echo "CLI commands available after install:"
echo "  agentic-memory-server    — Run the MCP server"
echo "  agentic-memory-search    — Search memories"
echo "  agentic-memory-rebuild   — Rebuild indexes"
echo "  agentic-memory-compact   — Full compaction"
echo "  agentic-memory-bootstrap — Bootstrap a project"
