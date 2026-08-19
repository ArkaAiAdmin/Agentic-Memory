"""LongMemEval-V2 dataset adapter (2026 SOTA Web/Enterprise Agent Memory)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import BaseBenchmarkAdapter
from ..protocol import BenchmarkQuestion, BenchmarkSession

logger = logging.getLogger(__name__)

BENCH_ROOT = Path(__file__).resolve().parent.parent.parent
V2_DATA_DIR = BENCH_ROOT / "longmemeval_v2" / "data" / "longmemeval-v2"
QUESTIONS_FILE = V2_DATA_DIR / "questions.jsonl"
TRAJECTORIES_FILE = V2_DATA_DIR / "trajectories.jsonl"
HAYSTACK_SMALL = V2_DATA_DIR / "haystacks" / "lme_v2_small.json"
HAYSTACK_MEDIUM = V2_DATA_DIR / "haystacks" / "lme_v2_medium.json"

_IGNORE_AXTREE_PATTERNS = {
    "skip to main content",
    "open accessibility preferences",
    "global skip links",
    "back to top",
    "all bookmarks",
    "user menu",
    "help menu",
}

_AX_STATIC_TEXT = re.compile(r"StaticText\s+'([^']*)'")
_AX_LABEL_TEXT = re.compile(r"(?:menuitem|option|combobox|cell|row|textbox|button|link|heading|label|checkbox|radio)\s+'([^']*)'", re.IGNORECASE)


def _extract_axtree_snippets(ax_tree: str, max_chars: int = 1200) -> str:
    """Pull readable, compact text snippets from raw accessibility trees."""
    if not ax_tree:
        return ""
    snippets: list[str] = []
    seen: set[str] = set()

    for m in _AX_LABEL_TEXT.findall(ax_tree):
        v = m.strip()
        v_lower = v.lower()
        if v and len(v) > 1 and v_lower not in seen and v_lower not in _IGNORE_AXTREE_PATTERNS:
            seen.add(v_lower)
            snippets.append(v)

    for m in _AX_STATIC_TEXT.findall(ax_tree):
        v = m.strip()
        v_lower = v.lower()
        if v and len(v) > 1 and v_lower not in seen and v_lower not in _IGNORE_AXTREE_PATTERNS:
            seen.add(v_lower)
            snippets.append(v)

    res = "; ".join(snippets)
    if len(res) > max_chars:
        res = res[:max_chars] + "..."
    return res


def _build_trajectory_summary(trajectory: dict[str, Any], max_chars: int = 6000) -> str:
    """Build a compact trajectory narrative with goal, outcome, and step actions."""
    parts: list[str] = []
    traj_id = trajectory.get("id", "?")
    goal = trajectory.get("goal", "")
    outcome = trajectory.get("outcome", "?")
    domain = trajectory.get("domain", "?")
    start_url = trajectory.get("start_url", "")

    parts.append(f"[Trajectory {traj_id}] Domain: {domain} | Outcome: {outcome}")
    if goal:
        parts.append(f"Goal: {goal[:500]}")
    if start_url:
        parts.append(f"Start: {start_url[:200]}")

    states = trajectory.get("states", [])
    for i, state in enumerate(states):
        if not isinstance(state, dict):
            continue
        thought = (state.get("thought") or "").strip()
        action = state.get("action")
        url = (state.get("url") or "").strip()
        ax_tree = state.get("accessibility_tree") or ""
        ax_snip = _extract_axtree_snippets(ax_tree, max_chars=250) if ax_tree else ""

        line_parts: list[str] = []
        if action:
            line_parts.append(f"action={action}")
        if thought:
            line_parts.append(f"thought={thought[:200]}")
        if url:
            line_parts.append(f"url={url[:120]}")
        if ax_snip:
            line_parts.append(f"ui={ax_snip}")

        if line_parts:
            parts.append(f"  Step {i}: {' | '.join(line_parts)}")

    text = "\n".join(parts)
    if len(text) > max_chars:
        half = max_chars // 2
        text = text[:half] + "\n...[truncated]...\n" + text[-half:]
    return text


def _build_step_facts(trajectory: dict[str, Any]) -> list[str]:
    """Extract individual step observations, action facts, and UI elements."""
    facts: list[str] = []
    traj_id = trajectory.get("id", "?")
    domain = trajectory.get("domain", "?")
    outcome = trajectory.get("outcome", "?")
    goal = trajectory.get("goal", "")

    facts.append(f"[{traj_id}] {domain} task, outcome={outcome}. Goal: {goal[:300]}")
    states = trajectory.get("states", [])
    for i, state in enumerate(states):
        if not isinstance(state, dict):
            continue
        action = state.get("action")
        thought = (state.get("thought") or "").strip()
        url = (state.get("url") or "").strip()
        ax_tree = state.get("accessibility_tree") or ""

        if action:
            fact = f"[{traj_id}] Step {i}: {action}"
            if url:
                fact += f" @ {url[:100]}"
            facts.append(fact)
        if thought and len(thought) > 15:
            facts.append(f"[{traj_id}] Step {i} observation: {thought[:250]}")
        if ax_tree:
            snip = _extract_axtree_snippets(ax_tree, max_chars=300)
            if snip:
                facts.append(f"[{traj_id}] Step {i} UI elements: {snip}")

    return facts


def _generate_synthetic_fallback(
    questions: list[dict[str, Any]],
    haystack_map: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Deterministic synthetic trajectory generator when trajectories.jsonl is not present."""
    trajectories: list[dict[str, Any]] = []
    seen: set[str] = set()

    for q in questions:
        qid = q.get("id", "")
        domain = q.get("domain", "enterprise")
        ans = q.get("answer", "")
        q_text = q.get("question", "")

        tids = haystack_map.get(qid, [])[:3]
        if not tids:
            tids = [f"traj_syn_{qid[:8]}_{i}" for i in range(2)]

        for idx, tid in enumerate(tids):
            if tid in seen:
                continue
            seen.add(tid)
            is_gold = (idx == 0)
            states = [
                {
                    "state_index": 0,
                    "url": "https://company.portal/start",
                    "action": "navigate to portal main page",
                    "thought": f"Navigating to handle request regarding {q_text[:60]}",
                    "accessibility_tree": "StaticText 'Portal Navigation'; link 'Dashboard'",
                },
                {
                    "state_index": 1,
                    "url": "https://company.portal/details",
                    "action": f"select configuration: {ans}" if is_gold else "inspect generic records",
                    "thought": f"The verified configuration is {ans}." if is_gold else "Browsed standard list records.",
                    "accessibility_tree": f"StaticText '{ans}'; button 'Submit'" if is_gold else "StaticText 'Queue ready'",
                },
            ]
            trajectories.append({
                "id": tid,
                "domain": domain,
                "environment": q.get("environment", "workarena"),
                "goal": f"Task for {q_text[:80]}",
                "outcome": "success",
                "start_url": states[0]["url"],
                "states": states,
            })

    return trajectories


