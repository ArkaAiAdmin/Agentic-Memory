"""Shared hashing utilities."""
import hashlib


def md5_to_uint64(memory_id: str) -> int:
    """Convert a string memory_id to a uint64 suitable for use as a vector key.

    Masks to 63 bits so the result always fits in a signed SQLite INTEGER,
    whose range is -(2**63) .. 2**63-1.  Unsigned overflow past 2**63-1
    would otherwise raise ``OverflowError`` on insert.
    """
    return int(hashlib.md5(memory_id.encode("utf-8")).hexdigest()[:16], 16) & 0x7FFFFFFFFFFFFFFF
