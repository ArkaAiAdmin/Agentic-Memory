import sys
from pathlib import Path

WORKTREE_ROOT = str(Path(__file__).resolve().parent)

def _should_redirect(p) -> bool:
    p_str = str(p)
    if ".venv" in p_str or "site-packages" in p_str:
        return False
    return "/.config/agentic-memory" in p_str and "agentic-memory-wt-packaging" not in p_str

class PathRedirector(list):
    def insert(self, index, value):
        if _should_redirect(value):
            value = WORKTREE_ROOT
        super().insert(index, value)

    def append(self, value):
        if _should_redirect(value):
            value = WORKTREE_ROOT
        super().append(value)

    def extend(self, values):
        super().extend(
            WORKTREE_ROOT if _should_redirect(v) else v
            for v in values
        )

# Initialize PathRedirector with current sys.path redirected
initial_paths = []
for p in sys.path:
    if _should_redirect(p):
        initial_paths.append(WORKTREE_ROOT)
    else:
        initial_paths.append(p)

sys.path = PathRedirector(initial_paths)
