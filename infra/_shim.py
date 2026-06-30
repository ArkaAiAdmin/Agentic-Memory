"""Shared backward-compat shim for modules relocated to subpackages.

Usage in a shim file (e.g. ``saga.py``):

    # backward compat — real implementation is in infra/saga
    import sys
    import infra.saga as _real

    def __getattr__(name):
        return getattr(_real, name)

    def __dir__():
        return sorted(set(object.__dir__(_real)) | set(dir(_real)))

    if __name__ in sys.modules:
        from infra._shim import install_shim
        install_shim(__name__, _real)
"""

import sys
import types


class _ShimModule(types.ModuleType):
    """Module subclass that delegates all attribute access to ``_real``."""
    _real = None

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name in ("_real", "__class__"):
            super().__setattr__(name, value)
        else:
            setattr(self._real, name, value)

    def __delattr__(self, name):
        if name == "_real":
            raise AttributeError("_real is protected")
        delattr(self._real, name)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(dir(self._real)))


def install_shim(shim_module_name: str, real_module) -> None:
    """Swap the caller's module class to ``_ShimModule`` proxying *real_module*.

    Must be called at module level in the shim file's ``if __name__ in sys.modules:``
    block, after the module-level ``__getattr__``/``__dir__`` are defined.
    """
    _mod = sys.modules[shim_module_name]
    _mod.__class__ = _ShimModule
    object.__setattr__(_mod, "_real", real_module)
