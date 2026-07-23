"""Agentic Memory backend for LongMemEval-V2.

Implements hybrid retrieval over web-agent trajectories:
1. On insert: extracts goal/outcome summary + per-step (thought, action) facts
   from the trajectory's ``states`` list.  Accessibility trees are mined for
   short visible-text snippets only — the raw tree is never stored in full.
2. On query: retrieves the most relevant evidence using cosine similarity over
   sentence-transformer embeddings with BM25 keyword-boost and RRF fusion.

Mirrors the agentic-memory production pipeline: content-keyed storage,
hybrid search (BM25 + vector), and RRF fusion.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np

from .memory import Memory, MemoryContextItem, register_memory, require


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_matrix() -> np.ndarray:
    return np.zeros((0, 0), dtype=np.float32)


def _append_rows(existing: np.ndarray, new_rows: np.ndarray) -> np.ndarray:
    if existing.size == 0:
        return new_rows
    if new_rows.size == 0:
        return existing
    return np.vstack([existing, new_rows])


def _argsort_desc(arr: np.ndarray) -> list[int]:
    return list(np.argsort(arr)[::-1])


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine similarity between row vector *a* and each row of *b*."""
    if a.size == 0 or b.size == 0:
        return np.array([], dtype=np.float32)
    a_norm = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-10)
    b_norm = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-10)
    return a_norm @ b_norm.T


def _truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...[truncated]...\n" + text[-half:]


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------------------
# Accessibility-tree text extraction
# ---------------------------------------------------------------------------

# Pattern to pull readable text from accessibility-tree lines like:
#   [a184] note '', visible\n\t\t\t\t\t\t\tStaticText 'Number'
_AX_STATIC_TEXT = re.compile(r"StaticText\s+'([^']*)'")
_AX_LABEL_TEXT = re.compile(r"(?:textbox|button|link|heading|label)\s+'([^']*)'")


def _extract_axtree_snippets(ax_tree: str, max_chars: int = 600) -> str:
    """Pull a compact set of human-readable snippets from an accessibility tree."""
    if not ax_tree:
        return ""
    snippets: list[str] = []
    seen: set[str] = set()
    # Grab StaticText values (actual visible text)
    for m in _AX_STATIC_TEXT.findall(ax_tree):
        val = m.strip()
        if val and val not in seen and len(val) > 1:
            seen.add(val)
            snippets.append(val)
    # Grab labelled controls
    for m in _AX_LABEL_TEXT.findall(ax_tree):
        val = m.strip()
        if val and val not in seen and len(val) > 2:
            seen.add(val)
            snippets.append(val)
    result = "; ".join(snippets)
    if len(result) > max_chars:
        result = result[:max_chars] + "…"
    return result


# ---------------------------------------------------------------------------
# Trajectory → text extraction
# ---------------------------------------------------------------------------

def _build_trajectory_summary(trajectory: dict[str, Any], max_chars: int = 6000) -> str:
    """Compact narrative: goal, outcome, then per-step (thought, action) pairs."""
    parts: list[str] = []
    traj_id = trajectory.get("id", "?")
    goal = trajectory.get("goal", "")
    outcome = trajectory.get("outcome", "?")
    domain = trajectory.get("domain", "?")
    start_url = trajectory.get("start_url", "")

    parts.append(f"[{traj_id}] domain={domain} outcome={outcome}")
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

        line_parts: list[str] = []
        if action:
            line_parts.append(f"action={action}")
        if thought:
            line_parts.append(f"thought={thought[:200]}")
        if url:
            line_parts.append(f"url={url[:120]}")

        if line_parts:
            parts.append(f"  Step {i}: {' | '.join(line_parts)}")

    text = "\n".join(parts)
    return _truncate_middle(text, max_chars)


def _build_step_facts(trajectory: dict[str, Any]) -> list[str]:
    """One fact per meaningful (action, thought) pair."""
    facts: list[str] = []
    traj_id = trajectory.get("id", "?")
    domain = trajectory.get("domain", "?")
    outcome = trajectory.get("outcome", "?")
    goal = trajectory.get("goal", "")

    # High-level fact
    facts.append(f"[{traj_id}] {domain} task, outcome={outcome}. Goal: {goal[:300]}")

    states = trajectory.get("states", [])
    for i, state in enumerate(states):
        if not isinstance(state, dict):
            continue
        action = state.get("action")
        thought = (state.get("thought") or "").strip()
        url = (state.get("url") or "").strip()

        if action:
            fact = f"[{traj_id}] Step {i}: {action}"
            if url:
                fact += f" @ {url[:100]}"
            facts.append(fact)

        # Include thought if it contains concrete observations
        if thought and len(thought) > 20:
            # Extract key observations from thought
            thought_fact = f"[{traj_id}] Step {i} observation: {thought[:250]}"
            facts.append(thought_fact)

    return facts


