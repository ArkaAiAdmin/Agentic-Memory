"""Minimal setup.py — discovers root-level modules AND packages for flat layout.

pyproject.toml owns metadata, dependencies, and console_scripts.
This file only tells setuptools to include the 119 root-level .py files
and the top-level packages (infra/, save/, search/, cron/, hooks/, etc.)
that form the actual MCP server, CLI, pipelines, and background workers.

Without this, only the `agentic_memory/` subpackage is shipped and
`pip install agentic-memory` produces a broken installation.
"""
from __future__ import annotations

import os
from glob import glob
from setuptools import find_packages, setup


def _root_modules() -> list[str]:
    """Discover top-level .py files, stripping the .py suffix."""
    this_dir = os.path.dirname(os.path.abspath(__file__))
    modules = [
        os.path.splitext(os.path.basename(f))[0]
        for f in glob(os.path.join(this_dir, "*.py"))
        if not os.path.basename(f).startswith("_")
    ]
    modules.sort()
    return modules


setup(
    py_modules=_root_modules(),
    packages=find_packages(exclude=["eval", "eval.*", "memory", "memory.*", "venv", "venv.*"]),
    package_data={"": ["*.toml", "*.sh", "*.json", "*.yaml", "*.yml"]},
)
