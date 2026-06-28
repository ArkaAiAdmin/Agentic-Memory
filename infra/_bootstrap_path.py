"""Bootstrap sys.path so project modules are importable.

Run this BEFORE any ``from memory_*``, ``from mcp_*``,
``from search_pipeline``, ``from save_pipeline``, ``from config``,
etc. import.  Idempotent: if memory_common is already importable,
the ``sys.path.insert`` is a no-op.

Usage in every entry-point module::

    import _bootstrap_path  # noqa: E402
"""

import os
import sys
from pathlib import Path

INSTALL_ROOT = Path(
    os.environ.get("MEMORY_INSTALL_ROOT") or str(Path(__file__).resolve().parent.parent)
).resolve()
if not (INSTALL_ROOT / "memory_config.py").exists():
    INSTALL_ROOT = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_ROOT))
