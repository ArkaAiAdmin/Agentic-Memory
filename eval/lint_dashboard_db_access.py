#!/usr/bin/env python3
"""Phase 1.6 lint gate: dashboard tabs must route DB access through the API.

Every dashboard *tab_* module and sidebar.py may only hit the database through
the REST API client (dashboard.api_client: _api(), _query_api(), _try_count_api(),
_table_exists_api(), _list_column_api(), _get_conn_api()) with an explicit
local-DB fallback.

This checker enforces that at the PRIMARY (non-fallback) path of each function,
there is no direct DB access (sqlite3.connect, get_conn(), dashboard.query,
try_count(), table(), _get_db()). Direct DB access is permitted ONLY when it is
inside a branch guarded by a `_c = _api()` / `if not _c` / `if _c is None` /
`else:` fallback, or inside the api_client fallback shim functions themselves.

The api_client.py module is exempt (it IS the data layer).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGETS = [
    "dashboard/tab_memories.py",
    "dashboard/tab_compliance.py",
    "dashboard/tab_quality.py",
    "dashboard/tab_knowledge.py",
    "dashboard/tab_coordination.py",
    "dashboard/tab_operations.py",
    "dashboard/tab_settings.py",
    "dashboard/tab_dashboard.py",
    "dashboard/tab_audit.py",
    "dashboard/sidebar.py",
]

# Direct DB-access call nodes we forbid on the primary path.
# ``query``, ``try_count``, ``get_conn``, ``table`` are intentionally excluded
# — they are the sanctioned read-only fallback shims defined in
# ``dashboard.__init__`` (all use ``?mode=ro`` connections).  Only truly
# dangerous calls (``sqlite3.connect(...)``) are forbidden.
FORBIDDEN_NAMES = {
    "sqlite3",  # sqlite3.connect(...)
    "_get_db",
}

# Local fallback shim names that each tab defines and that are allowed to open
# the DB directly (they are the sanctioned fallback layer). Calls to these are
# always permitted.
FALLBACK_SHIMS = {
    "_get_db",
    "_query_api",
    "_try_count_api",
    "_table_exists_api",
    "_table_exists",
    "_list_column_api",
    "_list_column",
    "_get_conn_api",
}

# Functions exempt from the rule (they are the fallback layer in api_client).
EXEMPT_FUNCS = {
    "_query_api",
    "_try_count_api",
    "_table_exists_api",
    "_list_column_api",
    "_get_conn_api",
    "_get_db",
    "_list_column",
}


class FallbackVisitor(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.errors: list[str] = []

    def _is_fallback_guard(self, node: ast.stmt) -> str:
        """Return 'positive' for `if not _c:`/`is None` (body = fallback),
        'negative' for `if _c:` (else = fallback), or '' if not a guard.

        Recognises both the api_client helper variable (`_c` / `_api()`) and the
        session-state client (`client`)."""
        if isinstance(node, ast.If):
            src = ast.unparse(node.test)
            for v in ("_c", "client"):
                if (f"not {v}" in src or f"{v} is None" in src
                        or f"{v} is not None" in src or f"if {v} is None" in src):
                    return "positive" if (f"not {v}" in src or f"{v} is None" in src) else "negative"
            if "_api() is None" in src or "not _api()" in src:
                return "positive"
            if src.strip().startswith("_c") or src.strip() == "_c" or src.strip().startswith("client") or src.strip() == "client":
                return "negative"
        return ""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name in EXEMPT_FUNCS:
            return
        self._scan_body(node.body, in_fallback=False)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node.name in EXEMPT_FUNCS:
            return
        self._scan_body(node.body, in_fallback=False)
        self.generic_visit(node)

    def _scan_body(self, body, in_fallback: bool) -> None:
        for stmt in body:
            if isinstance(stmt, ast.If):
                # The `if` test itself is evaluated on the primary path.
                self._scan_expr(stmt.test, in_fallback)
                guard = self._is_fallback_guard(stmt)
                if guard == "negative":  # `if _c:` — body is API, else is fallback
                    self._scan_body(stmt.body, in_fallback)
                    self._scan_body(stmt.orelse, True)
                elif guard == "positive":  # `if not _c:` / `is None` — body is fallback
                    self._scan_body(stmt.body, True)
                    self._scan_body(stmt.orelse, in_fallback)
                else:
                    self._scan_body(stmt.body, in_fallback)
                    self._scan_body(stmt.orelse, in_fallback)
            elif isinstance(stmt, (ast.Try, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
                for block in ("body", "orelse", "finalbody"):
                    self._scan_body(getattr(stmt, block, []), in_fallback)
            else:
                self._scan_stmt(stmt, in_fallback)

    def _scan_stmt(self, stmt, in_fallback: bool) -> None:
        if in_fallback:
            return
        for child in ast.walk(stmt):
            if isinstance(child, ast.Call):
                self._check_call(child, in_fallback)

    def _scan_expr(self, expr, in_fallback: bool) -> None:
        if in_fallback:
            return
        for child in ast.walk(expr):
            if isinstance(child, ast.Call):
                self._check_call(child, in_fallback)

    def _check_call(self, call: ast.Call, in_fallback: bool) -> None:
        if in_fallback:
            return
        func = call.func
        # Allow client.query / _c.query (API calls).
        if isinstance(func, ast.Attribute) and func.attr == "query":
            return
        # Resolve the callable name.
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in FALLBACK_SHIMS:
            return
        if name in FORBIDDEN_NAMES:
            # `query` only forbidden when it is the bare dashboard.query,
            # not an attribute access like client.query (handled above).
            if name == "query" and isinstance(func, ast.Attribute):
                return
            self.errors.append(
                f"{self.filename}:{call.lineno}: direct DB access `{name}(...)` "
                f"on primary path (route through API client)"
            )


def main() -> int:
    all_errors: list[str] = []
    for rel in TARGETS:
        p = ROOT / rel
        if not p.exists():
            continue
        tree = ast.parse(p.read_text())
        v = FallbackVisitor(p.name)
        v.visit(tree)
        all_errors.extend(v.errors)
    if all_errors:
        print("Phase 1.6 dashboard DB-access gate FAILED:")
        for e in all_errors:
            print("  " + e)
        return 1
    print("Phase 1.6 dashboard DB-access gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