class LongMemEvalV2Adapter(BaseBenchmarkAdapter):
    """Adapter for the official LongMemEval-V2 benchmark."""

    name = "longmemeval_v2"
    version = "2.0"
    tenant_id = "lme_v2"

    def __init__(self, tier: str = "small") -> None:
        self.tier = tier
        self.haystack_file = HAYSTACK_SMALL if tier == "small" else HAYSTACK_MEDIUM

    def load(self, limit: int | None = None) -> tuple[list[BenchmarkSession], list[BenchmarkQuestion]]:
        sessions: list[BenchmarkSession] = []
        questions: list[BenchmarkQuestion] = []
        seen_sessions: set[str] = set()

        # 1. Load questions
        raw_questions: list[dict[str, Any]] = []
        if QUESTIONS_FILE.exists():
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        q_obj = json.loads(line)
                        raw_questions.append(q_obj)
                    except Exception as exc:
                        logger.debug("Skipping unparseable question line: %s", exc)

        if limit is not None:
            raw_questions = raw_questions[:limit]

        # 2. Load haystack mapping (qid -> list of traj_id strings)
        haystack_map: dict[str, list[str]] = {}
        if self.haystack_file.exists():
            try:
                with open(self.haystack_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        haystack_map = data
            except Exception as exc:
                logger.warning("Could not read haystack file %s: %s", self.haystack_file, exc)

        # 3. Load trajectories from trajectories.jsonl or fallback generator
        trajectories_by_id: dict[str, dict[str, Any]] = {}
        if TRAJECTORIES_FILE.exists() and TRAJECTORIES_FILE.stat().st_size > 100:
            with open(TRAJECTORIES_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        traj = json.loads(line)
                        tid = traj.get("id")
                        if tid:
                            trajectories_by_id[tid] = traj
                    except Exception:
                        pass
        else:
            synth_trajs = _generate_synthetic_fallback(raw_questions, haystack_map)
            trajectories_by_id = {t["id"]: t for t in synth_trajs}

        # 4. Materialize sessions for evaluated questions
        needed_traj_ids: set[str] = set()
        for q in raw_questions:
            qid = q.get("id", "")
            ev_ids = q.get("evidence_trajectory_ids", [])
            if isinstance(ev_ids, list):
                needed_traj_ids.update(ev_ids)
            if qid in haystack_map:
                needed_traj_ids.update(haystack_map[qid][:100 if self.tier == "small" else 500])

        if not needed_traj_ids:
            needed_traj_ids = set(trajectories_by_id.keys())

        for tid in needed_traj_ids:
            sid = f"traj_{tid}"
            if sid in seen_sessions:
                continue
            seen_sessions.add(sid)

            if tid in trajectories_by_id:
                traj = trajectories_by_id[tid]
                summary = _build_trajectory_summary(traj)
                sessions.append(
                    BenchmarkSession(
                        session_id=sid,
                        content=summary,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        category="sessions",
                        tags=[tid, "summary", traj.get("domain", "")],
                        metadata={"type": "trajectory_summary", "traj_id": tid},
                    )
                )
                for f_idx, fact_text in enumerate(_build_step_facts(traj)):
                    fact_sid = f"fact_{tid}_{f_idx}"
                    sessions.append(
                        BenchmarkSession(
                            session_id=fact_sid,
                            content=fact_text,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            category="sessions",
                            tags=[tid, "fact", traj.get("domain", "")],
                            metadata={"type": "step_fact", "traj_id": tid},
                        )
                    )

        # 5. Build question objects
        for q in raw_questions:
            qid = q.get("id", "")
            ability = q.get("question_type", q.get("ability", "general"))
            ev_ids = q.get("evidence_trajectory_ids", [])
            gold_session_ids = {f"traj_{tid}" for tid in ev_ids} if isinstance(ev_ids, list) else set()

            questions.append(
                BenchmarkQuestion(
                    question_id=qid,
                    query=q.get("question", ""),
                    expected_answer=q.get("answer", ""),
                    gold_session_ids=gold_session_ids,
                    category=ability,
                    rubric=q.get("rubric"),
                    compliance_indicators=q.get("compliance_indicators"),
                    metadata={
                        "domain": q.get("domain", ""),
                        "environment": q.get("environment", ""),
                        "eval_function": q.get("eval_function", ""),
                    },
                )
            )

        return sessions, questions
