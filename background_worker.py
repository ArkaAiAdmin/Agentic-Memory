
# backward compat — real implementation is in background/background_worker.py
import sys
from pathlib import Path as _Path

if __name__ == "background_worker":
    _CRON_DIR = str(_Path(__file__).resolve().parent / "cron")
    if _CRON_DIR not in sys.path:
        sys.path.insert(0, _CRON_DIR)

import background.background_worker as _real

def __getattr__(name):
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

if __name__ in sys.modules:
    from infra._shim import install_shim
    install_shim(__name__, _real)

if __name__ == '__main__':
    raise SystemExit(_real.main())
