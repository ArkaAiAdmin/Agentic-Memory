# backward compat — DEPRECATED: real implementation is in kg/temporal_resolver.py
# which itself is superseded by fact/fact_temporal.py for the write path.
# This shim will be removed in a future release.
import sys
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

if __name__ in sys.modules:
    from infra._shim import install_shim
    install_shim(__name__, _real)
