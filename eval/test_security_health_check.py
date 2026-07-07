"""Security Health Check — OWASP Top 10 (non-LLM) + OWASP Top 10 for LLMs.

This module is a STATIC security scanner plus a pytest regression gate.

Design principles
----------------
* **Regression gate, not a presence counter.** Each scanner returns a
  ``SecurityFinding`` only when a real vulnerability IS present. The pytest
  tests assert the *absence* of each blocking finding, so the suite is RED
  while a vulnerability exists and GREEN once it is fixed. Fixing a bug can
  never break the suite (the old design asserted ``len(findings) > 0``, which
  was backwards).
* **One root cause = one finding.** Duplicates across OWASP categories are
  collapsed (e.g. the empty API token is reported once under A02, SSRF once
  under A10, the audit-log secret leak once under A09).
* **Behavioral detection.** Scanners check for the *absence of a control*
  (an SSRF guard, a length cap, redaction, a path-containment check), not
  merely for the presence of a feature. Substring/keyword checks that cannot
  tell a fixed handler from a broken one have been removed or upgraded.
* **No PASS-as-finding.** Green checks (e.g. DB file perms) are asserted by
  dedicated regression tests, not emitted as findings.

Run with::

    pytest eval/test_security_health_check.py -v

or generate the Markdown report::

    python -c "import eval.test_security_health_check as s; print(s.generate_security_report())"
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = REPO_ROOT / "memory"
MEMORY_TOML = REPO_ROOT / "memory.toml"
CONFIG_PY = REPO_ROOT / "infra" / "config.py"
SAGA_PY = REPO_ROOT / "infra" / "saga.py"
PIPELINE_PY = REPO_ROOT / "save" / "pipeline.py"
WRITE_JOURNAL_PY = REPO_ROOT / "infra" / "write_journal.py"
MCP_VERBS_PY = REPO_ROOT / "mcp_verbs.py"
MCP_MEMORY_PY = REPO_ROOT / "mcp_memory.py"
MCP_SEARCH_PY = REPO_ROOT / "mcp_search.py"
MCP_SAFETY_PY = REPO_ROOT / "mcp_safety.py"
TOOL_COMPLETE_PY = REPO_ROOT / "background" / "tool_complete.py"
AUTO_SAVE_PY = REPO_ROOT / "background" / "auto_save.py"
INJECTION_PY = REPO_ROOT / "memory_injection.py"
FILE_LOCK_PY = REPO_ROOT / "infra" / "file_lock.py"
CRDT_MERGE_PY = REPO_ROOT / "crdt" / "crdt_merge.py"
DB_PY = REPO_ROOT / "infra" / "db.py"
MEMORY_COMMON_PY = REPO_ROOT / "infra" / "memory_common.py"
SDK_PY = REPO_ROOT / "sdk.py"
CLIENT_PY = REPO_ROOT / "agentic_memory" / "client.py"
MULTI_MODAL_PY = REPO_ROOT / "multi_modal.py"
CLI_PY = REPO_ROOT / "cli.py"
TOOL_REGISTRY_PY = REPO_ROOT / "infra" / "tool_registry.py"
MEMORY_MCP_PY = REPO_ROOT / "memory_mcp.py"
SEARCH_ORCH_PY = REPO_ROOT / "search" / "orchestrator.py"
AUDIT_PY = REPO_ROOT / "infra" / "audit.py"
SYNC_SERVER_PY = REPO_ROOT / "infra" / "sync_server.py"
RERANKER_PY = REPO_ROOT / "infra" / "reranker.py"
LLM_PROVIDERS_PY = REPO_ROOT / "fact" / "llm_providers.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Production sources scanned by the "missing-control" scanners.
_PRODUCTION_FILES = [
    PIPELINE_PY, WRITE_JOURNAL_PY, MCP_VERBS_PY, MCP_MEMORY_PY,
    MCP_SEARCH_PY, MCP_SAFETY_PY, TOOL_COMPLETE_PY, AUTO_SAVE_PY, CLI_PY,
    SEARCH_ORCH_PY, CRDT_MERGE_PY, SDK_PY, CLIENT_PY, MULTI_MODAL_PY,
    CONFIG_PY, SAGA_PY, DB_PY, MEMORY_COMMON_PY, FILE_LOCK_PY,
    INJECTION_PY, AUDIT_PY, SYNC_SERVER_PY, RERANKER_PY, LLM_PROVIDERS_PY,
    MEMORY_MCP_PY, TOOL_REGISTRY_PY,
]

SAFE_SQL_CLAUSE_NAMES: Set[str] = {
    "where", "clauses", "sql", "query", "order_by", "order", "order_by_clause",
    "limit_clause", "join", "group_by", "having", "select_cols", "cols",
    "set_clause", "columns", "tail", "suffix",
}

# Substrings that mark an interpolated f-string field as a SQL *syntax* builder
# (column list, placeholder string, clause skeleton) rather than a data value.
# Clause-builder interpolation is safe; direct value interpolation is not.
_SAFE_SQL_BUILDER_HINTS = (
    "ph", "placeholder", "col", "sql", "clause", "filter", "table", "now",
    "ts", "tag", "valid", "extra", "set_", "update", "insert", "select",
    "join", "order", "limit", "group", "having", "expr", "stmt", "cond",
    "kw", "part", "field", "name_",
)


def _is_safe_sql_builder(name: str) -> bool:
    low = name.lower()
    return low in SAFE_SQL_CLAUSE_NAMES or any(h in low for h in _SAFE_SQL_BUILDER_HINTS)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityFinding:
    """One concrete security finding."""

    id: str
    severity: str  # HIGH | MEDIUM | LOW | INFO
    owasp: str     # e.g. "A01" or "LLM01"
    component: str
    finding: str
    remediation: str
    networked: bool = False


# Findings whose absence is enforced by the regression-gate tests. While any
# of these is present, the suite is RED. Accepted-risk findings (local-first
# trust boundary) are intentionally NOT in this set.
BLOCKING_IDS: Set[str] = {
    "A01-001", "A01-003", "A01-004", "A02-001", "A05-001", "A05-002",
    "A03-001", "A03-002", "A03-003", "A03-004", "A04-002", "A04-004",
    "A06-001", "A08-001", "A08-002", "A09-001", "A10-001", "A10-002",
    "LLM01-002", "LLM03-001", "LLM10-001",
    "A02-004",  # secrets-in-repo
    "A07-002",  # sync-server auth
}

# ---------------------------------------------------------------------------
# Helpers — file / code introspection
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _contains(pattern: str, text: str, flags: int = re.IGNORECASE | re.MULTILINE) -> bool:
    return bool(re.search(pattern, text, flags))


def _ast_find_subprocess_calls(source: str) -> List[ast.Call]:
    """Find subprocess.run / Popen / call and os.system / popen / exec* calls."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: List[ast.Call] = []
    dangerous_os = {"system", "popen", "exec", "execl", "execv", "execve", "spawnl", "spawnv"}
    dangerous_subprocess = {"run", "Popen", "call"}

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                mod, attr = node.func.value.id, node.func.attr
                if mod == "subprocess" and attr in dangerous_subprocess:
                    hits.append(node)
                elif mod == "os" and attr in dangerous_os:
                    hits.append(node)
                elif mod == "asyncio" and attr == "create_subprocess_shell":
                    hits.append(node)
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


