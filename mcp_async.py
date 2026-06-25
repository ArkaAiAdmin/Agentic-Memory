"""Async wrappers for save/search pipeline.

Extracted from memory_mcp.py on 2026-06-21 to reduce the
monolithic re-export hub.  Four async entry points:

    async_memory_save
    async_memory_search
    async_memory_save_batch
    async_memory_search_batch

Each delegates to the synchronous equivalent via ``asyncio.to_thread``.
"""

import _bootstrap_path  # noqa: E402
import os
import sys
from pathlib import Path


import asyncio

from mcp_tools import memory_save as _memory_save, memory_search as _memory_search


async def async_memory_save(
    content: str,
    category: str,
    title_slug: str,
    tags: list | None = None,
    pinned: bool = False,
    is_global: bool = False,
) -> str:
    """Async wrapper around memory_save."""
    return await asyncio.to_thread(
        _memory_save,
        content=content,
        category=category,
        title_slug=title_slug,
        tags=tags,
        pinned=pinned,
        is_global=is_global,
    )


async def async_memory_search(
    query: str,
    limit: int = 5,
    rerank: bool = True,
    boost_pinned: bool = True,
    recency_weight: float = 0.1,
    include_global: bool = True,
    include_invalid: bool = True,
    deep_rerank: bool = False,
) -> str:
    """Async wrapper around memory_search."""
    return await asyncio.to_thread(
        _memory_search,
        query=query,
        limit=limit,
        rerank=rerank,
        boost_pinned=boost_pinned,
        recency_weight=recency_weight,
        include_global=include_global,
        include_invalid=include_invalid,
        deep_rerank=deep_rerank,
    )


async def async_memory_save_batch(items: list) -> list[tuple[str, float]]:
    """Save multiple memories concurrently."""
    import time as _time

    tasks = []
    for item in items:
        tasks.append(
            async_memory_save(
                content=item["content"],
                category=item["category"],
                title_slug=item["title_slug"],
                tags=item.get("tags"),
                pinned=item.get("pinned", False),
                is_global=item.get("is_global", False),
            )
        )
    start = _time.monotonic()
    done = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = (_time.monotonic() - start) * 1000
    results: list[tuple[str, float]] = []
    for r in done:
        if isinstance(r, BaseException):
            results.append((f"Error: {r}", elapsed / len(items)))
        else:
            assert isinstance(r, str)
            results.append((r, elapsed / len(items)))
    return results


async def async_memory_search_batch(queries: list) -> list[tuple[str, float]]:
    """Search multiple queries concurrently."""
    import time as _time

    tasks = []
    for q in queries:
        tasks.append(
            async_memory_search(
                query=q["query"],
                limit=q.get("limit", 5),
                rerank=q.get("rerank", True),
                include_global=q.get("include_global", True),
                include_invalid=q.get("include_invalid", True),
            )
        )
    start = _time.monotonic()
    done = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = (_time.monotonic() - start) * 1000
    results: list[tuple[str, float]] = []
    for r in done:
        if isinstance(r, BaseException):
            results.append((f"Error: {r}", elapsed / len(queries)))
        else:
            assert isinstance(r, str)
            results.append((r, elapsed / len(queries)))
    return results
