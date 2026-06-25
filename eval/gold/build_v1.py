#!/usr/bin/env python3
"""
Build v1 gold set for agentic-memory MVE evaluation.

Output:
  - eval/gold/v1.jsonl          (100 hand-curated queries with gold IDs)
  - eval/gold/validation-v1.json (distribution + integrity report)

Idempotent: re-running produces byte-identical outputs.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GOLD_DIR = ROOT / "eval" / "gold"
OUT_JSONL = GOLD_DIR / "v1.jsonl"
OUT_VALIDATION = GOLD_DIR / "validation-v1.json"

CORPORA = {
    "lmeval": "/Users/arka/.config/agentic-memory/memory.db",
    "lrdevplugin": "/Users/arka/Desktop/ai-agent.lrdevplugin/memory/memory.db",
    "taskmanager": "/Users/arka/Assets/TaskManager/memory/memory.db",
    "antinote": "/Users/arka/Assets/AntiNote/memory/memory.db",
    "periodtracker": "/Users/arka/Assets/PeriodTracker/memory/memory.db",
}


def load_ids(corpus_path: str) -> set[str]:
    db = sqlite3.connect(corpus_path)
    return {r[0] for r in db.execute("SELECT id FROM memories").fetchall()}


# ---------------------------------------------------------------------------
# Hand-curated 100 queries. Each entry:
#   (query, corpus_key, gold_ids, relevance, query_type, provenance, notes)
# query_type ∈ {multi_word_technical, single_keyword, natural_language, vague,
#               zero_result, exact_phrase, temporal, tagged, chunked, pinned}
# ---------------------------------------------------------------------------

QUERIES: list[tuple[str, str, list[str], list[int], str, str, str]] = [
    # =================================================================
    # LMEVAL CORPUS (68 queries)
    # =================================================================
    # --- multi_word_technical on lmeval (18) ---
    (
        "sleep journaling consistent schedule strategies",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_9282283d_abs_1"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User asked for sleep strategies beyond journaling",
    ),
    (
        "aquarium nitrite partial water changes",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_c65042d7_1"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User asked about aquarium nitrite levels and water changes",
    ),
    (
        "deep learning beginner online courses",
        "lmeval",
        ["sessions/lmeval-2022-11-17-answer_1e2369c9_1"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User wants deep learning beginner resources",
    ),
    (
        "automatic backup software working files",
        "lmeval",
        ["sessions/lmeval-2023-08-15-answer_e3892371_2"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User asked for automatic backup software options",
    ),
    (
        "chicken wings crispy skin baking tips",
        "lmeval",
        ["sessions/lmeval-2023-05-23-answer_733e443a_abs_2"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User wants tips for crispy skin on chicken wings",
    ),
    (
        "vegan chili meal prep fitness",
        "lmeval",
        ["sessions/lmeval-2023-03-10-answer_9793daa4_2"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User asked for healthy meal prep ideas",
    ),
    (
        "Nordstrom anniversary sale skincare routine",
        "lmeval",
        ["sessions/lmeval-2023-05-26-answer_cfcf5340_2"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User mentioned $500 Nordstrom skincare purchase",
    ),
    (
        "toaster oven recipe ideas new appliance",
        "lmeval",
        ["sessions/lmeval-2023-05-22-answer_728deb4d_3"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User upgraded from toaster to toaster oven",
    ),
    (
        "herbal tea spice blend market trends",
        "lmeval",
        ["sessions/lmeval-2023-06-01-answer_23759615_3"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User researching herbal tea product line",
    ),
    (
        "Mike Trout batting average home runs season",
        "lmeval",
        ["sessions/lmeval-2023-05-22-answer_a22b654d_abs_1"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User asked for Mike Trout latest stats",
    ),
    (
        "haight ashbury restaurant recommendations san francisco",
        "lmeval",
        ["sessions/lmeval-2023-05-27-answer_ab603dd5_2"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User asked for SF Haight-Ashbury restaurants",
    ),
    (
        "Eastern Sierra scenic hiking trails July August",
        "lmeval",
        ["sessions/lmeval-2023-05-15-answer_661b711f_1"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User planning Eastern Sierra hike in summer",
    ),
    (
        "Australia Sydney travel recommendations tips",
        "lmeval",
        ["sessions/lmeval-2023-05-28-answer_a68db5db_2"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User planning Australia Sydney trip",
    ),
    (
        "Hawaii power adapter plug electrical outlet",
        "lmeval",
        ["sessions/lmeval-2023-03-15-answer_5328c3c2_2"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User needs Hawaii power adapters",
    ),
    (
        "coffee maker black stainless three weeks",
        "lmeval",
        ["sessions/lmeval-2023-05-22-answer_c4e5d969_1"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User has issues with new coffee maker",
    ),
    (
        "yoga apps besides youtube beginner practice",
        "lmeval",
        ["sessions/lmeval-2023-06-18-answer_cdbe2250_1"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User asked for yoga app recommendations beyond YouTube",
    ),
    (
        "endometrial cancer molecular subtypes grant",
        "lmeval",
        ["sessions/lmeval-2023-05-22-answer_sharegpt_hfmn2zx_0"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User asked for grants aim page on endometrial cancer subtypes",
    ),
    (
        "Sephora eyeshadow palette points skincare",
        "lmeval",
        ["sessions/lmeval-2023-05-25-answer_66c23110_1"],
        [3],
        "multi_word_technical",
        "lmeval",
        "User bought Sephora eyeshadow and asking skincare advice",
    ),
    # --- single_keyword on lmeval (10) ---
    (
        "audiobook",
        "lmeval",
        ["sessions/lmeval-2023-05-28-answer_5e3bb940_2"],
        [3],
        "single_keyword",
        "lmeval",
        "Single keyword targeting audiobook notes",
    ),
    (
        "playlists",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_47152166_2"],
        [3],
        "single_keyword",
        "lmeval",
        "Single keyword targeting playlist notes",
    ),
    (
        "Tameca",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_e05e4612"],
        [3],
        "single_keyword",
        "lmeval",
        "User asked for music like Tameca Jones",
    ),
    (
        "skincare",
        "lmeval",
        ["sessions/lmeval-2023-05-25-answer_66c23110_1"],
        [3],
        "single_keyword",
        "lmeval",
        "Single keyword for skincare session",
    ),
    (
        "display",
        "lmeval",
        ["sessions/lmeval-2023-05-29-answer_5cc9b056"],
        [3],
        "single_keyword",
        "lmeval",
        "Single keyword for vintage watch display cases",
    ),
    (
        "tank",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_c65042d7_1"],
        [3],
        "single_keyword",
        "lmeval",
        "Single keyword for aquarium tank",
    ),
    (
        "recipe",
        "lmeval",
        ["sessions/lmeval-2023-05-22-answer_728deb4d_3"],
        [3],
        "single_keyword",
        "lmeval",
        "Single keyword for recipe notes",
    ),
    (
        "gallery",
        "lmeval",
        ["sessions/lmeval-2023-03-03-answer_990c8992_3"],
        [3],
        "single_keyword",
        "lmeval",
        "Single keyword for art gallery notes",
    ),
    (
        "workout",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_47152166_2"],
        [3],
        "single_keyword",
        "lmeval",
        "Single keyword for workout playlist notes",
    ),
    (
        "rings",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_sharegpt_2bsxlar_0"],
        [3],
        "single_keyword",
        "lmeval",
        "Single keyword for engagement rings blog",
    ),
    # --- natural_language on lmeval (10) ---
    (
        "What did I say about my recent coffee maker purchase?",
        "lmeval",
        ["sessions/lmeval-2023-05-22-answer_c4e5d969_1"],
        [3],
        "natural_language",
        "lmeval",
        "Natural language question about coffee maker",
    ),
    (
        "What gift am I planning to get my brother for his birthday?",
        "lmeval",
        ["sessions/lmeval-2022-05-15-answer_016f6bd4_2"],
        [3],
        "natural_language",
        "lmeval",
        "User asked about watch gift for brother",
    ),
    (
        "Where did I see live music recently?",
        "lmeval",
        ["sessions/lmeval-2023-04-15-answer_f999b05c_3"],
        [3],
        "natural_language",
        "lmeval",
        "User mentioned seeing Queen at Prudential Center",
    ),
    (
        "What's a good museum for art workshops near me?",
        "lmeval",
        ["sessions/lmeval-2023-03-03-answer_990c8992_3"],
        [3],
        "natural_language",
        "lmeval",
        "User asked for art museums with workshops",
    ),
    (
        "What is the best way to manage tasks when taking time off?",
        "lmeval",
        ["sessions/lmeval-2023-05-23-answer_feb5f98a_1"],
        [3],
        "natural_language",
        "lmeval",
        "User wants task prioritization before time off",
    ),
    (
        "What kind of social media strategy did we discuss for engagement?",
        "lmeval",
        ["sessions/lmeval-2023-05-25-answer_203bf3fa_3"],
        [3],
        "natural_language",
        "lmeval",
        "User asked for help increasing follower count",
    ),
    (
        "How do I write a grants aim page about cancer?",
        "lmeval",
        ["sessions/lmeval-2023-05-22-answer_sharegpt_hfmn2zx_0"],
        [3],
        "natural_language",
        "lmeval",
        "User asked for grants aim page on endometrial cancer",
    ),
    (
        "What podcasts or music similar to Tameca Jones?",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_e05e4612"],
        [3],
        "natural_language",
        "lmeval",
        "User wants music similar to Tameca Jones",
    ),
    (
        "How do I improve my sleep with journaling?",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_9282283d_abs_1"],
        [3],
        "natural_language",
        "lmeval",
        "User asked for sleep strategies beyond journaling",
    ),
    (
        "Where am I planning a trip in the Eastern Sierra?",
        "lmeval",
        ["sessions/lmeval-2023-05-15-answer_661b711f_1"],
        [3],
        "natural_language",
        "lmeval",
        "User planning Eastern Sierra hike",
    ),
    # --- vague on lmeval (10) ---
    (
        "the rings thing",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_sharegpt_2bsxlar_0"],
        [3],
        "vague",
        "lmeval",
        "Vague 'rings' refers to engagement rings post",
    ),
    (
        "recent stuff about Tokyo",
        "lmeval",
        ["sessions/lmeval-2023-05-23-answer_33c251f0_2"],
        [3],
        "vague",
        "lmeval",
        "Vague 'Tokyo' refers to Japan trip planning",
    ),
    (
        "the workout songs",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_47152166_2"],
        [3],
        "vague",
        "lmeval",
        "Vague reference to workout playlists",
    ),
    (
        "volleyball thing",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_53582e7e_2"],
        [3],
        "vague",
        "lmeval",
        "Vague reference to volleyball fitness note",
    ),
    (
        "engagement post we talked about",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_sharegpt_2bsxlar_0"],
        [3],
        "vague",
        "lmeval",
        "Vague 'engagement post' for blog post on engagement rings",
    ),
    (
        "that spa thing",
        "lmeval",
        ["sessions/lmeval-2023-05-24-answer_02b63d04_4"],
        [3],
        "vague",
        "lmeval",
        "Vague 'spa' reference to sinus pressure relief",
    ),
    (
        "fitness post",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_53582e7e_2"],
        [3],
        "vague",
        "lmeval",
        "Vague reference to fitness post",
    ),
    (
        "the yoga stuff",
        "lmeval",
        ["sessions/lmeval-2023-06-18-answer_cdbe2250_1"],
        [3],
        "vague",
        "lmeval",
        "Vague reference to yoga apps",
    ),
    (
        "skincare notes",
        "lmeval",
        ["sessions/lmeval-2023-05-26-answer_cfcf5340_2"],
        [3],
        "vague",
        "lmeval",
        "Vague reference to skincare notes",
    ),
    (
        "engagement rings",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_sharegpt_2bsxlar_0"],
        [3],
        "vague",
        "lmeval",
        "Vague 'engagement rings'",
    ),
    # --- zero_result on lmeval (5) ---
    (
        "kubernetes ingress nginx tls termination",
        "lmeval",
        [],
        [],
        "zero_result",
        "lmeval",
        "Kubernetes not in lmeval corpus",
    ),
    (
        "asdfghjkl random gibberish test",
        "lmeval",
        [],
        [],
        "zero_result",
        "lmeval",
        "Random gibberish, not in corpus",
    ),
    (
        "q4 2099 roadmap strategic planning",
        "lmeval",
        [],
        [],
        "zero_result",
        "lmeval",
        "Date 2099 outside corpus range",
    ),
    (
        "Solidity smart contract reentrancy attack",
        "lmeval",
        [],
        [],
        "zero_result",
        "lmeval",
        "Solidity not in lmeval corpus",
    ),
    (
        "Eigenvalue decomposition quantum circuit",
        "lmeval",
        [],
        [],
        "zero_result",
        "lmeval",
        "Quantum not in lmeval corpus",
    ),
    # --- exact_phrase on lmeval (5) ---
    (
        '"Open-World Games"',
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_8d015d9d_3"],
        [3],
        "exact_phrase",
        "lmeval",
        "Phrase verbatim from open-world games note",
    ),
    (
        '"charity 5K run"',
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_53582e7e_2"],
        [3],
        "exact_phrase",
        "lmeval",
        "Exact phrase from fitness session",
    ),
    (
        '"high-energy playlists"',
        "lmeval",
        ["sessions/lmeval-2023-06-21-answer_ae3a122b_2"],
        [3],
        "exact_phrase",
        "lmeval",
        "Exact phrase from workout playlist session",
    ),
    (
        '"deep learning"',
        "lmeval",
        ["sessions/lmeval-2022-11-17-answer_1e2369c9_1"],
        [3],
        "exact_phrase",
        "lmeval",
        "Exact phrase from deep learning session",
    ),
    (
        '"engagement rings"',
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_sharegpt_2bsxlar_0"],
        [3],
        "exact_phrase",
        "lmeval",
        "Exact phrase from engagement rings blog post",
    ),
    # --- temporal on lmeval (5) ---
    (
        "recent lmeval sessions 2024",
        "lmeval",
        ["sessions/lmeval-2024-02-20-answer_e6b6353d"],
        [3],
        "temporal",
        "lmeval",
        "Most recent 2024 session — 20-mile bike ride hydration",
    ),
    (
        "latest pinned note in corpus",
        "lmeval",
        ["lessons/test-harness-destructive-wipe-guard"],
        [3],
        "temporal",
        "lmeval",
        "Most recently created pinned note in lmeval DB",
    ),
    (
        "oldest lessons in the corpus",
        "lmeval",
        ["lessons/eval-test-isolation-bug-deep-e2e"],
        [3],
        "temporal",
        "lmeval",
        "First pinned lesson by ID sort order",
    ),
    (
        "sessions from May 2023 about travel",
        "lmeval",
        [
            "sessions/lmeval-2023-05-28-answer_a68db5db_2",
            "sessions/lmeval-2023-05-27-answer_ab603dd5_2",
        ],
        [3, 3],
        "temporal",
        "lmeval",
        "Temporal+chunked — multiple travel sessions in May 2023",
    ),
    (
        "sessions from 2022",
        "lmeval",
        [
            "sessions/lmeval-2022-05-15-answer_016f6bd4_2",
            "sessions/lmeval-2022-11-17-answer_1e2369c9_1",
        ],
        [3, 3],
        "temporal",
        "lmeval",
        "Temporal+chunked — two 2022 sessions",
    ),
    # --- tagged on lmeval (5) — search by frontmatter tag in content ---
    (
        "tagged multi-session",
        "lmeval",
        ["sessions/lmeval-2023-03-08-answer_a679a86a_2"],
        [3],
        "tagged",
        "lmeval",
        "Tagged query: frontmatter tag 'multi-session'",
    ),
    (
        "tagged temporal-reasoning",
        "lmeval",
        ["sessions/lmeval-2023-11-01-answer_c18d480b_1"],
        [3],
        "tagged",
        "lmeval",
        "Tagged query: frontmatter tag 'temporal-reasoning'",
    ),
    (
        "tagged knowledge-update",
        "lmeval",
        ["sessions/lmeval-2023-05-20-answer_sharegpt_2bsxlar_0"],
        [3],
        "tagged",
        "lmeval",
        "Tagged query: frontmatter tag 'knowledge-update'",
    ),
    (
        "tagged single-session-preference",
        "lmeval",
        ["sessions/lmeval-2023-05-21-answer_f3164f2c"],
        [3],
        "tagged",
        "lmeval",
        "Tagged query: frontmatter tag 'single-session-preference'",
    ),
    (
        "tagged single-session-user",
        "lmeval",
        ["sessions/lmeval-2023-05-22-answer_ef84b994_1"],
        [3],
        "tagged",
        "lmeval",
        "Tagged query: frontmatter tag 'single-session-user'",
    ),
    # =================================================================
    # LRDEVPLUGIN (8 queries)
    # =================================================================
    (
        "Lightroom Classic AI agent Lua plugin",
        "lrdevplugin",
        ["projects/lightroom-agent"],
        [3],
        "multi_word_technical",
        "project_synth",
        "Lightroom agent project note",
    ),
    (
        '"MVVM" mobile architecture',
        "lrdevplugin",
        ["preferences/workflow"],
        [3],
        "exact_phrase",
        "project_synth",
        "Exact phrase MVVM from workflow preferences",
    ),
    (
        "DeepSeek free tier tool_choice 400",
        "lrdevplugin",
        ["lessons/api-pitfalls"],
        [3],
        "multi_word_technical",
        "project_synth",
        "API pitfalls DeepSeek note",
    ),
    (
        "Android emulator macOS Tahoe freeze",
        "lrdevplugin",
        ["quirks/android-emulator-freeze"],
        [3],
        "multi_word_technical",
        "project_synth",
        "Quirks note about Android emulator freeze",
    ),
    (
        "tagged lesson",
        "lrdevplugin",
        ["lessons/api-pitfalls", "lessons/ip-guardrails"],
        [3, 3],
        "tagged",
        "project_synth",
        "Tagged query: notes with 'lesson' tag in lrdevplugin",
    ),
    (
        "JSONL session restore timestamp shadowing",
        "lrdevplugin",
        ["lessons/jsonl-reconstruction"],
        [3],
        "multi_word_technical",
        "project_synth",
        "JSONL log reconstruction lesson",
    ),
    (
        "ADR hybrid memory storage",
        "lrdevplugin",
        ["decisions/adr-001-hybrid-memory-storage"],
        [3],
        "vague",
        "project_synth",
        "ADR 001 — hybrid storage strategy",
    ),
    (
        "pinned project notes",
        "lrdevplugin",
        ["projects/lightroom-agent", "projects/grounded-memory", "projects/kyma"],
        [3, 3, 3],
        "pinned",
        "project_synth",
        "Pinned projects in lrdevplugin (lightroom-agent, grounded-memory, kyma)",
    ),
    # =================================================================
    # TASKMANAGER (8 queries)
    # =================================================================
    (
        "TaskManager glassmorphic design system",
        "taskmanager",
        ["projects/taskmanager"],
        [3],
        "multi_word_technical",
        "project_synth",
        "TaskManager project design rules",
    ),
    (
        "com.taskmanager.app package",
        "taskmanager",
        ["projects/taskmanager"],
        [3],
        "multi_word_technical",
        "project_synth",
        "Package name reference in TaskManager project",
    ),
    (
        "grounded memory system phase 1",
        "taskmanager",
        ["projects/grounded-memory"],
        [3],
        "multi_word_technical",
        "project_synth",
        "Grounded Memory project phase 1",
    ),
    (
        "tagged sqlite concurrency",
        "taskmanager",
        ["lessons/atomic-sqlite-ops"],
        [3],
        "tagged",
        "project_synth",
        "Tagged query: sqlite concurrency tag",
    ),
    (
        "compaction proposal auto-generated session",
        "taskmanager",
        ["sessions/compaction-proposal"],
        [3],
        "vague",
        "project_synth",
        "Compaction proposal session note",
    ),
    (
        "Kyma period tracker Nielsen score",
        "taskmanager",
        ["projects/kyma"],
        [3],
        "vague",
        "project_synth",
        "Kyma period tracker project note",
    ),
    (
        "AntiNote macos scratchpad math",
        "taskmanager",
        ["projects/antimote"],
        [3],
        "natural_language",
        "project_synth",
        "AntiNote scratchpad concept",
    ),
    (
        "tagged memory-system",
        "taskmanager",
        [
            "lessons/memory-system-eval-results",
            "lessons/memory-system-operations",
            "lessons/memory-systems-comparative-analysis",
            "lessons/next-level-memory-features",
            "projects/grounded-memory",
        ],
        [3, 3, 3, 3, 3],
        "tagged",
        "project_synth",
        "Tagged query: all memory-system tagged notes in taskmanager",
    ),
    # =================================================================
    # ANTINOTE (8 queries)
    # =================================================================
    (
        "AntiNote scratchpad live math expression",
        "antinote",
        ["projects/antimote"],
        [3],
        "multi_word_technical",
        "project_synth",
        "AntiNote live math expression concept",
    ),
    (
        "AntiNoteApp.swift main window layout",
        "antinote",
        ["projects/antimote"],
        [3],
        "multi_word_technical",
        "project_synth",
        "AntiNote main window file",
    ),
    (
        "MathEngine expression parsing",
        "antinote",
        ["projects/antimote"],
        [3],
        "multi_word_technical",
        "project_synth",
        "MathEngine file in AntiNote",
    ),
    (
        "tagged macos",
        "antinote",
        ["projects/antimote"],
        [3],
        "tagged",
        "project_synth",
        "Tagged query: macos tag in antinote",
    ),
    (
        "pinned project antimote",
        "antinote",
        ["projects/antimote"],
        [3],
        "pinned",
        "project_synth",
        "Pinned antimote project",
    ),
    (
        "EVAL-01 lexical search recall",
        "antinote",
        ["lessons/memory-system-eval-results"],
        [3],
        "exact_phrase",
        "project_synth",
        "Exact phrase EVAL-01 from memory eval results",
    ),
    (
        "pinned workflow preferences",
        "antinote",
        ["preferences/workflow"],
        [3],
        "pinned",
        "project_synth",
        "Pinned workflow preferences in antinote",
    ),
    (
        "next-level memory features prompt caching",
        "antinote",
        ["lessons/next-level-memory-features"],
        [3],
        "multi_word_technical",
        "project_synth",
        "Next level memory features lesson",
    ),
    # =================================================================
    # PERIODTRACKER (8 queries)
    # =================================================================
    (
        "Kyma cycle ring tracker calendar",
        "periodtracker",
        ["projects/kyma"],
        [3],
        "multi_word_technical",
        "project_synth",
        "Kyma cycle ring tracker feature",
    ),
    (
        "Jetpack Compose Room DB canvas visualization",
        "periodtracker",
        ["projects/kyma"],
        [3],
        "multi_word_technical",
        "project_synth",
        "Kyma stack mention",
    ),
    (
        "MEMORY.md index bug fix empty",
        "periodtracker",
        ["lessons/memory-md-index-bug-fix"],
        [3],
        "multi_word_technical",
        "project_synth",
        "MEMORY.md index bug fix lesson",
    ),
    (
        "agentic memory github push repo",
        "periodtracker",
        ["lessons/agentic-memory-github-push"],
        [3],
        "multi_word_technical",
        "project_synth",
        "Agentic memory github push lesson",
    ),
    (
        "tagged pregnancy kick counter contraction",
        "periodtracker",
        ["projects/kyma"],
        [3],
        "vague",
        "project_synth",
        "Kyma pending work — pregnancy dashboard features",
    ),
    (
        "memory system hardening AGENTS.md CLAUDE.md",
        "periodtracker",
        ["lessons/memory-system-hardened"],
        [3],
        "multi_word_technical",
        "project_synth",
        "Memory system hardening lesson",
    ),
    (
        "tagged bug-fix critical",
        "periodtracker",
        ["lessons/memory-md-index-bug-fix"],
        [3],
        "tagged",
        "project_synth",
        "Tagged query: bug-fix critical tag in periodtracker",
    ),
    (
        "pinned memory-system architecture",
        "periodtracker",
        [
            "lessons/grounded-memory-v2-implementation",
            "lessons/memory-system-hardened",
            "projects/grounded-memory",
            "lessons/atomic-sqlite-ops",
            "lessons/api-pitfalls",
            "lessons/ip-guardrails",
        ],
        [3, 3, 3, 3, 3, 3],
        "pinned",
        "project_synth",
        "Pinned memory-system/architecture-tagged notes in periodtracker",
    ),
]


# ---------------------------------------------------------------------------
# Validation + write
# ---------------------------------------------------------------------------
def main() -> int:
    GOLD_DIR.mkdir(parents=True, exist_ok=True)

    assert len(QUERIES) == 100, f"Expected exactly 100 queries, got {len(QUERIES)}"

    corpus_ids: dict[str, set[str]] = {k: load_ids(v) for k, v in CORPORA.items()}

    # Build entries + validate
    entries: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    type_counter: Counter[str] = Counter()
    by_corpus: Counter[str] = Counter()
    by_provenance: Counter[str] = Counter()

    for i, (query, corpus, golds, relevance, qtype, provenance, notes) in enumerate(
        QUERIES, start=1
    ):
        if corpus not in CORPORA:
            print(f"FAIL q{i:03d}: unknown corpus '{corpus}'")
            return 1

        q_norm = query.lower().strip()
        if q_norm in seen_queries:
            print(f"FAIL q{i:03d}: duplicate query: {query!r}")
            return 1
        seen_queries.add(q_norm)

        for gid in golds:
            if gid not in corpus_ids[corpus]:
                print(f"FAIL q{i:03d}: gold_id '{gid}' not in corpus '{corpus}'")
                return 1

        if len(relevance) != len(golds):
            print(
                f"FAIL q{i:03d}: relevance length {len(relevance)} != gold_ids length {len(golds)}"
            )
            return 1

        if qtype == "zero_result" and (golds or relevance):
            print(f"FAIL q{i:03d}: zero_result query must have empty golds/relevance")
            return 1

        if qtype != "zero_result" and not golds:
            print(f"FAIL q{i:03d}: non-zero_result query must have gold_ids")
            return 1

        entry = {
            "id": f"q{i:03d}",
            "query": query,
            "corpus": CORPORA[corpus],
            "gold_ids": list(golds),
            "relevance": list(relevance),
            "provenance": provenance,
            "notes": notes,
        }
        entries.append(entry)
        type_counter[qtype] += 1
        by_corpus[CORPORA[corpus]] += 1
        by_provenance[provenance] += 1

    # Distribution minimums check
    MINIMUMS = {
        "multi_word_technical": 20,
        "single_keyword": 10,
        "natural_language": 10,
        "vague": 10,
        "zero_result": 5,
        "exact_phrase": 5,
        "temporal": 5,
        "tagged": 5,
    }
    for k, mn in MINIMUMS.items():
        if type_counter.get(k, 0) < mn:
            print(
                f"FAIL: query type '{k}' only has {type_counter.get(k, 0)} entries, need >= {mn}"
            )
            return 1

    # Corpus distribution check (~70 lmeval, 7-8 per project DB)
    lmeval_count = by_corpus.get(CORPORA["lmeval"], 0)
    project_counts = [
        by_corpus.get(CORPORA[k], 0)
        for k in ("lrdevplugin", "taskmanager", "antinote", "periodtracker")
    ]
    if not (65 <= lmeval_count <= 75):
        print(f"WARN: lmeval count {lmeval_count} outside target ~70")
    for k, c in zip(
        ("lrdevplugin", "taskmanager", "antinote", "periodtracker"), project_counts
    ):
        if not (6 <= c <= 10):
            print(f"WARN: {k} count {c} outside target 7-8")

    # Aggregates
    total_query_words = sum(len(e["query"].split()) for e in entries)
    mean_query_words = round(total_query_words / len(entries), 2)
    total_golds = sum(len(e["gold_ids"]) for e in entries)
    mean_gold_per_query = round(total_golds / len(entries), 2)

    # Write JSONL
    with OUT_JSONL.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    validation = {
        "total": len(entries),
        "by_corpus": dict(by_corpus),
        "by_provenance": dict(by_provenance),
        "by_query_type": dict(type_counter),
        "mean_query_words": mean_query_words,
        "mean_gold_per_query": mean_gold_per_query,
        "duplicates": 0,
        "all_golds_exist": True,
    }
    with OUT_VALIDATION.open("w", encoding="utf-8") as f:
        json.dump(validation, f, indent=2, sort_keys=True)

    print(f"WROTE {OUT_JSONL} ({len(entries)} entries)")
    print(f"WROTE {OUT_VALIDATION}")
    print(f"\nby corpus: {dict(by_corpus)}")
    print(f"by provenance: {dict(by_provenance)}")
    print(f"by query type: {dict(type_counter)}")
    print(f"mean_query_words: {mean_query_words}")
    print(f"mean_gold_per_query: {mean_gold_per_query}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
