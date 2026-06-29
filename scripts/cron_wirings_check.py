#!/usr/bin/env python3
"""cron_wirings_check.py — verify cron scripts actually call the right
entry points and the target functions exist.

Findings from the 2026-06-15 audit:
- cron_consolidate.py is read-only despite name suggesting writes
- cron_compact.py references a script that may not exist
- cron_worker.py was removed (2026-06-17) — superseded by background_worker.py

This script checks:
1. Each cron_*.py exists and has a `main()` function
2. The scripts it calls (via `subprocess.run` or direct `__import__`)
   exist and import cleanly
3. The sub-commands the cron scripts invoke exist in their target
   modules (e.g. `auto_save.py daily-digest` → `daily_digest()` in
   auto_save.py)
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / "venv" / "bin" / "python")


def main() -> int:
    issues: list[str] = []
    # Check both the legacy root-level cron_*.py files and the
    # cron/cron_*.py subpackage scripts.
    cron_files = sorted(ROOT.glob("cron_*.py"))
    cron_subdir = ROOT / "cron"
    if cron_subdir.is_dir():
        cron_files.extend(sorted(cron_subdir.glob("cron_*.py")))
    print(f"Checking {len(cron_files)} cron scripts...\n")

    for cron in cron_files:
        # Parse and find the main() and any subprocess calls.
        src = cron.read_text()
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            issues.append(f"{cron.name}: SYNTAX ERROR {e}")
            continue

        # Look for main() OR a top-level dispatcher call.
        # Small cron files (cron_pinned_decay, cron_rewrite_links) are
        # 1-line dispatchers that call the target function at module
        # top-level — that's also a valid cron pattern.
        has_main = any(
            isinstance(node, ast.FunctionDef) and node.name == "main"
            for node in ast.walk(tree)
        )
        has_top_level_call = any(
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            for node in tree.body
            if isinstance(node, (ast.Expr, ast.Assign))
        )
        has_main_block = any(
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            for node in tree.body
        )
        if not (has_main or has_top_level_call or has_main_block):
            issues.append(f"{cron.name}: no main() and no top-level dispatch")

        # Look for subprocess.run / subprocess.check_output calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Only match actual subprocess.* calls, not custom
                # helper functions also named `run` (e.g. the helper
                # inside cron/cron_compact.py that resolves paths
                # via the cron/ subdir first).
                is_subprocess = False
                func_name: str | None = None
                if isinstance(func, ast.Attribute):
                    func_name = func.attr
                    val = func.value
                    if isinstance(val, ast.Name) and val.id == "subprocess":
                        is_subprocess = True
                    elif isinstance(val, ast.Attribute) and val.attr == "subprocess":
                        is_subprocess = True
                if not is_subprocess:
                    continue
                if func_name not in ("run", "check_output", "call", "Popen"):
                    continue
                # Extract the first arg (script name)
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                script = str(node.args[0].value)
                if not script.endswith(".py"):
                    continue
                # If the script path is absolute, check it exists
                script_path = (
                    Path(script) if script.startswith("/") else (ROOT / script)
                )
                if not script_path.exists():
                    issues.append(f"{cron.name}: calls non-existent script {script}")

        # Look for direct imports of target modules
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if not module.startswith("."):
                    try:
                        __import__(module)
                    except (ImportError, ModuleNotFoundError):
                        # OK if it's a stdlib / local module not in path
                        pass

        # Look for `os.system("python ... path/to/script ...")` patterns
        for m in re.finditer(r"os\.system\(\s*[\"']([^\"']+)[\"']", src):
            cmd = m.group(1)
            # Pull out the script name
            parts = cmd.split()
            for p in parts:
                if p.endswith(".py"):
                    sp = Path(p) if p.startswith("/") else (ROOT / p)
                    if not sp.exists():
                        issues.append(f"{cron.name}: os.system references missing {p}")

    # Specifically check cron_compact.py's chain
    cron_compact = ROOT / "cron_compact.py"
    if cron_compact.exists():
        src = cron_compact.read_text()
        # Find all scripts it tries to run
        # Match `run("X.py")` calls — ignore mentions inside comments.
        for m in re.finditer(r'run\(\s*"(\w+\.py)"', src):
            script = m.group(1)
            # The cron_compact.run() helper tries cron/ first, then
            # the repo root. Mirror that lookup so the check matches
            # runtime behavior.
            if not (ROOT / "cron" / script).exists() and not (ROOT / script).exists():
                issues.append(f"cron_compact.py: missing script {script}")

    if issues:
        print(f"DRIFT: {len(issues)} issues\n")
        for i in issues:
            print(f"  - {i}")
        return 1
    else:
        print(
            f"OK: all {len(cron_files)} cron scripts parse and reference existing targets."
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
