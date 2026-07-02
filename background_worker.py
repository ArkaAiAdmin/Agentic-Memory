
# backward compat — real implementation is in background/background_worker.py
import sys
import types
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

class _ShimModule(types.ModuleType):
    _real = None
    def __getattr__(self, name):
        return getattr(self._real, name)
    def __setattr__(self, name, value):
        if name in ('_real', '__class__'):
            super().__setattr__(name, value)
        else:
            setattr(self._real, name, value)
    def __delattr__(self, name):
        if name == '_real':
            raise AttributeError("_real is protected")
        delattr(self._real, name)
    def __dir__(self):
        return sorted(set(super().__dir__()) | set(dir(self._real)))

if __name__ in sys.modules:
    _shim = sys.modules[__name__]
    _shim.__class__ = _ShimModule
    object.__setattr__(_shim, '_real', _real)

if __name__ == '__main__':
    raise SystemExit(_real.main())
