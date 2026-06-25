#!/usr/bin/env python3
"""Lightweight mutation testing for agentic-memory.

Mutates key functions in core modules and runs the adversarial + concurrent
test suites to detect surviving mutants. Works around mutmut's limitation
with sys.path-based imports by patching modules in-place.

Usage:
    ~/.config/agentic-memory/venv/bin/python eval/mutation_test.py [--module NAME] [--budget SECS]
"""

import argparse
import ast
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

# Modules to mutate (core logic only)
TARGET_MODULES = {
    "memory_common": INSTALL_DIR / "memory_common.py",
    "save_pipeline": INSTALL_DIR / "save_pipeline.py",
    "search_pipeline": INSTALL_DIR / "search_pipeline.py",
    "embedding_search": INSTALL_DIR / "embedding_search.py",
    "audit": INSTALL_DIR / "audit.py",
    "contradiction_detector": INSTALL_DIR / "contradiction_detector.py",
    "knowledge_graph": INSTALL_DIR / "knowledge_graph/__init__.py",
    "spaced_repetition": INSTALL_DIR / "spaced_repetition.py",
    "memory_delete": INSTALL_DIR / "memory_delete.py",
    "recall": INSTALL_DIR / "recall.py",
    "consolidation": INSTALL_DIR / "consolidation.py",
    "backfill_all": INSTALL_DIR / "backfill_all.py",
    "memory_injection": INSTALL_DIR / "memory_injection.py",
}


def mutate_number(node):
    """Change a numeric literal: 0→1, 1→0, N→N+1, N→N-1."""
    if not hasattr(node, "value") or not hasattr(node, "lineno"):
        return None
    if isinstance(node.value, bool):
        return ("bool", ast.Constant(value=not node.value))
    elif isinstance(node.value, int):
        if node.value == 0:
            return ("int", ast.Constant(value=1))
        elif node.value == 1:
            return ("int", ast.Constant(value=0))
        else:
            return ("int", ast.Constant(value=node.value + 1))
    elif isinstance(node.value, float):
        return ("float", ast.Constant(value=node.value + 1.0))
    return None


def mutate_compare(node):
    """Swap comparison operators: <→<=, >→>=, ==→!=, !=→==, <=→<, >=→>."""
    if not hasattr(node, "ops") or not node.ops or not hasattr(node, "lineno"):
        return None
    op_map = {
        ast.Lt: ast.LtE,
        ast.LtE: ast.Lt,
        ast.Gt: ast.GtE,
        ast.GtE: ast.Gt,
        ast.Eq: ast.NotEq,
        ast.NotEq: ast.Eq,
    }
    new_op = op_map.get(type(node.ops[0]))
    if new_op:
        return (
            "compare",
            ast.Compare(left=node.left, ops=[new_op()], comparators=node.comparators),
        )
    return None


def mutate_not(node):
    """Add or remove not in BoolOp or unary expressions."""
    if not hasattr(node, "lineno"):
        return None
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and hasattr(node, "operand")
    ):
        return ("not", node.operand)
    return None


def mutate_return_none(node):
    """Change return X to return None."""
    if (
        isinstance(node, ast.Return)
        and hasattr(node, "value")
        and node.value is not None
        and hasattr(node, "lineno")
    ):
        return ("return_none", ast.Return(value=ast.Constant(value=None)))
    return None


def _node_position(node):
    """Get (lineno, col_offset) for a node, used as identity."""
    return (getattr(node, "lineno", None), getattr(node, "col_offset", None))


class MutantCollector(ast.NodeTransformer):
    """Collect and apply one mutation at a time to an AST."""

    def __init__(self, source, mutators):
        self.source = source
        self.mutators = mutators
        self.mutations = []
        self._collect(ast.parse(source))

    def _collect(self, tree):
        for node in ast.walk(tree):
            for mutator in self.mutators:
                result = mutator(node)
                if result is not None:
                    mut_type, replacement = result
                    pos = _node_position(node)
                    self.mutations.append((pos, replacement, mut_type))

    def apply(self, index):
        """Return source with mutation at index applied."""
        tree = ast.parse(self.source)
        target_pos, replacement, _ = self.mutations[index]

        class Applier(ast.NodeTransformer):
            def generic_visit(self, node):
                if _node_position(node) == target_pos:
                    return ast.copy_location(replacement, node)
                return super().generic_visit(node)

        new_tree = Applier().visit(tree)
        ast.fix_missing_locations(new_tree)
        return ast.unparse(new_tree)


