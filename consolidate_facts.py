
# backward compat — real implementation is in fact/consolidate_facts.py
import sys
import types
from pathlib import Path

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

import fact.consolidate_facts as _real

# Module-level __getattr__ / __dir__ for both import and exec_module paths
def __getattr__(name):
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

if __name__ in sys.modules:
    _shim = sys.modules[__name__]
    _shim.__class__ = _ShimModule
    object.__setattr__(_shim, '_real', _real)

if __name__ == "__main__":
    import runpy
    _MAIN = str(Path(__file__).resolve().parent / "fact" / "consolidate_facts.py")
    sys.argv[0] = _MAIN
    runpy.run_path(_MAIN, run_name="__main__")
