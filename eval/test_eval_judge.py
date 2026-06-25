"""Tests for eval_judge.py — Phase 5.2 LLM-as-judge framework.

Covers:
  * StubJudge deterministic behavior
  * SubprocessJudge pipes prompt and returns stdout
  * Rubric base parser (JSON extraction)
  * SearchRelevanceRubric prompt + response parsing
  * SaveExtractionRubric
  * SummarizationFaithfulnessRubric
  * JudgeRunner.grade / run
  * get_default_judge respects env var
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from eval_judge import (  # noqa: E402
    JudgeRunner,
    JudgeSample,
    JudgeScore,
    Rubric,
    SaveExtractionRubric,
    SearchRelevanceRubric,
    StubJudge,
    SubprocessJudge,
    SummarizationFaithfulnessRubric,
    get_default_judge,
)


class TestStubJudge(unittest.TestCase):
    def test_default_response(self):
        judge = StubJudge()
        response = judge.grade("anything")
        data = json.loads(response)
        self.assertEqual(data["score"], 1.0)

    def test_custom_response(self):
        judge = StubJudge(response='{"score": 0.3, "reasoning": "meh"}')
        response = judge.grade("anything")
        data = json.loads(response)
        self.assertEqual(data["score"], 0.3)

    def test_ignores_prompt(self):
        judge = StubJudge()
        self.assertEqual(judge.grade("foo"), judge.grade("bar"))


class TestSubprocessJudge(unittest.TestCase):
    def test_pipes_prompt_to_stdin(self):
        judge = SubprocessJudge(command=["cat"], timeout=5)
        self.assertEqual(judge.grade("hello stdin"), "hello stdin")

    def test_returns_stdout(self):
        judge = SubprocessJudge(
            command=[sys.executable, "-c", "print('hi from judge')"],
            timeout=5,
        )
        self.assertEqual(judge.grade("x").strip(), "hi from judge")

    def test_nonzero_exit_raises(self):
        judge = SubprocessJudge(
            command=[sys.executable, "-c", "import sys; sys.exit(1)"],
            timeout=5,
        )
        with self.assertRaises(RuntimeError):
            judge.grade("x")

    def test_timeout_raises(self):
        judge = SubprocessJudge(
            command=[sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=0.5,
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            judge.grade("x")

    def test_env_merged(self):
        judge = SubprocessJudge(
            command=[sys.executable, "-c", "import os; print(os.environ['MY_VAR'])"],
            env={"MY_VAR": "test-value"},
            timeout=5,
        )
        self.assertEqual(judge.grade("x").strip(), "test-value")


class TestRubricBaseParser(unittest.TestCase):
    def test_strips_json_fence(self):
        r = Rubric()
        score, reasoning = r.parse_response(
            '```json\n{"score": 0.7, "reasoning": "ok"}\n```'
        )
        self.assertEqual(score, 0.7)
        self.assertEqual(reasoning, "ok")

    def test_unparseable_returns_half(self):
        r = Rubric()
        score, reasoning = r.parse_response("not json at all")
        self.assertEqual(score, 0.5)
        self.assertIn("unparseable", reasoning)


class TestSearchRelevanceRubric(unittest.TestCase):
    def setUp(self):
        self.rubric = SearchRelevanceRubric()
        self.sample = JudgeSample(
            sample_id="q1",
            input="how to install",
            output=[
                {"id": "a", "content": "pip install agentic-memory", "score": 0.9},
                {"id": "b", "content": "vector embeddings primer", "score": 0.4},
            ],
        )

    def test_prompt_contains_query(self):
        system, user = self.rubric.build_prompt(self.sample)
        self.assertIn("how to install", user)
        self.assertIn("pip install", user)

    def test_parse_per_result_scores(self):
        raw = json.dumps(
            {
                "scores": [1.0, 0.0],
                "reasoning": "first is on-topic, second is off",
            }
        )
        score, reasoning = self.rubric.parse_response(raw)
        self.assertEqual(score, 0.5)
        self.assertIn("on-topic", reasoning)

    def test_empty_scores(self):
        score, reasoning = self.rubric.parse_response('{"scores": []}')
        self.assertEqual(score, 0.0)


class TestSaveExtractionRubric(unittest.TestCase):
    def setUp(self):
        self.rubric = SaveExtractionRubric()
        self.sample = JudgeSample(
            sample_id="s1",
            input="John works at Acme Corp as of 2024",
            output={"subject": "John", "predicate": "works_at", "object": "Acme"},
            expected={"subject": "John", "predicate": "works_at", "object": "Acme"},
        )

    def test_prompt_has_source_and_extraction(self):
        system, user = self.rubric.build_prompt(self.sample)
        self.assertIn("John works at Acme", user)
        self.assertIn("Acme", user)

    def test_score_extraction(self):
        score, _ = self.rubric.parse_response(
            '{"score": 1.0, "reasoning": "exact match"}'
        )
        self.assertEqual(score, 1.0)


class TestSummarizationFaithfulnessRubric(unittest.TestCase):
    def setUp(self):
        self.rubric = SummarizationFaithfulnessRubric()
        self.sample = JudgeSample(
            sample_id="s1",
            input="Agentic memory is a local-first persistent memory layer for AI agents. It uses SQLite, FTS5, and a knowledge graph.",
            output="Agentic memory is local-first memory for AI agents. It uses SQLite.",
        )

    def test_prompt_has_source_and_summary(self):
        _, user = self.rubric.build_prompt(self.sample)
        self.assertIn("Agentic memory is a local-first", user)
        self.assertIn("uses SQLite", user)

    def test_score(self):
        score, _ = self.rubric.parse_response(
            '{"score": 0.8, "reasoning": "mostly faithful"}'
        )
        self.assertEqual(score, 0.8)


class TestJudgeRunner(unittest.TestCase):
    def setUp(self):
        self.judge = StubJudge(response='{"scores": [0.5, 1.0], "reasoning": "ok"}')
        self.runner = JudgeRunner(self.judge, SearchRelevanceRubric())

    def test_grade_returns_score(self):
        sample = JudgeSample(
            sample_id="q1",
            input="how to install",
            output=[{"id": "a"}, {"id": "b"}],
        )
        result = self.runner.grade(sample)
        self.assertIsInstance(result, JudgeScore)
        self.assertEqual(result.score, 0.75)
        self.assertEqual(result.rubric, "search_relevance")

    def test_grade_handles_judge_error(self):
        class BrokenJudge:
            name = "broken"

            def grade(self, prompt, system=""):
                raise RuntimeError("judge failed")

        runner = JudgeRunner(BrokenJudge(), SearchRelevanceRubric())
        sample = JudgeSample("q1", "x", None, [])
        result = runner.grade(sample)
        self.assertEqual(result.score, 0.0)
        self.assertIn("judge error", result.reasoning)

    def test_run_aggregates(self):
        samples = [
            JudgeSample("q1", "q", None, [{"id": "a"}]),
            JudgeSample("q2", "q", None, [{"id": "b"}]),
        ]
        mean, results = self.runner.run(samples)
        self.assertEqual(len(results), 2)
        # Both scored 0.75, mean is 0.75.
        self.assertAlmostEqual(mean, 0.75)

    def test_run_empty(self):
        mean, results = self.runner.run([])
        self.assertEqual(mean, 0.0)
        self.assertEqual(results, [])


class TestGetDefaultJudge(unittest.TestCase):
    def test_returns_stub_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            judge = get_default_judge()
        self.assertIsInstance(judge, StubJudge)

    def test_uses_env_var(self):
        with patch.dict(
            os.environ,
            {"AGENTIC_MEMORY_JUDGE_COMMAND": '["cat"]'},
        ):
            judge = get_default_judge()
        self.assertIsInstance(judge, SubprocessJudge)
        self.assertEqual(judge.command, ["cat"])  # type: ignore[attr-defined]

    def test_invalid_json_falls_back_to_stub(self):
        with patch.dict(
            os.environ,
            {"AGENTIC_MEMORY_JUDGE_COMMAND": "not json"},
        ):
            judge = get_default_judge()
        self.assertIsInstance(judge, StubJudge)


class TestScoreFrozenDataclass(unittest.TestCase):
    def test_score_is_frozen(self):
        import dataclasses

        self.assertTrue(dataclasses.is_dataclass(JudgeScore))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s = JudgeScore(score=0.5, reasoning="x", rubric="r")
            s.score = 0.9  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