# ---------------------------------------------------------------------------
# Embedding model (lazy-loaded)
# ---------------------------------------------------------------------------

_MODEL_CACHE: dict[str, Any] = {}


def _get_embedding_model(model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> Any:
    if model_name in _MODEL_CACHE:
        return _MODEL_CACHE[model_name]
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(model_name)
        _MODEL_CACHE[model_name] = model
        return model
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

@register_memory
class AgenticMemoryBackend(Memory):
    """Hybrid-retrieval memory for web-agent trajectories.

    Stores per-trajectory summaries and per-step facts as searchable text
    entries with sentence-transformer embeddings.  Retrieval uses cosine
    similarity + BM25 keyword overlap with RRF fusion.
    """

    memory_type = "agentic_memory"

    def __init__(self, memory_params: dict[str, Any]) -> None:
        super().__init__(memory_params)
        self.top_k = int(memory_params.get("top_k", 10))
        self.enable_facts = bool(memory_params.get("enable_facts", True))
        self.enable_bm25_boost = bool(memory_params.get("enable_bm25_boost", True))
        self.max_summary_chars = int(memory_params.get("max_summary_chars", 6000))
        self.embedding_model = str(memory_params.get("embedding_model", "sentence-transformers/all-MiniLM-L6-v2"))

        # Storage
        self.inserted_ids: list[str] = []
        self._inserted_set: set[str] = set()
        self.summary_entries: list[dict[str, Any]] = []
        self.fact_entries: list[dict[str, Any]] = []

        # Embeddings
        self._model: Any = None
        self._model_lock = threading.Lock()
        self.summary_embeddings = _empty_matrix()
        self.fact_embeddings = _empty_matrix()

    # -- model ---------------------------------------------------------------

    def _get_model(self) -> Any:
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    self._model = _get_embedding_model(self.embedding_model)
                    require(
                        self._model is not None,
                        "AgenticMemoryBackend requires sentence-transformers. "
                        "Install: pip install sentence-transformers",
                    )
        return self._model

    def _embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return _empty_matrix()
        model = self._get_model()
        embeddings = model.encode(
            texts,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return np.array(embeddings, dtype=np.float32)

    # -- Memory interface ----------------------------------------------------

    def insert(self, trajectory: dict[str, Any]) -> None:
        traj_id = str(trajectory.get("id", ""))
        require(traj_id, "Trajectory must have an 'id' field")
        if traj_id in self._inserted_set:
            return
        self._inserted_set.add(traj_id)
        self.inserted_ids.append(traj_id)

        # 1) Summary entry
        summary_text = _build_trajectory_summary(trajectory, self.max_summary_chars)
        self.summary_entries.append({
            "entry_id": f"summary_{traj_id}",
            "trajectory_id": traj_id,
            "text": summary_text,
        })
        summary_emb = self._embed_texts([summary_text])
        self.summary_embeddings = _append_rows(self.summary_embeddings, summary_emb)

        # 2) Fact entries
        if self.enable_facts:
            facts = _build_step_facts(trajectory)
            for fact_idx, fact_text in enumerate(facts):
                self.fact_entries.append({
                    "entry_id": f"fact_{traj_id}_{fact_idx}",
                    "trajectory_id": traj_id,
                    "text": fact_text,
                })
            if facts:
                # Batch embed in chunks to avoid memory issues
                chunk_size = 64
                all_embs: list[np.ndarray] = []
                for start in range(0, len(facts), chunk_size):
                    chunk = facts[start:start + chunk_size]
                    all_embs.append(self._embed_texts(chunk))
                fact_embs = np.vstack(all_embs) if all_embs else _empty_matrix()
                self.fact_embeddings = _append_rows(self.fact_embeddings, fact_embs)

    def query(
        self,
        query: str,
        query_image: str | None = None,
    ) -> list[MemoryContextItem]:
        require(isinstance(query, str) and query.strip(), "Query must be non-empty")

        items: list[MemoryContextItem] = []

        # Search summaries
        summary_results = self._search(
            query,
            self.summary_entries,
            self.summary_embeddings,
            top_k=min(self.top_k, len(self.summary_entries)),
        )

        # Search facts
        fact_results: list[dict[str, Any]] = []
        if self.enable_facts and self.fact_entries:
            fact_results = self._search(
                query,
                self.fact_entries,
                self.fact_embeddings,
                top_k=min(self.top_k * 3, len(self.fact_entries)),
            )

        # Build context: facts first (more granular), then summaries
        seen_traj_ids: set[str] = set()
        for result in fact_results:
            items.append({"type": "text", "value": result["text"]})
            seen_traj_ids.add(result["trajectory_id"])

        for result in summary_results:
            if result["trajectory_id"] not in seen_traj_ids:
                items.append({"type": "text", "value": result["text"]})

        return items[: self.top_k * 2]

    # -- internal search -----------------------------------------------------

    def _search(
        self,
        query: str,
        entries: list[dict[str, Any]],
        embeddings: np.ndarray,
        *,
        top_k: int,
    ) -> list[dict[str, Any]]:
        if not entries or embeddings.size == 0:
            return []

        query_emb = self._embed_texts([query])
        vec_scores = _cosine_sim(query_emb, embeddings).flatten()

        if self.enable_bm25_boost:
            query_tokens = set(re.findall(r"\w+", query.lower()))
            keyword_scores = np.zeros(len(entries), dtype=np.float32)
            for i, entry in enumerate(entries):
                entry_tokens = set(re.findall(r"\w+", entry["text"].lower()))
                overlap = len(query_tokens & entry_tokens)
                keyword_scores[i] = overlap / (len(query_tokens) + 1)
            # RRF fusion
            vec_ranks = _argsort_desc(vec_scores)
            kw_ranks = _argsort_desc(keyword_scores)
            fused = np.zeros(len(entries), dtype=np.float32)
            for rank_pos, idx in enumerate(vec_ranks):
                fused[idx] += 1.0 / (60 + rank_pos + 1)
            for rank_pos, idx in enumerate(kw_ranks):
                fused[idx] += 1.0 / (60 + rank_pos + 1)
            scores = fused
        else:
            scores = vec_scores

        top_indices = np.argsort(scores)[::-1][:top_k]
        return [entries[i] for i in top_indices if scores[i] > 0]

    # -- persistence ---------------------------------------------------------

    def _save_backend(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        index = {
            "memory_type": self.memory_type,
            "inserted_trajectory_ids": list(self.inserted_ids),
            "summary_count": len(self.summary_entries),
            "fact_count": len(self.fact_entries),
            "top_k": self.top_k,
            "enable_facts": self.enable_facts,
            "enable_bm25_boost": self.enable_bm25_boost,
        }
        (output_dir / "index.json").write_text(
            json.dumps(index, indent=2) + "\n", encoding="utf-8"
        )
        _write_jsonl(output_dir / "summary_pool.jsonl", self.summary_entries)
        _write_jsonl(output_dir / "fact_pool.jsonl", self.fact_entries)
        if self.summary_embeddings.size:
            np.save(output_dir / "summary_embeddings.npy", self.summary_embeddings)
        if self.fact_embeddings.size:
            np.save(output_dir / "fact_embeddings.npy", self.fact_embeddings)

    def _load_backend(self, input_dir: Path) -> None:
        index = json.loads((input_dir / "index.json").read_text(encoding="utf-8"))
        self.inserted_ids = list(index.get("inserted_trajectory_ids", []))
        self._inserted_set = set(self.inserted_ids)
        self.top_k = int(index.get("top_k", 10))
        self.enable_facts = bool(index.get("enable_facts", True))
        self.enable_bm25_boost = bool(index.get("enable_bm25_boost", True))
        self.summary_entries = _read_jsonl(input_dir / "summary_pool.jsonl")
        self.fact_entries = _read_jsonl(input_dir / "fact_pool.jsonl")
        emb_path = input_dir / "summary_embeddings.npy"
        if emb_path.exists():
            self.summary_embeddings = np.load(emb_path)
        fact_emb_path = input_dir / "fact_embeddings.npy"
        if fact_emb_path.exists():
            self.fact_embeddings = np.load(fact_emb_path)
