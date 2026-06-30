# backward compat — real implementation is in kg/contradiction_detector.py
import sys
from pathlib import Path

import kg.contradiction_detector as _real

def __getattr__(name):
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

if __name__ in sys.modules:
    from infra._shim import install_shim
    install_shim(__name__, _real)

if __name__ == "__main__":
    import runpy
    _MAIN = str(Path(__file__).resolve().parent / "kg" / "contradiction_detector.py")
    sys.argv[0] = _MAIN
    runpy.run_path(_MAIN, run_name="__main__")