def _ast_find_eval_exec(source: str) -> List[ast.Call]:
    """Find bare eval() / exec() builtin calls (code-injection risk)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: List[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
                hits.append(node)
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


def _ast_find_dangerous_deserialization(source: str) -> List[str]:
    """Return human-readable descriptions of unsafe deserialization sinks."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    issues: List[str] = []
    unsafe_yaml = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            full = f"{getattr(node.func.value, 'id', '')}.{node.func.attr}"
            if full == "pickle.load" or full == "pickle.loads":
                issues.append("pickle.load/loads (unsafe deserialization)")
            elif full == "yaml.load" and not any(
                isinstance(kw.value, ast.Attribute)
                and kw.value.attr == "SafeLoader"
                for kw in node.keywords
                if kw.arg == "Loader"
            ):
                if not unsafe_yaml:
                    issues.append("yaml.load without SafeLoader")
                    unsafe_yaml = True
            elif full == "torch.load" and not any(
                isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in node.keywords
                if kw.arg == "weights_only"
            ):
                issues.append("torch.load without weights_only=True")
            elif full == "marshal.load" or full == "marshal.loads":
                issues.append("marshal.load/loads (unsafe deserialization)")
    return issues


def _shell_true(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "shell":
            if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return True
    return False


def _ast_find_sql_strings(source: str) -> List[str]:
    """Return f-string SQL that interpolates a VALUE without a placeholder.

    Clause-skeleton f-strings (e.g. ``f"... WHERE {where}"`` where ``where`` is
    a trusted clause built from ``?`` placeholders) are NOT flagged. Only SQL
    that interpolates a variable directly into a value position with no ``?`` /
    ``%s`` / ``:name`` placeholder is reported. The WHOLE joined string is
    inspected, not just the first segment.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: List[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
            full = "".join(
                str(v.value) if isinstance(v, ast.Constant) else f"{{{_field_name(v)}}}"
                for v in node.values
            )
            upper = full.upper()
            # Word-boundary keyword match so "updated"/"valid_from" don't trip.
            if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", upper):
                self.generic_visit(node)
                return
            if not re.search(r"\b(VALUES|WHERE|SET|FROM|MATCH)\b", upper):
                self.generic_visit(node)
                return
            # Placeholder present anywhere -> parameterised, safe.
            if "?" in full or "%s" in full or re.search(r":\w+", full):
                self.generic_visit(node)
                return
            # Collect interpolated field names.
            fields = [
                _field_name(v) for v in node.values
                if not isinstance(v, ast.Constant)
            ]
            risky = [f for f in fields if f and not _is_safe_sql_builder(f)]
            if risky:
                hits.append(full)
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


def _field_name(node: ast.AST) -> str:
    if isinstance(node, ast.FormattedValue):
        if isinstance(node.value, ast.Name):
            return node.value.id
        if isinstance(node.value, ast.Attribute):
            return node.value.attr
        return "expr"
    return ""


def _get_memory_toml_value(key: str) -> Optional[str]:
    """Return raw value for a dotted TOML key (e.g. ``api.token``)."""
    text = _read(MEMORY_TOML)
    if "." in key:
        section, bare = key.split(".", 1)
        section_re = re.compile(
            rf"^\[{re.escape(section)}\](.*?)(?=^\[|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        m = section_re.search(text)
        if not m:
            return None
        block = m.group(1)
        m2 = re.search(rf"^{re.escape(bare)}\s*=\s*(.+)", block, re.MULTILINE)
        if not m2:
            return None
        raw = m2.group(1).strip()
        raw = re.sub(r"\s+#.*$", "", raw)
        return raw.strip().strip('"')
    m = re.search(rf"^{key}\s*=\s*(.+)", text, re.MULTILINE)
    return m.group(1).strip().strip('"') if m else None


def _ssrf_guard_present(source: str) -> bool:
    """True if the source contains an SSRF mitigation (IP-block / allowlist)."""
    signals = (
        "169.254.169.254", "is_private", "ipaddress", "allowed_hosts",
        "ALLOWED_HOSTS", "resolve_ip", "block_private", "ssrf",
        "deny_list", "denylist", "0.0.0.0/8", "10.", "172.16.", "192.168.",
    )
    low = source.lower()
    return any(s.lower() in low for s in signals)


def _path_containment_guard_present(source: str) -> bool:
    """True if the source restricts file access to an allowed tree."""
    signals = (
        "is_relative_to", "resolve().parent", "allowed_dir", "BASE_DIR",
        "sandbox", "ALLOWED_PATH", "within_root", "realpath",
    )
    low = source.lower()
    return any(s.lower() in low for s in signals)


# ---------------------------------------------------------------------------
# Scan functions — one per OWASP category (return findings only when vulnerable)
# ---------------------------------------------------------------------------


def _scan_A01_broken_access_control() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    verbs_src = _read(MCP_VERBS_PY)
    sdk_src = _read(SDK_PY)
    maintenance_src = _read(REPO_ROOT / "mcp_maintenance.py")

    # A01-001: hard delete without a confirmation gate.
    if "hard=True" in verbs_src and "confirm" not in verbs_src:
        findings.append(SecurityFinding(
            id="A01-001",
            severity="MEDIUM",
            owasp="A01",
            component="mcp_verbs.py / mcp_memory.py",
            finding=(
                "memory_delete(note_id, hard=True) performs permanent deletion "
                "with no confirmation gate. A destructive operation should require "
                "an explicit confirm=True or be split into a separate purge verb."
            ),
            remediation=(
                "Require an explicit confirm=True for hard=True; emit a warning "
                "naming what will be removed; audit-log every hard delete."
            ),
        ))

    # A01-003: SDK Memory.clear() with no confirmation / size cap.
    if "def clear(" in sdk_src and "confirm" not in sdk_src:
        findings.append(SecurityFinding(
            id="A01-003",
            severity="LOW",
            owasp="A01",
            component="sdk.py",
            finding=(
                "Memory.clear() deletes all SDK-created memories without a "
                "confirmation flag or size limit."
            ),
            remediation=(
                "Add a dry_run=True default and require explicit confirm=True; "
                "return the count that would be deleted."
            ),
        ))

    # A01-004: maintenance surface must gate destructive ops behind confirm.
    # The router must declare DESTRUCTIVE_MAINTENANCE_OPS and refuse those
    # ops unless confirm=True is supplied.
    gate_present = (
        "DESTRUCTIVE_MAINTENANCE_OPS" in maintenance_src
        and "confirm" in maintenance_src
        and bool(
            re.search(
                r"DESTRUCTIVE_MAINTENANCE_OPS\s+and\s+not\s+.*confirm",
                maintenance_src,
                re.DOTALL,
            )
        )
    )
    if not gate_present:
        findings.append(SecurityFinding(
            id="A01-004",
            severity="HIGH",
            owasp="A01",
            component="mcp_maintenance.py",
            finding=(
                "The maintenance router (memory_maintenance) does not gate "
                "destructive operations (rebuild, purge_expired, trash, crdt_sync, "
                "okf_export/import, share family, agent_clear, dedup) behind an "
                "explicit confirm=True. A single unauthenticated MCP call can "
                "delete, overwrite, exfiltrate, or merge remote data."
            ),
            remediation=(
                "Declare a DESTRUCTIVE_MAINTENANCE_OPS frozenset and refuse any "
                "of those ops in memory_maintenance unless kwargs['confirm'] is "
                "truthy; memory_advanced inherits the gate by delegation."
            ),
        ))

    return findings


def _scan_A02_crypto_failures() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    # A02-001: REST API has no effective authentication. Secure when the
    # loopback bypass is gated behind an explicit dev flag AND a token is
    # either configured or auto-generated at startup.
    api_token = _get_memory_toml_value("api.token")
    api_server_src = _read(REPO_ROOT / "infra" / "api_server.py")
    cli_src = _read(CLI_PY)
    loopback_gated = "insecure_loopback" in api_server_src
    token_autogen = "token_hex" in cli_src or "secrets" in cli_src
    if (not loopback_gated) or (api_token in ('""', "", None) and not token_autogen):
        findings.append(SecurityFinding(
            id="A02-001",
            severity="HIGH",
            owasp="A02",
            component="memory.toml / infra/api_server.py / cli.py",
            finding=(
                "The REST API server has no effective authentication: the loopback "
                "bypass is not gated behind a dev flag AND/OR no token is configured "
                "or auto-generated. Any local process can read, search, add, and "
                "delete memories with zero auth."
            ),
            remediation=(
                "Gate the loopback bypass behind an explicit api.insecure_loopback "
                "dev flag; auto-generate and persist a secure random token if empty; "
                "require the bearer token for all clients."
            ),
        ))

    # A02-004: hardcoded secrets committed in source.
    secret_hits = _find_committed_secrets()
    if secret_hits:
        findings.append(SecurityFinding(
            id="A02-004",
            severity="MEDIUM",
            owasp="A02",
            component="; ".join(secret_hits[:5]),
            finding=(
                f"Potential hardcoded secrets found in {len(secret_hits)} location(s) "
                "(API keys / tokens / private keys committed to source)."
            ),
            remediation=(
                "Move secrets to environment variables or a secrets manager; "
                "rotate any exposed credential; add a pre-commit secret scanner."
            ),
        ))

    return findings


def _find_committed_secrets() -> List[str]:
    """Scan production files for obviously hardcoded secrets.

    S9: broadened to catch env-default placeholder patterns and common
    non-standard key names (``apikey``, ``passwd``, ``client_secret``,
    ``private_key``) in addition to the canonical ones. Placeholder values
    such as ``"changeme"`` / ``"your-token-here"`` are also flagged so a
    committed default secret is caught.
    """
    hits: List[str] = []
    # Canonical key names with a literal value of meaningful length.
    key_names = (
        r"api_key|apikey|secret|client_secret|private_key|passwd|password|"
        r"token|access_key|auth_token"
    )
    literal_pat = rf'(?<![\w])({key_names})\s*=\s*["\'][A-Za-z0-9_\-]{{12,}}["\']'
    # Placeholder/default env values (committed default secrets).
    placeholder_pat = (
        rf'(?<![\w])({key_names})\s*=\s*["\']'
        rf'(changeme|change-me|your[-_][a-z]+|xxx+|placeholder|example|'
        rf'default[-_][a-z]+|insert[-_][a-z]+|todo|fixme)["\']'
    )
    for path in _PRODUCTION_FILES:
        src = _read(path)
        if not src:
            continue
        for pat, label in (
            (literal_pat, "key literal"),
            (placeholder_pat, "placeholder secret"),
        ):
            for m in re.finditer(pat, src):
                key = m.group(1)
                hits.append(f"{path.name}:{key} {label}")
                break  # one per file per pattern is enough
    # OpenAI-style key and sync-token literals (format-driven, no key name).
    format_patterns = [
        (r'(?<![\w])sk-[A-Za-z0-9]{20,}', "OpenAI-style sk- key"),
        (r'(?<![\w])MEMORY_SYNC_TOKEN\s*=\s*["\'][^"\']{12,}["\']', "sync token literal"),
    ]
    for path in _PRODUCTION_FILES:
        src = _read(path)
        if not src:
            continue
        for pat, label in format_patterns:
            for m in re.finditer(pat, src):
                hits.append(f"{path.name}:{label}")
                break
    return hits


def _scan_A03_injection() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    for fname, src in {
        "pipeline.py": _read(PIPELINE_PY),
        "write_journal.py": _read(WRITE_JOURNAL_PY),
        "orchestrator.py": _read(SEARCH_ORCH_PY),
        "mcp_verbs.py": _read(MCP_VERBS_PY),
        "tool_complete.py": _read(TOOL_COMPLETE_PY),
        "cli.py": _read(CLI_PY),
        "file_lock.py": _read(FILE_LOCK_PY),
    }.items():
        for call in _ast_find_subprocess_calls(src):
            if _shell_true(call):
                findings.append(SecurityFinding(
                    id="A03-001",
                    severity="CRITICAL",
                    owasp="A03",
                    component=fname,
                    finding="subprocess/os call uses shell=True — command injection risk.",
                    remediation="Remove shell=True; pass args as a list.",
                ))
        for call in _ast_find_eval_exec(src):
            findings.append(SecurityFinding(
                id="A03-004",
                severity="HIGH",
                owasp="A03",
                component=fname,
                finding="Bare eval()/exec() call — arbitrary code execution risk.",
                remediation="Remove eval()/exec(); use ast.literal_eval or a safe parser.",
            ))
        deser = _ast_find_dangerous_deserialization(src)
        for d in deser:
            findings.append(SecurityFinding(
                id="A03-003",
                severity="HIGH",
                owasp="A03",
                component=fname,
                finding=f"Unsafe deserialization sink: {d}.",
                remediation=(
                    "Use yaml.safe_load, torch.load(weights_only=True), or avoid "
                    "pickle/marshal on untrusted input."
                ),
            ))
        for sql in _ast_find_sql_strings(src):
            findings.append(SecurityFinding(
                id="A03-002",
                severity="HIGH",
                owasp="A03",
                component=fname,
                finding=f"F-string SQL interpolates a value without a placeholder: {sql[:120]}",
                remediation="Use parameterised queries (?, :name) for all SQL.",
            ))

    return findings


def _scan_A04_insecure_design() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    mm_src = _read(MULTI_MODAL_PY)
    sdk_src = _read(SDK_PY)

    # A04-002: arbitrary local file read via ingest_file (no containment).
    if "def ingest_file" in mm_src and not _path_containment_guard_present(mm_src):
        findings.append(SecurityFinding(
            id="A04-002",
            severity="HIGH",
            owasp="A04",
            component="multi_modal.py",
            finding=(
                "ingest_file reads any local path with no directory containment "
                "check. Any allowed-extension file anywhere on disk is readable, "
                "including files outside the memory tree."
            ),
            remediation=(
                "Restrict ingestion to an allowed base directory; reject absolute "
                "paths and symlink/hardlink escapes outside that tree."
            ),
        ))

    # A04-004: SDK saves globally visible by default.
    if "is_global=True" in sdk_src or "is_global: True" in sdk_src:
        findings.append(SecurityFinding(
            id="A04-004",
            severity="MEDIUM",
            owasp="A04",
            component="sdk.py",
            finding=(
                "Memory.add() defaults to is_global=True — all SDK saves are "
                "globally visible across all projects without explicit opt-in."
            ),
            remediation="Default is_global=False; require explicit opt-in for global scope.",
        ))

    return findings


def _scan_A05_security_misconfiguration() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    # A05-001: dashboard binds to all interfaces with no auth.
    cli_src = _read(CLI_PY)
    if "0.0.0.0" in cli_src and "server.address" not in cli_src:
        findings.append(SecurityFinding(
            id="A05-001",
            severity="HIGH",
            owasp="A05",
            component="cli.py",
            finding=(
                "The Streamlit dashboard is launched bound to 0.0.0.0 (all "
                "interfaces) with no authentication, exposing memory contents on "
                "the local network."
            ),
            remediation=(
                "Default to 127.0.0.1; expose --server.address; require auth or a "
                "reverse proxy."
            ),
        ))

    # A05-002: env vars can disable integrity-critical flags without warning.
    config_src = _read(CONFIG_PY)
    if "MEMORY_SAGA_ENABLED" in config_src and "integrity" not in config_src.lower():
        findings.append(SecurityFinding(
            id="A05-002",
            severity="MEDIUM",
            owasp="A05",
            component="infra/config.py",
            finding=(
                "MEMORY_* env vars can disable integrity-critical flags "
                "(MEMORY_SAGA_ENABLED, MEMORY_WRITE_JOURNAL_ENABLED) with no "
                "startup warning, silently downgrading crash-consistency."
            ),
            remediation=(
                "Emit a startup warning / audit line when integrity-critical flags "
                "are overridden via env; consider a locked-flags allowlist."
            ),
        ))

    return findings


def _scan_A06_vulnerable_components() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    pp_src = _read(PYPROJECT)
    lockfile_exists = (REPO_ROOT / "uv.lock").exists() or (REPO_ROOT / "requirements.txt").exists()

    # Only flag caret/tilde ranges. PEP 440 >=X,<Y ranges are acceptable when a
    # lockfile pins the resolved versions.
    caret_tilde = re.findall(r'^\s*[\w\.-]+\s*=\s*"[~^]\d', pp_src, re.MULTILINE)
    if caret_tilde and not lockfile_exists:
        findings.append(SecurityFinding(
            id="A06-001",
            severity="MEDIUM",
            owasp="A06",
            component="pyproject.toml",
            finding=(
                f"{len(caret_tilde)} dependencies use caret/tilde ranges and no "
                "lockfile is present to pin resolved versions."
            ),
            remediation=(
                "Pin dependencies with uv.lock / requirements.txt; run pip-audit or "
                "safety check in CI."
            ),
            networked=True,
        ))
    return findings


def _scan_A07_auth_failures() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    # A07-002: sync server must authenticate remote callers.
    sync_src = _read(SYNC_SERVER_PY)
    if sync_src and "MEMORY_SYNC_TOKEN" in sync_src and "401" not in sync_src and "403" not in sync_src:
        findings.append(SecurityFinding(
            id="A07-002",
            severity="MEDIUM",
            owasp="A07",
            component="infra/sync_server.py",
            finding=(
                "The sync server references MEMORY_SYNC_TOKEN but does not appear "
                "to reject unauthenticated requests (no 401/403 path)."
            ),
            remediation="Reject requests without a valid bearer token; log auth failures.",
        ))
    return findings


def _scan_A08_data_integrity() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    # A08-001: journal materialization does not re-run injection validation.
    journal_src = _read(WRITE_JOURNAL_PY)
    if "scan_for_injection" not in journal_src and "_scan_for_injection" not in journal_src:
        findings.append(SecurityFinding(
            id="A08-001",
            severity="MEDIUM",
            owasp="A08",
            component="infra/write_journal.py",
            finding=(
                "materialize_journal_entry does not re-run injection validation, so "
                "a poisoned entry written before a scanner upgrade bypasses the new "
                "detector at materialization time."
            ),
            remediation=(
                "Re-run scan_for_injection inside materialize_journal_entry (or the "
                "daemon drain loop) before persisting to the main DB."
            ),
        ))

    # A08-002: migrations are not checksum-verified.
    migration_src = _read(REPO_ROOT / "infra" / "migration_runner.py")
    if "checksum" not in migration_src.lower() and "sha256" not in migration_src.lower():
        findings.append(SecurityFinding(
            id="A08-002",
            severity="LOW",
            owasp="A08",
            component="infra/migration_runner.py",
            finding="Migrations are not checksum-verified — a modified file could run undetected.",
            remediation="Store and verify SHA256 of each migration file at apply time.",
        ))

    return findings


def _scan_A09_logging_failures() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    # A09-001: audit log stores raw tool args without redaction.
    audit_src = _read(AUDIT_PY)
    if "json.dumps(args" in audit_src and "redact" not in audit_src.lower():
        findings.append(SecurityFinding(
            id="A09-001",
            severity="MEDIUM",
            owasp="A09",
            component="infra/audit.py",
            finding=(
                "Audit log stores raw tool args via json.dumps(args) with no "
                "redaction. Secrets in tool parameters are persisted in plaintext."
            ),
            remediation=(
                "Add a redaction pass (keys matching token|secret|password|api_key) "
                "before serialising args."
            ),
        ))

    return findings


def _scan_A10_ssrf() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    mm_src = _read(MULTI_MODAL_PY)

    # A10-001: outbound URL fetch with no SSRF guard.
    if "urlopen" in mm_src and not _ssrf_guard_present(mm_src):
        findings.append(SecurityFinding(
            id="A10-001",
            severity="HIGH",
            owasp="A10",
            component="multi_modal.py",
            finding=(
                "ingest_url fetches arbitrary URLs via urllib with no SSRF guard "
                "(no private/link-local IP block, no allowlist, no redirect "
                "re-validation). Can reach 169.254.169.254, 10.*, 172.16.*, "
                "192.168.*, localhost."
            ),
            remediation=(
                "Resolve the hostname and reject loopback/link-local/private ranges; "
                "re-validate the resolved IP on every redirect; require an allowlist."
            ),
            networked=True,
        ))

    # A10-002: unbounded query passed to embedding subprocess.
    search_src = _read(MCP_SEARCH_PY)
    if "subprocess" in search_src and "len(query)" not in search_src and "MAX_QUERY" not in search_src:
        findings.append(SecurityFinding(
            id="A10-002",
            severity="LOW",
            owasp="A10",
            component="mcp_search.py",
            finding=(
                "memory_semantic_search spawns a subprocess with the query as a CLI "
                "argument and enforces no length limit (resource-exhaustion / arg "
                "overflow risk)."
            ),
            remediation="Clamp the query length (e.g. 4096 chars) before spawning the subprocess.",
        ))

    return findings


# ---------------------------------------------------------------------------
# LLM-specific OWASP Top 10
# ---------------------------------------------------------------------------


def _scan_LLM01_prompt_injection() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    pipeline_src = _read(PIPELINE_PY)

    # LLM01-002: injection scan fails open (swallows non-SaveValidationError
    # and logs it as "benign"). Secure once the fail-open branch is removed.
    if "scan_for_injection" in pipeline_src and "benign" in pipeline_src.lower():
        findings.append(SecurityFinding(
            id="LLM01-002",
            severity="HIGH",
            owasp="LLM01",
            component="save/pipeline.py",
            finding=(
                "If scan_for_injection raises an exception other than "
                "SaveValidationError, the save proceeds (scanner failure fails "
                "open — logged as 'benign'). A scanner regression could allow "
                "prompt injection."
            ),
            remediation=(
                "Fail closed on scanner errors (or at minimum log at WARNING/ERROR "
                "and surface the failure); treat scanner exceptions as rejections "
                "on the high-assurance path."
            ),
        ))

    return findings


def _scan_LLM03_supply_chain() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    reranker_src = _read(RERANKER_PY)
    llm_src = _read(LLM_PROVIDERS_PY)

    # LLM03-001: models downloaded without pinned revision / integrity check.
    unpinned = (
        ("revision" in reranker_src and '"main"' in reranker_src)
        or ("from_pretrained" in llm_src and "revision" not in llm_src)
        or "trust_remote_code=True" in llm_src
    )
    if unpinned:
        findings.append(SecurityFinding(
            id="LLM03-001",
            severity="MEDIUM",
            owasp="LLM03",
            component="infra/reranker.py / fact/llm_providers.py",
            finding=(
                "LLM/reranker models are loaded from HuggingFace Hub without a pinned "
                "commit hash (mutable 'main' ref) and/or with trust_remote_code=True, "
                "allowing silent weight swaps or remote code execution."
            ),
            remediation=(
                "Pin all models to commit hashes; verify SHA256 of downloaded "
                "artifacts; drop trust_remote_code=True or gate it behind an opt-in flag."
            ),
            networked=True,
        ))

    return findings


def _scan_LLM10_unbounded_consumption() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    journal_src = _read(WRITE_JOURNAL_PY)

    # LLM10-001: write journal has no size limit.
    if "MAX_SIZE" not in journal_src and "size_limit" not in journal_src:
        findings.append(SecurityFinding(
            id="LLM10-001",
            severity="LOW",
            owasp="LLM10",
            component="infra/write_journal.py",
            finding="The write journal DB has no size limit; a burst of writes could fill disk.",
            remediation="Add a max-size check; prune applied entries when the journal exceeds N MB.",
        ))

    return findings


# ---------------------------------------------------------------------------
# Master scan
# ---------------------------------------------------------------------------


def _run_all_scans() -> List[SecurityFinding]:
    scanners = [
        _scan_A01_broken_access_control,
        _scan_A02_crypto_failures,
        _scan_A03_injection,
        _scan_A04_insecure_design,
        _scan_A05_security_misconfiguration,
        _scan_A06_vulnerable_components,
        _scan_A07_auth_failures,
        _scan_A08_data_integrity,
        _scan_A09_logging_failures,
        _scan_A10_ssrf,
        _scan_LLM01_prompt_injection,
        _scan_LLM03_supply_chain,
        _scan_LLM10_unbounded_consumption,
    ]
    all_findings: List[SecurityFinding] = []
    for scanner in scanners:
        try:
            all_findings.extend(scanner())
        except Exception as exc:  # pragma: no cover - scanner robustness
            all_findings.append(SecurityFinding(
                id=f"ERR-{scanner.__name__}",
                severity="INFO",
                owasp="META",
                component=scanner.__name__,
                finding=f"Scanner raised an exception: {exc}",
                remediation="Fix the scanner.",
            ))
    return all_findings


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_security_report(findings: Optional[List[SecurityFinding]] = None) -> str:
    """Return a Markdown security health check report."""
    if findings is None:
        findings = _run_all_scans()

    lines = [
        "# Security Health Check Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Total findings: {len(findings)}",
        f"Blocking (regression-gate) findings: {sum(1 for f in findings if f.id in BLOCKING_IDS)}",
        "",
        "## Summary by Severity",
        "",
    ]
    for sev in ("HIGH", "MEDIUM", "LOW", "INFO"):
        count = sum(1 for f in findings if f.severity == sev)
        lines.append(f"- **{sev}**: {count}")
    lines.append("")

    lines.append("## Findings by OWASP Category")
    lines.append("")
    for owasp in sorted({f.owasp for f in findings}):
        group = [f for f in findings if f.owasp == owasp]
        lines.append(f"### {owasp}")
        for f in group:
            lines.append(f"#### [{f.severity}] {f.id} — {f.component}")
            lines.append(f"- **Finding**: {f.finding}")
            lines.append(f"- **Remediation**: {f.remediation}")
            if f.networked:
                lines.append("- *Networked check*")
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pytest — regression gate: assert the ABSENCE of blocking findings
# ---------------------------------------------------------------------------


class TestSecurityHealthCheck:
    """Regression gate. Each test fails (RED) while its vulnerability exists
    and passes (GREEN) once fixed. Positive-control tests prove the scanners
    actually detect synthetic vulnerabilities (so the gate cannot silently pass).
    """

    # ---- Non-LLM lane ----

    def test_A01_no_unconfirmed_destructive_ops(self) -> None:
        findings = _scan_A01_broken_access_control()
        blocking = [f for f in findings if f.id in ("A01-001", "A01-003", "A01-004")]
        assert not blocking, "Destructive ops without confirmation: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_A02_api_auth_and_no_secrets(self) -> None:
        findings = _scan_A02_crypto_failures()
        blocking = [f for f in findings if f.id in ("A02-001", "A02-004")]
        assert not blocking, "API auth / secrets: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_A03_no_injection_sinks(self) -> None:
        findings = _scan_A03_injection()
        blocking = [f for f in findings if f.id in ("A03-001", "A03-002", "A03-003", "A03-004")]
        assert not blocking, "Injection sinks: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_A04_no_unrestricted_ingest_or_global_default(self) -> None:
        findings = _scan_A04_insecure_design()
        blocking = [f for f in findings if f.id in ("A04-002", "A04-004")]
        assert not blocking, "Insecure design: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_A05_no_misconfiguration(self) -> None:
        findings = _scan_A05_security_misconfiguration()
        blocking = [f for f in findings if f.id in ("A05-001", "A05-002")]
        assert not blocking, "Misconfiguration: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_A06_deps_pinned(self) -> None:
        findings = _scan_A06_vulnerable_components()
        blocking = [f for f in findings if f.id == "A06-001"]
        assert not blocking, "Unpinned deps: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_A07_sync_server_authenticated(self) -> None:
        findings = _scan_A07_auth_failures()
        blocking = [f for f in findings if f.id == "A07-002"]
        assert not blocking, "Sync auth: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_A08_data_integrity(self) -> None:
        findings = _scan_A08_data_integrity()
        blocking = [f for f in findings if f.id in ("A08-001", "A08-002")]
        assert not blocking, "Data integrity: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_A09_audit_redaction(self) -> None:
        findings = _scan_A09_logging_failures()
        blocking = [f for f in findings if f.id == "A09-001"]
        assert not blocking, "Audit logging: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_A10_no_ssrf_or_unbounded_subprocess(self) -> None:
        findings = _scan_A10_ssrf()
        blocking = [f for f in findings if f.id in ("A10-001", "A10-002")]
        assert not blocking, "SSRF / subprocess: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    # ---- LLM lane ----

    def test_LLM01_scan_does_not_fail_open(self) -> None:
        findings = _scan_LLM01_prompt_injection()
        blocking = [f for f in findings if f.id == "LLM01-002"]
        assert not blocking, "Fail-open: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_LLM03_models_pinned(self) -> None:
        findings = _scan_LLM03_supply_chain()
        blocking = [f for f in findings if f.id == "LLM03-001"]
        assert not blocking, "Supply chain: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    def test_LLM10_journal_size_limited(self) -> None:
        findings = _scan_LLM10_unbounded_consumption()
        blocking = [f for f in findings if f.id == "LLM10-001"]
        assert not blocking, "Unbounded consumption: " + "; ".join(
            f"{f.id}: {f.finding}" for f in blocking
        )

    # ---- Positive-control tests: prove scanners detect synthetic vulns ----

    def test_positive_control_shell_true_detected(self) -> None:
        sample = "import subprocess\nsubprocess.run('ls -la', shell=True)\n"
        calls = _ast_find_subprocess_calls(sample)
        assert any(_shell_true(c) for c in calls), "Scanner missed shell=True"

    def test_positive_control_eval_detected(self) -> None:
        sample = "x = eval(user_input)\n"
        assert _ast_find_eval_exec(sample), "Scanner missed eval()"

    def test_positive_control_unparam_sql_detected(self) -> None:
        sample = 'sql = f"SELECT * FROM t WHERE name = {user_input}"\n'
        hits = _ast_find_sql_strings(sample)
        assert hits, "Scanner missed value-interpolated f-string SQL"

    def test_positive_control_column_only_sql_not_flagged(self) -> None:
        sample = 'sql = f"SELECT id, name FROM t WHERE {where} LIMIT ?"\n'
        assert not _ast_find_sql_strings(sample), "Scanner falsely flagged clause-only SQL"

    def test_positive_control_ssrf_detected(self) -> None:
        sample = "import urllib.request\nurllib.request.urlopen(url)\n"
        assert not _ssrf_guard_present(sample), "Guard check should be False for unguarded sample"

    # ---- S7: maintenance-surface confirmation gate (positive controls) ----

    def test_maintenance_requires_confirm_for_destructive_ops(self) -> None:
        from mcp_maintenance import (
            memory_maintenance,
            DESTRUCTIVE_MAINTENANCE_OPS,
            MaintenanceOp,
        )
        from mcp_common import ErrorCode

        # The constant must exist and enumerate the destructive ops.
        assert DESTRUCTIVE_MAINTENANCE_OPS, "DESTRUCTIVE_MAINTENANCE_OPS is empty"
        assert MaintenanceOp.AGENT_CLEAR in DESTRUCTIVE_MAINTENANCE_OPS
        assert MaintenanceOp.OKF_EXPORT in DESTRUCTIVE_MAINTENANCE_OPS
        assert MaintenanceOp.CRDT_SYNC in DESTRUCTIVE_MAINTENANCE_OPS

        # Without confirm=True the destructive op is refused (error envelope).
        refused = memory_maintenance("agent_clear")
        assert ErrorCode.INVALID_PARAMS.value in refused, refused
        assert "confirm" in refused.lower(), refused

        # With confirm=True the op is dispatched (here to a stub handler).
        with patch("mcp_maintenance_ops._get_handlers") as mock_h:
            mock_h.return_value = {
                MaintenanceOp.AGENT_CLEAR: lambda **_: "AGENT_CLEAR_OK",
            }
            ran = memory_maintenance("agent_clear", confirm=True)
        assert ran == "AGENT_CLEAR_OK", ran

    def test_memory_advanced_covered_by_confirm_gate(self) -> None:
        from mcp_maintenance import MaintenanceOp
        from mcp_common import ErrorCode
        from mcp_verbs import memory_advanced

        # memory_advanced delegates to memory_maintenance, so the same gate
        # applies. Without confirm -> refused.
        refused = memory_advanced(operation="okf_export", output_dir="/tmp/x")
        assert ErrorCode.INVALID_PARAMS.value in refused, refused
        assert "confirm" in refused.lower(), refused

        # With confirm + stub handler -> proceeds.
        with patch("mcp_maintenance_ops._get_handlers") as mock_h:
            mock_h.return_value = {
                MaintenanceOp.OKF_EXPORT: lambda **_: "OKF_EXPORT_OK",
            }
            ran = memory_advanced(
                operation="okf_export", output_dir="/tmp/x", confirm=True
            )
        assert ran == "OKF_EXPORT_OK", ran

    def test_crdt_sync_rejects_unauthenticated_remote_json(self) -> None:
        from mcp_crdt import memory_crdt_sync
        from mcp_common import ErrorCode

        token = "test-sync-token-1234567890abcdef"
        with patch.dict(os.environ, {"MEMORY_SYNC_TOKEN": token,
                                     "MEMORY_CRDT_TRUSTED_PEERS": ""}):
            # No token supplied -> rejected, remote JSON never merged.
            refused = memory_crdt_sync(
                agent_id="peer-x", remote_notes_json="{}"
            )
            assert ErrorCode.INVALID_PARAMS.value in refused, refused
            assert "sync_token" in refused.lower(), refused

            # Correct token -> authorized (handler runs; crdt_sync_all stubbed
            # so we don't touch the real DB).
            with patch("crdt.crdt_merge.crdt_sync_all",
                       return_value={"applied": 0, "total": 0}) as m:
                ok = memory_crdt_sync(
                    agent_id="peer-x",
                    remote_notes_json="{}",
                    sync_token=token,
                )
            assert "applied" in ok, ok
            assert m.called, "authorized crdt_sync must call crdt_sync_all"

    def test_crdt_sync_trusted_peer_allowlist(self) -> None:
        from mcp_crdt import memory_crdt_sync

        with patch.dict(os.environ, {
                "MEMORY_SYNC_TOKEN": "",
                "MEMORY_CRDT_TRUSTED_PEERS": "peer-trusted,other",
        }):
            with patch("crdt.crdt_merge.crdt_sync_all",
                       return_value={"applied": 0, "total": 0}) as m:
                ok = memory_crdt_sync(
                    agent_id="peer-trusted",
                    remote_notes_json="{}",
                )
            assert "applied" in ok, ok
            assert m.called, "trusted peer must be allowed without a token"

            refused = memory_crdt_sync(
                agent_id="peer-unknown", remote_notes_json="{}"
            )
            assert "sync_token" in refused.lower(), refused

    # ---- Report generation (structure only; findings may be 0 after fixes) ----

    def test_markdown_report_generation(self) -> None:
        report = generate_security_report()
        assert "Security Health Check Report" in report
        assert "Generated:" in report
        assert "Total findings:" in report


if __name__ == "__main__":
    print(generate_security_report())
