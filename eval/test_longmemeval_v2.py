"""Unit tests for the LongMemEval-V2 benchmark evaluation runner and adapter."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from eval.bench.adapters.longmemeval_v2 import LongMemEvalV2Adapter, _extract_axtree_snippets, _build_trajectory_summary, _build_step_facts
from eval.longmemeval_v2_eval import (
    extract_boxed_answer,
    mc_choice_match,
    norm_phrase_set_match,
    norm_phrase_set_match_ordered,
    load_dataset,
    build_or_load_db,
    run_evaluation,
)


def test_extract_axtree_snippets():
    raw_ax = """
    [a10] heading 'ServiceNow Portal'
    [a20] StaticText 'Total Incidents: 42'
    [a30] textbox 'Search catalog'
    [a40] button 'Submit'
    """
    snippets = _extract_axtree_snippets(raw_ax)
    assert "Total Incidents: 42" in snippets
    assert "Search catalog" in snippets
    assert "Submit" in snippets


def test_trajectory_summary_and_facts_builder():
    traj = {
        "id": "test_traj_001",
        "domain": "enterprise",
        "outcome": "success",
        "goal": "Reassign tickets to tier 2 engineers",
        "start_url": "https://service-now.com/nav",
        "states": [
            {
                "state_index": 0,
                "url": "https://service-now.com/incidents",
                "action": "click on incident filter",
                "thought": "Opening incident dropdown to filter open tickets.",
                "accessibility_tree": "StaticText 'Active Incidents'; button 'Filter'",
            },
            {
                "state_index": 1,
                "url": "https://service-now.com/reassign",
                "action": "select assignment group: Tier 2 Support",
                "thought": "Assigned problem tag to Tier 2 group.",
                "accessibility_tree": "StaticText 'Tier 2 Support'",
            },
        ],
    }

    summary = _build_trajectory_summary(traj)
    assert "[Trajectory test_traj_001]" in summary
    assert "Reassign tickets to tier 2 engineers" in summary
    assert "Tier 2 Support" in summary

    facts = _build_step_facts(traj)
    assert len(facts) >= 3
    assert any("Step 0 Action" in f or "Step 0" in f for f in facts)
    assert any("Tier 2 Support" in f for f in facts)


def test_norm_phrase_matching_evaluators():
    # Phrase set match (unordered)
    pred = "The open incident portal shows Incident Mobile, My Open Incidents, and Incident Portal options."
    gold = "Incident Mobile, Incident Portal, My Open Incidents"
    assert norm_phrase_set_match(pred, gold)

    # Missing phrase should fail
    incomplete_pred = "The open portal shows Incident Mobile and Incident Portal."
    assert not norm_phrase_set_match(incomplete_pred, gold)

    # Ordered phrase set match
    ordered_pred = "First navigate to Reports, then open Problems module."
    ordered_gold = "Reports;Problems"
    assert norm_phrase_set_match_ordered(ordered_pred, ordered_gold)

    reversed_pred = "First open Problems module, then view Reports."
    assert not norm_phrase_set_match_ordered(reversed_pred, ordered_gold)


def test_mc_choice_matching():
    assert mc_choice_match("\\boxed{B}", "B")
    assert mc_choice_match("Option B.", "B")
    assert mc_choice_match("Choice G", "G")
    assert not mc_choice_match("\\boxed{A}", "B")


def test_boxed_answer_extraction():
    assert extract_boxed_answer("Final decision is \\boxed{Austin, Texas} for location.") == "Austin, Texas"
    assert extract_boxed_answer("The result is \\boxed{300}") == "300"
    assert extract_boxed_answer("Plain answer without box") == "Plain answer without box"


def test_adapter_loading():
    adapter = LongMemEvalV2Adapter(tier="small")
    sessions, questions = adapter.load(limit=5)
    assert len(questions) == 5
    assert len(sessions) > 0
    q0 = questions[0]
    assert q0.question_id != ""
    assert q0.query != ""
    assert q0.metadata is not None
    assert "domain" in q0.metadata


def test_quick_evaluation_run():
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir)
        results = run_evaluation(
            tier="small",
            domain="all",
            max_questions=3,
            use_cache_db=False,
            rebuild=True,
            light=True,
            output_path=out_dir / "test_results.json",
        )
        assert results["total_questions"] == 3
        assert "overall_accuracy" in results["macro_metrics"]
        assert len(results["results"]) == 3
        assert (out_dir / "test_results.json").exists()
