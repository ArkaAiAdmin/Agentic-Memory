"""Protocol and data structures for the unified benchmark engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BenchmarkSession:
    """A memory session or chunk to be ingested into the test database."""

    session_id: str
    content: str
    timestamp: str  # ISO-8601 string
    category: str = "sessions"
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkQuestion:
    """A single evaluation query and ground-truth expectation."""

    question_id: str
    query: str
    expected_answer: str | None = None
    gold_session_ids: set[str] = field(default_factory=set)
    category: str = "general"
    as_of: float | None = None
    rubric: list[str] | None = None
    compliance_indicators: list[str] | None = None
    non_compliance_signs: list[str] | None = None
    difficulty: str = "normal"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkResult:
    """Evaluation result for a single question."""

    question_id: str
    category: str
    query: str
    expected: str | None
    retrieved_ids: list[str]
    retrieved_content: list[str]
    scores: dict[str, float]
    latency_ms: float
    phases: list[str] = field(default_factory=list)
    phase_latencies: dict[str, float] = field(default_factory=dict)
    phase_errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuiteSummary:
    """Aggregated results across an entire benchmark suite."""

    suite_name: str
    dataset_version: str
    total_questions: int
    total_sessions_ingested: int
    ingest_time_seconds: float
    wall_time_seconds: float
    latency_ms: dict[str, float]  # mean, p50, p95, p99, max
    macro_metrics: dict[str, float]
    category_metrics: dict[str, dict[str, float]]
    results: list[BenchmarkResult] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
