# backward compat — real implementation is in recall/search_memory
import os as _os
import sys
import io as _io
from pathlib import Path

import recall.search_memory as _real

# Pre-load common attributes so unittest.mock.patch works on this module
os = _os
io = _io
Path = Path
find_project_root = getattr(_real, "find_project_root", None)
get_memory_paths = getattr(_real, "get_memory_paths", None)
GLOBAL_MEM_DIR = getattr(_real, "GLOBAL_MEM_DIR", None)

def __getattr__(name):
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

if __name__ in sys.modules:
    from infra._shim import install_shim
    install_shim(__name__, _real)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: search_memory.py <query> [limit] [--no-global] [db_path]")
        sys.exit(1)
    query = sys.argv[1]
    limit = 5
    include_global = True
    db_path = None
    for arg in sys.argv[2:]:
        if arg == "--no-global":
            include_global = False
        elif not arg.startswith("--"):
            if arg.isdigit():
                limit = int(arg)
            else:
                db_path = arg
    from recall.search_memory import search_memories as _search

    _search(
        query,
        limit=limit,
        custom_db_path=db_path,
        include_global=include_global,
        silent=False,
    )
