"""Shared hashing utilities."""
import hashlib


def md5_to_uint64(memory_id: str) -> int:
    """Convert a string memory_id to a uint64 suitable for use as a vector key."""
    return int(hashlib.md5(memory_id.encode("utf-8")).hexdigest()[:16], 16)
