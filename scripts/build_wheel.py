#!/usr/bin/env python3
"""Build clean namespaced wheel artifact for agentic-memory."""

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def build_wheel() -> None:
    root = Path(__file__).resolve().parent.parent
    dist_dir = root / "dist"
    shutil.rmtree(dist_dir, ignore_errors=True)
    dist_dir.mkdir(exist_ok=True)

    python_bin = sys.executable
    venv_py = root / "venv" / "bin" / "python"
    if venv_py.exists():
        python_bin = str(venv_py)

    print(f"[Build] Building clean namespaced agentic-memory wheel from {root} using {python_bin}...")

    # Create temporary staging directory
    with tempfile.TemporaryDirectory(prefix="agentic_mem_build_") as tmpdir:
        staging = Path(tmpdir)
        pkg_dir = staging / "agentic_memory"
        pkg_dir.mkdir(parents=True)

        # 1. Copy agentic_memory/ subpackage
        src_pkg = root / "agentic_memory"
        if src_pkg.exists():
            for item in src_pkg.iterdir():
                if item.name in ("__pycache__", ".pytest_cache"):
                    continue
                if item.is_dir():
                    shutil.copytree(item, pkg_dir / item.name)
                else:
                    shutil.copy2(item, pkg_dir / item.name)

        # 2. Copy core subpackages into agentic_memory/
        subpackages = [
            "infra",
            "save",
            "search",
            "cron",
            "hooks",
            "kg",
            "fact",
            "mcp_surface",
            "knowledge_graph",
            "recall",
            "background",
            "backfill",
        ]
        for sub in subpackages:
            sub_src = root / sub
            if sub_src.exists() and sub_src.is_dir():
                dest = pkg_dir / sub
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(sub_src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        # 3. Copy root .py modules into agentic_memory/
        excluded_files = {"setup.py", "sitecustomize.py", "conftest.py"}
        for f in root.glob("*.py"):
            if f.name.startswith("_") or f.name in excluded_files:
                continue
            shutil.copy2(f, pkg_dir / f.name)

        # 4. Copy data/schema assets
        for ext in ("*.toml", "*.json", "*.yaml", "*.yml", "*.sql"):
            for f in root.glob(ext):
                shutil.copy2(f, pkg_dir / f.name)

        # 5. Write README.md and setup.py / pyproject.toml to staging root
        readme = root / "README.md"
        if readme.exists():
            shutil.copy2(readme, staging / "README.md")
        else:
            (staging / "README.md").write_text("# Agentic Memory\n")

        import tomllib
        with open(root / "pyproject.toml", "rb") as f:
            pyproject_data = tomllib.load(f)
        version = pyproject_data["project"]["version"]

        setup_content = f"""from setuptools import setup, find_packages

setup(
    name="agentic-memory",
    version="{version}",
    description="Local-first persistent memory for AI agents — markdown-native, MCP server with temporal KG, CRDT sync, hybrid search. Apache 2.0.",
    packages=find_packages(include=["agentic_memory", "agentic_memory.*"]),
    package_data={"agentic_memory": ["*.toml", "*.json", "*.yaml", "*.yml", "*.sql", "*.sh"]},
    install_requires=[
        "mcp>=1.0.0,<2",
        "numpy>=2.0.0,<3",
    ],
    extras_require={
        "embeddings": ["model2vec>=0.8.0,<1", "usearch>=2.0.0,<3"],
        "reranker": ["torch>=2.0.0,<3", "transformers>=4.0.0,<5"],
        "ltr": ["lightgbm>=4.0.0,<5"],
        "ner": ["spacy>=3.8.0,<4"],
    },
    entry_points={
        "console_scripts": [
            "agentic-memory = agentic_memory:main",
            "agentic-memory-server = agentic_memory.cli:server_main",
            "agentic-memory-search = agentic_memory.cli:search_main",
            "agentic-memory-rebuild = agentic_memory.cli:rebuild_main",
            "agentic-memory-backfill = agentic_memory.cli:backfill_main",
            "agentic-memory-consolidate = agentic_memory.cli:consolidate_main",
            "agentic-memory-integrity = agentic_memory.cli:integrity_main",
            "agentic-memory-tier = agentic_memory.cli:tier_main",
            "agentic-memory-compact = agentic_memory.cli:compact_main",
            "agentic-memory-bootstrap = agentic_memory.cli:bootstrap_main",
            "agentic-memory-worker = agentic_memory.cli:worker_main",
            "agentic-memory-sync = agentic_memory.cli:sync_main",
            "agentic-memory-init = agentic_memory.cli:init_main",
            "agentic-memory-doctor = agentic_memory.cli:doctor_main",
            "agentic-memory-status = agentic_memory.cli:status_main",
            "agentic-memory-version = agentic_memory.cli:version_main",
            "agentic-memory-install-mcp = agentic_memory.cli:install_mcp_main",
            "agentic-memory-dashboard = agentic_memory.cli:dashboard_main",
            "agentic-memory-api = agentic_memory.cli:api_server_main",
        ],
    },
)
"""
        (staging / "setup.py").write_text(setup_content)

        # 6. Run build in staging directory
        build_cmd = [python_bin, "-m", "build", "--wheel", "--outdir", str(dist_dir)]
        res = subprocess.run(build_cmd, cwd=str(staging))
        if res.returncode != 0:
            print("[Build] Trying setup.py bdist_wheel fallback...")
            fallback_cmd = [python_bin, "setup.py", "bdist_wheel", "--dist-dir", str(dist_dir)]
            res = subprocess.run(fallback_cmd, cwd=str(staging))
            if res.returncode != 0:
                print("[Build] Trying pip wheel fallback...")
                pip_cmd = [python_bin, "-m", "pip", "wheel", "--no-deps", "-w", str(dist_dir), "."]
                subprocess.run(pip_cmd, cwd=str(staging), check=True)

    # Clean up any root build/ or .egg-info artifacts if created
    shutil.rmtree(root / "build", ignore_errors=True)
    for p in root.glob("*.egg-info"):
        shutil.rmtree(p, ignore_errors=True)

    wheels = list(dist_dir.glob("*.whl"))
    print(f"[✓] Built {len(wheels)} wheel(s):")
    for w in wheels:
        print(f"  - {w.name} ({w.stat().st_size:,} bytes)")
        with zipfile.ZipFile(w, "r") as zf:
            top_levels = set(p.split("/")[0] for p in zf.namelist())
            print(f"    Top-level items in wheel: {sorted(top_levels)}")
            dist_info_name = f"agentic_memory-{version}.dist-info"
            assert top_levels.issubset({"agentic_memory", dist_info_name}), (
                f"Wheel pollutes site-packages with: {top_levels - {'agentic_memory', dist_info_name}}"
            )
    print("[✓] Verification passed: Zero site-packages pollution!")


if __name__ == "__main__":
    build_wheel()

