"""Memory recall and search package.

Re-exports all symbols from ``recall.recall`` so that
``from recall import <symbol>`` works transparently
(whether ``<symbol>`` is public or private).
"""

import recall.recall as _recall


def __getattr__(name):
    return getattr(_recall, name)


def __dir__():
    return sorted(set(object.__dir__(_recall)) | set(dir(_recall)))
