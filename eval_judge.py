"""LLM-as-judge evaluation framework (Phase 5.2).

Provides a way to score the quality of memory operations
(save extraction quality, search relevance, etc.) by asking an
LLM to grade the output against a rubric.

Why this exists:
  * Unit tests check correctness ("does the result equal the
    expected value?") but quality is graded on a spectrum
    ("how well does this summary capture the original?")
  * For LLM-based pipelines (fact extraction, summarization,
    entity linking), quality is the *right* signal — but it's
    not a deterministic one. A judge LLM gives us reproducible-
    enough quality scores to detect regressions.
  * We use a separate judge model (default: any LLM accessible
    via ``subprocess.run``) so the saved model and the judge
    model can be different — separating "what we extract" from
    "how we grade it".

Built-in rubrics:
  * ``search_relevance`` — is each result relevant to the query?
  * ``save_extraction`` — does the extracted fact/entity correctly
    represent the source memory?
  * ``summarization_faithfulness`` — does the summary preserve the
    source's meaning without hallucination?

Custom rubrics: subclasses of ``Rubric`` with a ``build_prompt``
that returns a question + scoring template.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeScore:
    """Result of a single judge evaluation.

    score: 0.0 (worst) — 1.0 (best), as judged by the LLM.
    reasoning: short free-text explanation from the judge.
    rubric: name of the rubric that produced this score.
    raw: the raw LLM response, for debugging.
    """

    score: float
    reasoning: str
    rubric: str
    raw: str = ""


@dataclass
class JudgeSample:
    """A single evaluation sample: the question/expected/actual + score.

    The fields are rubric-specific; ``input`` and ``output`` are the
    arbitrary dicts the rubric needs (e.g., for search_relevance,
    ``input`` is the query and ``output`` is the list of results).
    """

    sample_id: str
    input: Any
    output: Any
    expected: Any = None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Judge backends
# ---------------------------------------------------------------------------


@runtime_checkable
class Judge(Protocol):
    """Protocol a judge backend must satisfy."""

    name: str

    def grade(self, prompt: str, system: str = "") -> str:
        """Send a prompt to the LLM, return the raw text response."""
        ...


class StubJudge:
    """Deterministic judge that returns the same response every time.

    Use this for:
      * Unit tests that don't want LLM network calls
      * CI environments without LLM access
      * Reproducing a known judge response for regression tests

    The default stub returns a perfect score (1.0) for every
    prompt. Pass a different ``response`` to simulate a judge
    that's more critical.
    """

    def __init__(self, response: str | None = None) -> None:
        self.name = "stub"
        self.response = response or json.dumps(
            {
                "score": 1.0,
                "reasoning": "stub judge — pass-through",
            }
        )

    def grade(self, prompt: str, system: str = "") -> str:
        return self.response


class SubprocessJudge:
    """Judge that shells out to an LLM via a subprocess.

    Useful when the LLM lives in a different process (e.g., a
    Qwen2.5 inference script). The subprocess must print the LLM
    response to stdout.

    Args:
        command: list of args, e.g., ``["qwen2.5", "--json"]``.
        env: extra env vars to set (merged with os.environ).
        timeout: seconds before killing the subprocess.

    Example:
        judge = SubprocessJudge(
            command=["python", "/path/to/llm.py"],
            env={"LLM_MODEL": "qwen2.5-3b"},
            timeout=30,
        )
    """

    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.name = "subprocess:" + command[0]
        self.command = command
        self.env = env or {}
        self.timeout = timeout

    def grade(self, prompt: str, system: str = "") -> str:
        run_env = os.environ.copy()
        run_env.update(self.env)
        # Pipe prompt on stdin, capture stdout.
        result = subprocess.run(
            self.command,
            input=prompt,
            capture_output=True,
            text=True,
            env=run_env,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"judge subprocess failed (rc={result.returncode}): "
                f"{result.stderr[:200]}"
            )
        return result.stdout


def get_default_judge() -> Judge:
    """Pick the right judge for this environment.

    Resolution order:
      1. ``AGENTIC_MEMORY_JUDGE_COMMAND`` env var (JSON list) — use a
         SubprocessJudge with that command.
      2. ``AGENTIC_MEMORY_JUDGE=stub`` — return the stub.
      3. Default — return the stub.
    """
    cmd = os.environ.get("AGENTIC_MEMORY_JUDGE_COMMAND")
    if cmd:
        try:
            command = json.loads(cmd)
            return SubprocessJudge(command=command)
        except json.JSONDecodeError:
            LOG.warning("AGENTIC_MEMORY_JUDGE_COMMAND is not valid JSON; using stub")
    return StubJudge()


# ---------------------------------------------------------------------------
# Rubrics
# ---------------------------------------------------------------------------


class Rubric:
    """Base class for evaluation rubrics.

    Subclasses must implement:
      * ``name`` — short identifier
      * ``build_prompt(sample)`` — return (system, user) pair
      * ``parse_response(raw)`` — extract (score, reasoning) from the LLM text
    """

    name: str = "base"

    def build_prompt(self, sample: JudgeSample) -> tuple[str, str]:
        raise NotImplementedError

    def parse_response(self, raw: str) -> tuple[float, str]:
        """Default parser: extract JSON from the response."""
        text = raw.strip()
        # Strip ```json ... ``` fences if present.
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(ln for ln in lines if not ln.strip().startswith("```"))
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return 0.5, f"unparseable: {text[:100]}"
        score = float(data.get("score", 0.0))
        reasoning = str(data.get("reasoning", ""))
        return score, reasoning


class SearchRelevanceRubric(Rubric):
    """Judge: are these search results relevant to the query?

    Each result is graded on a 0-1 scale; the sample score is the mean.
    """

    name = "search_relevance"

    def build_prompt(self, sample: JudgeSample) -> tuple[str, str]:
        system = (
            "You are a relevance judge for a memory search system. "
            "For each result, decide if it answers the query. "
            "Respond with JSON only."
        )
        query = sample.input
        results = sample.output
        user = (
            f"Query: {query!r}\n\n"
            f"Results to grade (JSON list of {{id, content, score}}):\n"
            f"{json.dumps(results, indent=2, default=str)}\n\n"
            "For each result, decide relevance 0 (irrelevant) — 1 (perfect). "
            'Output JSON: {"scores": [0.7, 0.2, ...], '
            '"reasoning": "<one-line explanation>"}'
        )
        return system, user

    def parse_response(self, raw: str) -> tuple[float, str]:
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = "\n".join(
                    ln for ln in text.splitlines() if not ln.strip().startswith("```")
                )
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return 0.5, f"unparseable: {raw[:100]}"
        scores = data.get("scores", [])
        if not isinstance(scores, list) or not scores:
            return 0.0, data.get("reasoning", "no scores returned")
        try:
            mean = sum(float(s) for s in scores) / len(scores)
        except (TypeError, ValueError):
            return 0.0, f"non-numeric scores: {scores}"
        return mean, str(data.get("reasoning", ""))


class SaveExtractionRubric(Rubric):
    """Judge: did the extraction correctly capture the source memory?

    The input is the raw memory text; the output is the extracted
    fact/entity/relationship. The expected field (if provided) is the
    ground truth the judge compares against.
    """

    name = "save_extraction"

    def build_prompt(self, sample: JudgeSample) -> tuple[str, str]:
        system = (
            "You are a fact-extraction judge. Compare the source memory "
            "to the extracted fact/entity. Score 0 (wrong) — 1 (perfect). "
            "Respond with JSON only."
        )
        source = sample.input
        extracted = sample.output
        expected = sample.expected
        user = (
            f"Source memory:\n{source!r}\n\n"
            f"Extracted fact/entity:\n{json.dumps(extracted, default=str)}\n\n"
        )
        if expected is not None:
            user += f"Expected (ground truth):\n{json.dumps(expected, default=str)}\n\n"
        user += (
            "Score:\n"
            "  1.0  exact match or close paraphrase\n"
            "  0.5  partially correct (right subject, wrong detail)\n"
            "  0.0  wrong or fabricated\n\n"
            'Output JSON: {"score": 0.5, "reasoning": "..."}'
        )
        return system, user


class SummarizationFaithfulnessRubric(Rubric):
    """Judge: does the summary faithfully represent the source?"""

    name = "summarization_faithfulness"

    def build_prompt(self, sample: JudgeSample) -> tuple[str, str]:
        system = (
            "You are a faithfulness judge for a summarization system. "
            "Score the summary on whether it preserves the source's "
            "meaning without hallucination. Respond with JSON only."
        )
        user = (
            f"Source:\n{sample.input!r}\n\n"
            f"Summary:\n{sample.output!r}\n\n"
            "Score:\n"
            "  1.0  faithful, complete, no hallucination\n"
            "  0.5  mostly faithful, minor omissions or rewording\n"
            "  0.0  hallucinated or omits the main point\n\n"
            'Output JSON: {"score": 0.7, "reasoning": "..."}'
        )
        return system, user


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class JudgeRunner:
    """Runs a rubric over a list of samples and returns the aggregate score.

    Usage:
        judge = get_default_judge()
        rubric = SearchRelevanceRubric()
        runner = JudgeRunner(judge, rubric)
        score, results = runner.run(samples)
        print(f"Mean score: {score:.3f}")
    """

    def __init__(self, judge: Judge, rubric: Rubric) -> None:
        self.judge = judge
        self.rubric = rubric

    def grade(self, sample: JudgeSample) -> JudgeScore:
        system, user = self.rubric.build_prompt(sample)
        try:
            raw = self.judge.grade(user, system=system)
        except Exception as e:
            return JudgeScore(
                score=0.0,
                reasoning=f"judge error: {e}",
                rubric=self.rubric.name,
            )
        score, reasoning = self.rubric.parse_response(raw)
        return JudgeScore(
            score=score, reasoning=reasoning, rubric=self.rubric.name, raw=raw
        )

    def run(self, samples: list[JudgeSample]) -> tuple[float, list[JudgeScore]]:
        results = [self.grade(s) for s in samples]
        if not results:
            return 0.0, []
        mean = sum(r.score for r in results) / len(results)
        return mean, results


__all__ = [
    "Judge",
    "JudgeSample",
    "JudgeScore",
    "JudgeRunner",
    "Rubric",
    "SearchRelevanceRubric",
    "SaveExtractionRubric",
    "StubJudge",
    "SubprocessJudge",
    "SummarizationFaithfulnessRubric",
    "get_default_judge",
]
