
# backward compat — real implementation is in backfill/orchestrator.py
import sys
from pathlib import Path

import backfill.orchestrator as _real

# Module-level __getattr__ / __dir__ work for both import and exec_module paths
def __getattr__(name):
    """Delegate attribute access to the real orchestrator module."""
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

# __setattr__ / __delattr__ require __class__ override (import path only)

if __name__ in sys.modules:
    from infra._shim import install_shim
    install_shim(__name__, _real)

if __name__ == "__main__":
    import runpy
    _MAIN = str(Path(__file__).resolve().parent / "backfill" / "orchestrator.py")
    sys.argv[0] = _MAIN
    runpy.run_path(_MAIN, run_name="__main__")
