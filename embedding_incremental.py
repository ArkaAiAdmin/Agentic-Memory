"""embeddings.py — embedding utilities (removed SSM v1).

The old `embedding_incremental.py` (SsmEncoder, merge_embeddings,
_write_ssm_state) was removed in 2026-06-29.  The new Temporal SSM
lives in search/scoring.py and is wired into the read path.

This stub exists solely so that any third-party code that still does
``import embedding_incremental`` gets a clear DeprecationWarning at
import time, rather than an opaque ImportError.
"""

import warnings

warnings.warn(
    "embedding_incremental was removed (SSM v1 dead end). "
    "The new Temporal SSM is in search/scoring.py. "
    "Remove all imports of embedding_incremental.",
    DeprecationWarning,
    stacklevel=2,
)
