from infra.frontmatter import _coerce  # noqa: E402,F401 — explicit re-export for backward compat
from infra.memory_config import (  # noqa: E402,E501,F401 — explicit re-exports for backward compat
    PROJECT_ROOT_MARKERS,
    _VALID_LOG_LEVELS,
)

# backward compat - real implementation is in infra/memory_common.py
import sys
import infra.memory_common as _real

def __getattr__(name):
    return getattr(_real, name)

def __dir__():
    return sorted(set(object.__dir__(_real)) | set(dir(_real)))

if __name__ in sys.modules:
    from infra._shim import install_shim
    install_shim(__name__, _real)
