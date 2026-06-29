"""
Shared infrastructure for MCP tool domain modules.

Re-exports from memory_common, infrastructure, cache, and provides
helpers (_resolve_memory_dir, _run_subprocess_output, etc.) used by
all domain modules.  No tool registration here.
"""

import os
from pathlib import Path

import logging
import re
import subprocess

import re as _re

_SLUG_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


def _validate_slug(value: str, label: str) -> str | None:
    if not value or not _SLUG_RE.match(value):
        return f"Invalid {label}: must be 1-128 alphanumeric/dash/underscore chars, no path separators"
    return None


from memory_common import (
    atomic_write,
    connection_pool,
    parse_frontmatter,
    safe_close_db,
    open_db,
)
from db_migrations import run_db_migrations
from infra.infrastructure import resolve_db_for_memory_id
from infra.cache import _search_cache
from infra.infrastructure import (
    ErrorCode,
    _err,
    resolve_active_memory_dir,
    with_audit,
    with_memory_connection,
)

from config import GLOBAL_SCRIPTS_DIR, AGENTS_SKILLS_DIR
from infra.memory_config import GLOBAL_MEM_DIR, get_memory_paths

logger = logging.getLogger(__name__)

# Bootstrap path: canonical root for all user-level agentic-memory data.
_bootstrap_path = GLOBAL_SCRIPTS_DIR


def _resolve_memory_dir() -> Path:
    db_path = os.environ.get("MEMORY_DB_PATH")
    if db_path is not None:
        return Path(db_path).parent
    return resolve_active_memory_dir()


def _run_subprocess_output(
    cmd: list[str], timeout: int = 60, cwd: str | None = None
) -> tuple[str, int]:
    """Run *cmd* and return (stdout, returncode). stderr is merged into stdout."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    out = r.stdout or ""
    if r.stderr and r.stderr.strip():
        sep = "\n" if out and not out.endswith("\n") else ""
        out = out + sep + "[stderr]\n" + r.stderr
    return out, int(r.returncode)


def recompile_skills_catalog():
    try:
        skills_dir = AGENTS_SKILLS_DIR
        dest_file = GLOBAL_SCRIPTS_DIR / "preferences" / "installed-skills.md"
        if not skills_dir.exists() or not dest_file.parent.exists():
            return

        skills_data = []
        for item in sorted(skills_dir.iterdir()):
            if item.is_dir():
                skill_md = item / "SKILL.md"
                if skill_md.exists():
                    content = skill_md.read_text(encoding="utf-8", errors="ignore")
                    match = re.match(
                        r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", content, re.DOTALL
                    )
                    meta = {}
                    if match:
                        for line in match.group(1).splitlines():
                            if ":" in line:
                                k, v = line.split(":", 1)
                                meta[k.strip()] = v.strip().strip('"').strip("'")
                    name = meta.get("name", item.name)
                    desc = meta.get("description", "No description.")
                    when = meta.get("when_to_use", "Use when requested.")
                    skills_data.append(
                        {"name": name, "description": desc, "when": when}
                    )

        md_lines = [
            "---",
            "created: 2026-06-04",
            "updated: 2026-06-04",
            "tags: [reference, skills, tools]",
            "pinned: true",
            "related: []",
            "---",
            "",
            "# Installed Agent Skills Index",
            "",
            "This document lists all the custom agent skills installed on this machine at `~/.agents/skills/`. You can load the full instructions for any skill by reading `skill://<skill-name>` (e.g. `read skill://tdd` or `read skill://improve-codebase-architecture`).",
            "",
            "## Skills Directory",
            "",
            "| Skill | Trigger / When to Use | Description |",
            "| :--- | :--- | :--- |",
        ]
        for skill in skills_data:
            when_esc = skill["when"].replace("|", "\\|").replace("\n", " ")
            desc_esc = skill["description"].replace("|", "\\|").replace("\n", " ")
            md_lines.append(f"| `skill://{skill['name']}` | {when_esc} | {desc_esc} |")

        atomic_write(dest_file, "\n".join(md_lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to recompile skills catalog: %s", e)