def run_tests(test_files, timeout=300):
    """Run test files and return (passed, failed, output)."""
    cmd = (
        [sys.executable, "-m", "pytest"]
        + test_files
        + ["-x", "-q", "--tb=line", "--no-header"]
    )
    env = os.environ.copy()
    env["MEMORY_RERANKER_DISABLED"] = "1"
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(INSTALL_DIR),
            env=env,
        )
        output = result.stdout + result.stderr
        # Parse results
        import re

        m = re.search(r"(\d+)\s+passed", output)
        passed = int(m.group(1)) if m else 0
        m = re.search(r"(\d+)\s+failed", output)
        failed = int(m.group(1)) if m else 0
        m = re.search(r"(\d+)\s+error", output)
        errors = int(m.group(1)) if m else 0
        return passed, failed + errors, output
    except subprocess.TimeoutExpired:
        return 0, 1, "TIMEOUT"
    except Exception as e:
        return 0, 1, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", default="all", help="Module to mutate (or 'all')")
    parser.add_argument(
        "--budget", type=int, default=600, help="Time budget in seconds"
    )
    parser.add_argument(
        "--test",
        default="adversarial",
        help="Test suite: adversarial, concurrent, unit, both, all",
    )
    args = parser.parse_args()

    test_files_map = {
        "adversarial": ["eval/test_adversarial_e2e.py"],
        "concurrent": ["eval/test_concurrent.py"],
        "unit": [
            "eval/test_save_pipeline_unit.py",
            "eval/test_search_pipeline_unit.py",
            "eval/test_memory_common_unit.py",
        ],
        "unit-save": [
            "eval/test_save_pipeline_unit.py",
            "eval/test_mutation_killers.py",
        ],
        "unit-search": [
            "eval/test_search_pipeline_unit.py",
            "eval/test_mutation_killers.py",
        ],
        "unit-memory": [
            "eval/test_memory_common_unit.py",
            "eval/test_memory_common_mutation_killers.py",
        ],
        "unit-save-fast": ["eval/test_save_pipeline_unit.py"],
        "unit-search-fast": ["eval/test_search_pipeline_unit.py"],
        "unit-memory-fast": ["eval/test_memory_common_unit.py"],
        "both": ["eval/test_adversarial_e2e.py", "eval/test_concurrent.py"],
        "all": [
            "eval/test_adversarial_e2e.py",
            "eval/test_concurrent.py",
            "eval/test_save_pipeline_unit.py",
            "eval/test_search_pipeline_unit.py",
            "eval/test_memory_common_unit.py",
        ],
    }
    test_files = test_files_map[args.test]

    if args.module == "all":
        modules = TARGET_MODULES
    else:
        if args.module not in TARGET_MODULES:
            print(
                f"Unknown module: {args.module}. Available: {list(TARGET_MODULES.keys())}"
            )
            sys.exit(1)
        modules = {args.module: TARGET_MODULES[args.module]}

    # First, run baseline to confirm tests pass
    print("=" * 60)
    print("BASELINE: Running tests against original code...")
    print("=" * 60)
    passed, failed, output = run_tests(test_files)
    if failed > 0:
        print(f"FAIL: Baseline tests failing ({failed} failed). Fix tests first.")
        print(output[-500:])
        sys.exit(1)
    print(f"  Baseline: {passed} passed, {failed} failed")

    # Run baseline once more to get accurate timing
    t0 = time.time()
    passed, failed, _ = run_tests(test_files)
    baseline_time = time.time() - t0
    print(f"  Baseline time: {baseline_time:.1f}s")

    mutators = [mutate_number, mutate_compare, mutate_not, mutate_return_none]
    total_mutations = 0
    killed = 0
    survived = 0
    errors_count = 0
    survived_details = []

    start_time = time.time()

    for mod_name, mod_path in modules.items():
        if time.time() - start_time > args.budget:
            print(f"\nBudget exhausted after {args.budget}s")
            break

        source = mod_path.read_text()
        collector = MutantCollector(source, mutators)
        n_mutations = len(collector.mutations)
        print(f"\n{'=' * 60}")
        print(f"MODULE: {mod_name} ({n_mutations} mutations found)")
        print(f"{'=' * 60}")

        # Estimate time
        est_time = (n_mutations * baseline_time) + (n_mutations * 2)
        remaining_budget = args.budget - (time.time() - start_time)
        max_mutants = min(
            n_mutations, int(remaining_budget / max(baseline_time + 2, 5))
        )
        print(
            f"  Testing {max_mutants}/{n_mutations} mutations (est {est_time:.0f}s total, budget {remaining_budget:.0f}s)"
        )

        for i in range(max_mutants):
            if time.time() - start_time > args.budget:
                print(f"\n  Budget exhausted at mutation {i}/{max_mutants}")
                break

            mutated_source = collector.apply(i)
            _, _, mutation_desc = collector.mutations[i]

            # Write mutated source
            shutil.copy2(mod_path, mod_path.with_suffix(".py.bak"))
            try:
                mod_path.write_text(mutated_source)

                # Run tests
                t_start = time.time()
                p, f, output = run_tests(
                    test_files, timeout=max(int(baseline_time * 3), 60)
                )
                elapsed = time.time() - t_start
                total_mutations += 1

                if f > 0:
                    killed += 1
                    status = "KILLED"
                elif "ERROR" in output or "error" in output.lower():
                    errors_count += 1
                    status = "ERROR"
                else:
                    survived += 1
                    survived_details.append((mod_name, i, mutation_desc, output[-200:]))
                    status = "SURVIVED"

                print(
                    f"  [{i + 1}/{max_mutants}] {mutation_desc:30s} → {status:10s} ({elapsed:.1f}s)"
                )

            finally:
                # Restore original
                backup = mod_path.with_suffix(".py.bak")
                if backup.exists():
                    shutil.copy2(backup, mod_path)
                    backup.unlink()

    # Summary
    elapsed_total = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"MUTATION TESTING SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Total mutations tested: {total_mutations}")
    print(f"  Killed (caught by tests): {killed}")
    print(f"  Survived (not caught):   {survived}")
    print(f"  Errors:                  {errors_count}")
    if total_mutations > 0:
        kill_rate = killed / total_mutations * 100
        print(f"  Kill rate:               {kill_rate:.1f}%")
    print(f"  Time:                    {elapsed_total:.1f}s")

    if survived_details:
        print(f"\n{'=' * 60}")
        print(f"SURVIVED MUTANTS (weak test coverage)")
        print(f"{'=' * 60}")
        for mod, idx, desc, snippet in survived_details:
            print(f"  {mod} mutation #{idx}: {desc}")

    # Restore all originals
    for mod_name, mod_path in modules.items():
        backup = mod_path.with_suffix(".py.bak")
        if backup.exists():
            shutil.copy2(backup, mod_path)
            backup.unlink()


if __name__ == "__main__":
    main()
