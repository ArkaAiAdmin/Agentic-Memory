#!/usr/bin/env python3
"""Semantic search using model2vec embeddings.

Note: Requires model2vec + numpy installed in the agentic-memory venv.
Run with: ~/.config/agentic-memory/venv/bin/python embedding_search.py <query>
"""
import sys
import json
import sqlite3
from pathlib import Path

# Check if running in venv, if not, warn
venv_python = Path.home() / '.config' / 'agentic-memory' / 'venv' / 'bin' / 'python'
if sys.executable != str(venv_python) and not (Path(sys.executable).parents[1] / '.config' / 'agentic-memory' / 'venv').exists():
    print(f"Warning: For semantic search, run with the venv python:")
    print(f"  {venv_python} {__file__} <query>")
    print()


def find_project_root(start_path):
    for path in [start_path] + list(start_path.parents):
        if (path / 'memory').is_dir() or (path / '.git').exists() or (path / 'CLAUDE.md').exists():
            return path
    return start_path


class EmbeddingSearch:
    def __init__(self):
        self.model = None
        self.np = None
        self._load_model()

    def _load_model(self):
        try:
            from model2vec import StaticModel
            import numpy as np
            self.np = np
            self.model = StaticModel.from_pretrained("minishlab/potion-base-8M")
        except ImportError as e:
            print(f"Warning: {e}. Run: pip install model2vec numpy")
            self.model = None

    def encode(self, texts):
        if self.model is None:
            return None
        return self.model.encode(texts)

    def search(self, query, db_path, limit=5):
        if self.model is None:
            return "Embedding search unavailable. Install model2vec: pip install model2vec numpy"

        db = sqlite3.connect(str(db_path))
        db.execute("PRAGMA busy_timeout = 30000;")

        # Get all memories
        rows = db.execute("SELECT id, content, source_file, tags FROM memories").fetchall()
        if not rows:
            db.close()
            return "No memories found."

        # Encode query and all contents
        query_vec = self.model.encode([query])
        contents = [r[1][:500] for r in rows]  # Truncate for speed
        content_vecs = self.model.encode(contents)

        # Compute cosine similarities
        similarities = self.np.dot(content_vecs, query_vec.T).squeeze()

        # Sort by similarity
        top_indices = self.np.argsort(similarities)[::-1][:limit]

        results = []
        for idx in top_indices:
            row = rows[idx]
            results.append({
                'id': row[0],
                'source': row[2],
                'tags': json.loads(row[3]),
                'score': float(similarities[idx]),
                'preview': row[1][:200]
            })

        db.close()
        return results


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: embedding_search.py <query> [limit]")
        sys.exit(1)

    query = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    root = find_project_root(Path.cwd())
    db_path = root / 'memory' / 'memory.db'

    searcher = EmbeddingSearch()
    results = searcher.search(query, db_path, limit)

    if isinstance(results, str):
        print(results)
    else:
        print(f"\nSemantic search results for: '{query}' (Top {len(results)})")
        print("=" * 80)
        for i, r in enumerate(results, 1):
            print(f"[{i}] {r['id']}  (Score: {r['score']:.4f})")
            print(f"    Source: memory/{r['source']}")
            print(f"    Tags: {', '.join(r['tags'])}")
            print("-" * 80)