#!/usr/bin/env python3
# backward compat — real implementation is in infra/pinned_decay.py
import sys
import infra.pinned_decay as _real

def __getattr__(name):
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

if __name__ in sys.modules:
    from infra._shim import install_shim
    install_shim(__name__, _real)

if __name__ == "__main__":
    _real.main()
