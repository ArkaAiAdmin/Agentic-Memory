# backward compat — real implementation is in background/auto_save.py
import sys
from pathlib import Path as _Path

import background.auto_save as _real

def __getattr__(name):
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

if __name__ in sys.modules:
    from infra._shim import install_shim
    install_shim(__name__, _real)

if __name__ == "__main__":
    import runpy
    _MAIN = str(_Path(__file__).resolve().parent / "background" / "auto_save.py")
    sys.argv[0] = _MAIN
    runpy.run_path(_MAIN, run_name="__main__")
