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

# 2. Create Local Directory Structure
echo "Creating local memory folders..."
mkdir -p "$LOCAL_DIR"/{projects,lessons,preferences,sessions}

# 3. Create the Symbolic Link to Global Memory
if [ -L "$LOCAL_DIR/global" ] || [ -e "$LOCAL_DIR/global" ]; then
    rm -rf "$LOCAL_DIR/global"
fi
ln -s "$GLOBAL_DIR" "$LOCAL_DIR/global"
echo "Created symbolic link: $LOCAL_DIR/global -> $GLOBAL_DIR"

# 4. Create the Local Memory Index (MEMORY.md)
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

# 5. Write Default Topic Files
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

# 6. Create skills directory if it doesn't exist
mkdir -p "$HOME/.agents/skills"

# 7. Append Agent Briefing to AGENTS.md (idempotent)
MARKER_START="# >>> Agentic Memory System >>>"
MARKER_END="# <<< Agentic Memory System <<<"
echo "Writing agent instructions to AGENTS.md..."

# Remove old block if marker exists
if grep -q "$MARKER_START" AGENTS.md 2>/dev/null; then
    sed -i "/$MARKER_START/,/$MARKER_END/d" AGENTS.md
fi

cat << EOF >> AGENTS.md

$MARKER_START
# Agentic Memory System

## Context
This project uses the **Agentic Memory System** for persistent, cross-session context.
Global config: $GLOBAL_DIR
Local memory: $LOCAL_DIR/

## Bootstrapping — MANDATORY memory search at session start
At session start, agents MUST:
1. Read $LOCAL_DIR/MEMORY.md (the index)
2. Read $LOCAL_DIR/preferences/tools.md (tool preferences)
3. Search relevant memories using: python3 $GLOBAL_DIR/search_memory.py <query>

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
- Run: python3 $GLOBAL_DIR/session_reflect.py "$LOCAL_DIR"
- This prompts you to save anything you learned during the session
- If the script finds unsaved context, YOU MUST save it before finishing

### How to Save:
Use the MCP tool: memory_save(content, category, title_slug, tags, pinned, is_global)
Or CLI: python3 $GLOBAL_DIR/memory_mcp.py memory_save --content "..." --category lessons --title-slug "my-lesson" --tags '["tag1"]'

### Global vs Local:
- Lessons, pitfalls, preferences → is_global=true (shared across all projects)
- Project-specific decisions, context → is_global=false (local to this project)

## Memory Upkeep
- **Search**: python3 $GLOBAL_DIR/search_memory.py <query>
- **Rebuild index**: python3 $GLOBAL_DIR/rebuild_index.py $LOCAL_DIR $LOCAL_DIR/memory.db
- **Compact**: python3 $GLOBAL_DIR/consolidate_facts.py
- **Session reflect**: python3 $GLOBAL_DIR/session_reflect.py $LOCAL_DIR

## Cross-Project Knowledge
- Global memories (in $GLOBAL_DIR) are symlinked at $LOCAL_DIR/global/
- They are searchable and shared across ALL projects
- Local memories stay in the repo (git-tracked)

$MARKER_END
EOF

# 8. Append Instructions to CLAUDE.md (idempotent)
MARKER_START="# >>> Agentic Memory System >>>"
MARKER_END="# <<< Agentic Memory System <<<"
echo "Writing agent instructions to CLAUDE.md..."

# Remove old block if marker exists
if grep -q "$MARKER_START" CLAUDE.md 2>/dev/null; then
    sed -i "/$MARKER_START/,/$MARKER_END/d" CLAUDE.md
fi

cat << EOF >> CLAUDE.md

$MARKER_START
# Agentic Memory System

## Setup
Run \`bash $GLOBAL_DIR/setup_memory.sh\` from project root to bootstrap.

## Agent Workflow
At session start — MANDATORY memory search:
  Before answering ANY question, search for relevant memories:
  \`\`\`bash
  python3 $GLOBAL_DIR/search_memory.py "topic keywords"
  \`\`\`
  This takes <100ms and gives you context from past sessions.

During work — MANDATORY memory saves:
  - Solved a bug → memory_save(fix + root cause, "lessons", slug, tags, pinned=true, is_global=true)
  - Made a decision → memory_save(decision + rationale, "decisions", slug, tags, pinned=false, is_global=false)
  - Found a pitfall → memory_save(pitfall, "lessons", slug, tags, pinned=true, is_global=true)
  - Learned a pattern → memory_save(pattern, "lessons", slug, tags, pinned=false, is_global=true)

At session end — REQUIRED reflection:
  \`\`\`bash
  python3 $GLOBAL_DIR/session_reflect.py $LOCAL_DIR
  \`\`\`
  Save any unsaved learnings before yielding.

Maintenance (weekly):
  - memory_compact() - Full tier migration + consolidation + rebuild
  - memory_audit()   - Health check report
  - Review compaction-proposal.md in memory/sessions/

$MARKER_END
EOF

echo "=== Agentic Memory Initialized Successfully ==="