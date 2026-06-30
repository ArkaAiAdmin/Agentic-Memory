# backward compat — real implementation is in kg/kg_traversal.py
import sys
import kg.kg_traversal as _real

def __getattr__(name):
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

if __name__ in sys.modules:
    from infra._shim import install_shim
    install_shim(__name__, _real)
