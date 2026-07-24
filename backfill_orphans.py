
# backward compat — real implementation is in backfill/backfill_orphans.py
import sys
from pathlib import Path

import backfill.backfill_orphans as _real

# Module-level __getattr__ / __dir__ for both import and exec_module paths
def __getattr__(name):
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

if __name__ in sys.modules:
    from infra._shim import install_shim
    install_shim(__name__, _real)

if __name__ == "__main__":
    import runpy
    _MAIN = str(Path(__file__).resolve().parent / "backfill" / "backfill_orphans.py")
    sys.argv[0] = _MAIN
    runpy.run_path(_MAIN, run_name="__main__")
