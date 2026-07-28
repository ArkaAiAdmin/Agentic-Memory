#!/usr/bin/env python3
"""
Agent Initialization Script for Agentic Memory System

Run this at the start of every agent session to:
1. Load memory system configuration
2. Search for relevant context based on current task
3. Display memory index and recent sessions
4. Prime the agent with cross-project knowledge

Usage:
    python3 ~/.config/agentic-memory/agent_init.py [task_keywords...]
"""
import sys
from pathlib import Path

# Add agentic-memory to path for imports
sys.path.insert(0, str(Path.home() / '.config' / 'agentic-memory'))

from search_memory import search_memories

def find_project_root(start_path: Path) -> Path:
    for path in [start_path] + list(start_path.parents):
        if (path / 'memory').is_dir() or (path / '.git').exists() or (path / 'CLAUDE.md').exists():
            return path
    return start_path

def print_banner():
    print("""
╔═════════════════════════════════════════════════════════════════════════════╗
║                        AGENTIC MEMORY INITIALIZATION                        ║
║                    The memory system agents can trust                       ║
╚═════════════════════════════════════════════════════════════════════════════╝
""")

def load_memory_index(project_root: Path):
    """Load and display MEMORY.md index."""
    memory_md = project_root / 'memory' / 'MEMORY.md'
    if memory_md.exists():
        print("📚 LOCAL MEMORY INDEX")
        print("=" * 78)
        content = memory_md.read_text(encoding='utf-8', errors='ignore')
        # Print first 50 lines
        lines = content.splitlines()[:50]
        for line in lines:
            print(f"  {line}")
        if len(content.splitlines()) > 50:
            print("  ...")
        print()

def load_global_memory_index():
    """Load and display global MEMORY.md index."""
    global_mem = Path.home() / '.config' / 'agentic-memory'
    memory_md = global_mem / 'MEMORY.md'
    if memory_md.exists():
        print("🌍 GLOBAL MEMORY INDEX")
        print("=" * 78)
        content = memory_md.read_text(encoding='utf-8', errors='ignore')
        lines = content.splitlines()[:30]
        for line in lines:
            print(f"  {line}")
        if len(content.splitlines()) > 30:
            print("  ...")
        print()

def search_relevant_memories(keywords, limit=5):
    """Search for memories relevant to the given keywords."""
    if not keywords:
        return
    
    query = " ".join(keywords)
    print(f"🔍 SEARCHING FOR: '{query}'")
    print("=" * 78)
    
    # Search local + global with multi-root fallback
    search_memories(query, limit=limit, include_global=True, min_local_results=2)
    print()

def show_recent_sessions(project_root: Path, limit=3):
    """Show recent session logs."""
    sessions_dir = project_root / 'memory' / 'sessions'
    if sessions_dir.exists():
        sessions = sorted(sessions_dir.glob('*.md'), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
        if sessions:
            print("📝 RECENT SESSIONS")
            print("=" * 78)
            for session in sessions:
                try:
                    content = session.read_text(encoding='utf-8', errors='ignore')
                    # Extract first few lines as preview
                    lines = [l for l in content.splitlines() if l.strip()][:3]
                    print(f"  📄 {session.name}")
                    for line in lines:
                        print(f"     {line[:80]}")
                except Exception:
                    pass
            print()

def show_memory_stats(project_root: Path):
    """Show memory database statistics."""
    db_path = project_root / 'memory' / 'memory.db'
    global_db = Path.home() / '.config' / 'agentic-memory' / 'memory.db'
    
    print("📊 MEMORY STATISTICS")
    print("=" * 78)
    
    for label, path in [("Local", db_path), ("Global", global_db)]:
        if path.exists():
            try:
                from infra.db import open_db
                with open_db(path, timeout=5.0, pooled=True, write=False) as db:
                    cursor = db.cursor()
                    cursor.execute("SELECT COUNT(*) FROM memories")
                    row = cursor.fetchone()
                    count = row[0] if row else 0
                    cursor.execute("SELECT COUNT(*) FROM memories WHERE repo_id IS NULL")
                    row_g = cursor.fetchone()
                    global_count = row_g[0] if row_g else 0
                    cursor.execute("SELECT COUNT(*) FROM memories WHERE pinned = 1")
                    row_p = cursor.fetchone()
                    pinned_count = row_p[0] if row_p else 0
                
                if label == "Local":
                    print(f"  {label}: {count} memories ({global_count} global, {pinned_count} pinned)")
                else:
                    print(f"  {label}: {count} memories ({pinned_count} pinned)")
            except Exception as e:
                print(f"  {label}: Error reading stats - {e}")
        else:
            print(f"  {label}: Not initialized")
    print()

def print_workflow_guide():
    """Print the agent memory workflow guide."""
    print("""
🎯 AGENT MEMORY WORKFLOW
========================
At session start:
  1. Run this script: python3 ~/.config/agentic-memory/agent_init.py [keywords]
  2. Review local + global memory indexes
  3. Check recent sessions for context
  4. Search for relevant memories using keywords

During work:
  • Search:      memory_search("keyword") via MCP
  • Save:        memory_save(content, category, title_slug, tags, pinned, is_global)
  • Reinforce:   memory_reinforce([ids], success=true/false)
  • Compact:     memory_compact()
  • Audit:       memory_audit()
  • Review due:  memory_review_schedule()

Cross-project:
  • Global memories (is_global=True) visible to ALL projects
  • Search automatically falls back to global DB if local results < 3
  • Use --no-global flag to search local only

Maintenance (weekly):
  • memory_compact() - Full tier migration + consolidation + rebuild
  • memory_audit()   - Health check report
  • Review compaction-proposal.md in memory/sessions/
""")

def main():
    print_banner()
    
    # Get current project root
    cwd = Path.cwd()
    project_root = find_project_root(cwd)
    print(f"📁 Project: {project_root.name} ({project_root})")
    print()
    
    # Show memory stats
    show_memory_stats(project_root)
    
    # Show local memory index
    load_memory_index(project_root)
    
    # Show global memory index
    load_global_memory_index()
    
    # Search for relevant memories if keywords provided
    if len(sys.argv) > 1:
        search_relevant_memories(sys.argv[1:])
    
    # Show recent sessions
    show_recent_sessions(project_root)
    
    # Print workflow guide
    print_workflow_guide()
    
    print("✅ Agent initialization complete. Ready to work!")

if __name__ == '__main__':
    main()