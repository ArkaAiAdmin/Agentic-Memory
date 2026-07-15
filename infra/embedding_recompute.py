#!/usr/bin/env python3
"""Cron wrapper: embedding_recompute — detect model change, auto-rebuild vec index.

Compares the current embedding model config against the stored vec
index metadata. If the model has changed (dimensions, model name, or
api_base), triggers a full vec index rebuild.

Usage:
    venv/bin/python embedding_recompute.py [--force] [--dry-run]
"""

import logging
logger = logging.getLogger(__name__)
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from infra.memory_common import GLOBAL_MEM_DIR
from infra.infrastructure import resolve_active_memory_dir

# File that stores the vec index metadata
VEC_META_FILE = GLOBAL_MEM_DIR / "vec_index.meta.json"


def get_current_model_config() -> dict:
    """Get the current embedding model configuration."""
    try:
        # Try embedding_search module first (reads config + env vars)
        from infra.embedding_search import get_embedding_search, MODEL_ID

        es = get_embedding_search()
        if es.model is not None:
            return {
                "model": MODEL_ID,
                "api_base": "local",
                "dimensions": getattr(es.model, "dim", 768),
            }
        # Fallback: check if model is cached locally
        from pathlib import Path
        model_dir = Path.home() / ".cache" / "huggingface" / "hub"
        bge_dirs = list(model_dir.glob("models--BAAI--bge-base-en-v1.5*"))
        if bge_dirs:
            return {
                "model": "BAAI/bge-base-en-v1.5",
                "api_base": "local",
                "dimensions": 768,
            }
        return {"model": MODEL_ID, "api_base": "local", "dimensions": 768}
    except Exception as e:
        logger.warning("get_current_model_config failed: %s", e)
        return {"model": "", "api_base": "", "dimensions": 0}


def get_stored_model_config() -> dict:
    """Get the model config that was used to build the vec index."""
    if not VEC_META_FILE.exists():
        return {}
    try:
        config = json.loads(VEC_META_FILE.read_text(encoding="utf-8"))
        if isinstance(config, dict):
            return config
        return {}
    except Exception as e:
        logger.warning("get_stored_model_config failed: %s", e)
        return {}


def save_model_config(config: dict):
    """Save the current model config as the vec index metadata."""
    VEC_META_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def check_and_rebuild(force: bool = False, dry_run: bool = False) -> dict:
    """Check if embedding model changed, rebuild if needed.

    Returns: {"changed": bool, "rebuilt": bool, "details": str}
    """
    current = get_current_model_config()
    stored = get_stored_model_config()

    if not current.get("model"):
        return {
            "changed": False,
            "rebuilt": False,
            "details": "Could not detect current embedding model",
        }

    # Check if model changed
    model_changed = (
        force
        or current.get("model") != stored.get("model")
        or current.get("api_base") != stored.get("api_base")
        or current.get("dimensions") != stored.get("dimensions")
    )

    if not model_changed:
        return {
            "changed": False,
            "rebuilt": False,
            "details": f"Model unchanged: {current.get('model')}",
        }

    # Model changed — rebuild
    if not dry_run:
        import subprocess
        # 2026-06-29 fix: venv lookup chain. Hardcoded
        # `GLOBAL_MEM_DIR.parent / "venv" / "bin" / "python"` only works
        # on the user's local install; on CI the project lives at
        # /home/runner/work/.../ and the venv is right next to it. Try
        # the project-root venv, then `.venv`, then fall back to
        # sys.executable (always works because we ARE the venv python
        # when running inside a test).
        from infra.memory_config import install_root

        _project_root = Path(install_root()) if not os.environ.get(
            "MEMORY_INSTALL_ROOT"
        ) else Path(os.environ["MEMORY_INSTALL_ROOT"])
        venv_python = str(_project_root / "venv" / "bin" / "python")
        if not Path(venv_python).exists():
            venv_python = str(_project_root / ".venv" / "bin" / "python")
        if not Path(venv_python).exists():
            venv_python = sys.executable
        rebuild_script = str(Path(__file__).resolve().parent.parent / "rebuild_vec_index.py")
        db_path = str(GLOBAL_MEM_DIR / "memory.db")

        result = subprocess.run(
            [venv_python, rebuild_script, db_path],
            capture_output=True, text=True, timeout=300,
        )

        if result.returncode == 0:
            save_model_config(current)
            return {
                "changed": True,
                "rebuilt": True,
                "details": (
                    f"Model changed: {stored.get('model', 'unknown')} → "
                    f"{current.get('model')}. Vec index rebuilt."
                ),
            }
        else:
            return {
                "changed": True,
                "rebuilt": False,
                "details": f"Model changed but rebuild failed: {result.stderr[:200]}",
            }
    else:
        if not dry_run:
            save_model_config(current)
        return {
            "changed": True,
            "rebuilt": False,
            "details": f"Model changed: {stored.get('model', 'unknown')} → {current.get('model')} (dry run)" if dry_run else f"Model changed: {stored.get('model', 'unknown')} → {current.get('model')}",
        }


def main():
    force = "--force" in sys.argv
    dry_run = "--dry-run" in sys.argv

    db_path = resolve_active_memory_dir() / "memory.db"
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        sys.exit(1)

    stats = check_and_rebuild(force=force, dry_run=dry_run)
    if stats["changed"]:
        print(f"Embedding recomputation: {stats['details']}")
    else:
        print(f"Embedding recomputation: {stats['details']}")


if __name__ == "__main__":
    main()
