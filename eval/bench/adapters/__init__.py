"""Benchmark suite adapters registry."""

from __future__ import annotations

from typing import Type

from .base import BaseBenchmarkAdapter
from .locomo import LoCoMoAdapter
from .longmemeval_s import LongMemEvalSAdapter
from .longmemeval_v2 import LongMemEvalV2Adapter
from .beam import BEAMAdapter
from .adversarial import AdversarialAdapter
from .golden import GoldenAdapter

ADAPTERS: dict[str, Type[BaseBenchmarkAdapter]] = {
    "locomo": LoCoMoAdapter,
    "longmemeval_s": LongMemEvalSAdapter,
    "longmemeval_v2": LongMemEvalV2Adapter,
    "beam": BEAMAdapter,
    "adversarial": AdversarialAdapter,
    "golden": GoldenAdapter,
}

__all__ = [
    "BaseBenchmarkAdapter",
    "LoCoMoAdapter",
    "LongMemEvalSAdapter",
    "LongMemEvalV2Adapter",
    "BEAMAdapter",
    "AdversarialAdapter",
    "GoldenAdapter",
    "ADAPTERS",
]
