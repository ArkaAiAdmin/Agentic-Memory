# backward compat — DEPRECATED: real implementation is in kg/temporal_resolver.py
# which itself is superseded by fact/fact_temporal.py for the write path.
# This shim will be removed in a future release.
import sys
import types
import warnings

warnings.warn(
    "temporal_resolver is deprecated. Use fact.fact_temporal for write-path "
    "temporal resolution, or kg.fact_temporal for fact-level supersession.",
    DeprecationWarning,
    stacklevel=1,
)

import kg.temporal_resolver as _real

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
