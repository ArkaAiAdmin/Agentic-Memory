"""Recursive dict diff returning dot-path keys that differ."""
from __future__ import annotations

def dict_diff(a: dict, b: dict, prefix: str = "") -> list[str]:
    keys = set(a) | set(b)
    diffs = []
    for k in sorted(keys):
        path = f"{prefix}.{k}" if prefix else k
        if k not in a or k not in b:
            diffs.append(path)
            continue
        av, bv = a[k], b[k]
        if isinstance(av, dict) and isinstance(bv, dict):
            diffs.extend(dict_diff(av, bv, prefix=path))
        elif av != bv:
            diffs.append(path)
    return diffs
