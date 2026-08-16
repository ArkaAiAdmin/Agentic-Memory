"""Base benchmark adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence
from pathlib import Path

from ..protocol import BenchmarkQuestion, BenchmarkSession


class BaseBenchmarkAdapter(ABC):
    """Base class for benchmark dataset loaders and adapters."""

    name: str = "base"
    version: str = "1.0"
    tenant_id: str = "benchmark"

    @abstractmethod
    def load(self, limit: int | None = None) -> tuple[list[BenchmarkSession], list[BenchmarkQuestion]]:
        """Load benchmark sessions and evaluation questions.

        Returns: (sessions_list, questions_list)
        """
        ...
