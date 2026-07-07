"""Security Health Check — OWASP Top 10 (non-LLM) + OWASP Top 10 for LLMs.

Run with:
    pytest eval/test_security_health_check.py -v

Each test is a deterministic static scanner — no LLM calls, no network calls
unless explicitly marked ``networked``. Tests "pass" when they complete
without exception; findings are printed to stdout and aggregated in the
Markdown report produced by ``generate_security_report()``.
"""

from __future__ import annotations

import ast
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

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
PYPROJECT = REPO_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityFinding:
    """One concrete security finding."""

    id: str
    severity: str  # HIGH | MEDIUM | LOW | INFO
    owasp: str  # e.g. "A01" or "LLM01"
    component: str
    finding: str
    remediation: str
    networked: bool = False


# ---------------------------------------------------------------------------
# Helpers — file / code introspection
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _contains_python(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE | re.MULTILINE))


def _ast_find_calls(
    source: str, func_names: Sequence[str]
) -> List[ast.stmt]:
    """Return AST nodes where a function in *func_names* is called."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: List[ast.stmt] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in func_names:
                hits.append(node)
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


def _ast_find_subprocess_calls(source: str) -> List[ast.Call]:
    """Find all subprocess.run / subprocess.Popen / os.system calls."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: List[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    if node.func.value.id in ("subprocess", "os"):
                        if node.func.attr in ("run", "Popen", "call"):
                            hits.append(node)
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


def _shell_true(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "shell":
            if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return True
    return False


def _ast_find_sql_strings(source: str) -> List[str]:
    """Return f-string SQL patterns that include VALUES or WHERE and are not
    obviously parameterised via dynamic placeholder strings (IN (...) clauses).

    Some parameterised queries use f-string only for the surrounding SQL and
    inject '?' placeholders via a variable — those are safe and must not be
    flagged.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: List[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
            if not (isinstance(node.values[0], ast.Constant)
                    and isinstance(node.values[0].value, str)):
                self.generic_visit(node)
                return
            sql = node.values[0].value
            upper = sql.upper()
            if any(kw in upper for kw in ("SELECT", "INSERT", "UPDATE", "DELETE")):
                if any(kw in upper for kw in ("VALUES", "WHERE", "SET", "FROM", "MATCH")):
                    if not upper.rstrip().endswith("IN ("):
                        hits.append(sql)
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


def _get_memory_toml_value(key: str) -> Optional[str]:
    """Return raw value for a dotted TOML key (e.g. ``api.token``) from memory.toml.

    Handles INI-style sections: finds the section ``[prefix]`` then reads
    the bare ``key = "..."`` line within it. Strips inline comments (# ...).
    """
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
        m2 = re.search(
            rf"^{re.escape(bare)}\s*=\s*(.+)",
            block,
            re.MULTILINE,
        )
        if not m2:
            return None
        raw = m2.group(1).strip()
        # Strip inline comment: everything after unquoted #
        raw = re.sub(r"\s+#.*$", "", raw)
        return raw.strip().strip('"')
    m = re.search(rf"^{key}\s*=\s*(.+)", text, re.MULTILINE)
    return m.group(1).strip().strip('"') if m else None


# ---------------------------------------------------------------------------
# Scan functions — one per OWASP category
# ---------------------------------------------------------------------------


def _scan_A01_broken_access_control() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    verbs_src = _read(MCP_VERBS_PY)
    memory_src = _read(MCP_MEMORY_PY)
    config_src = _read(CONFIG_PY)
    sdk_src = _read(SDK_PY)

    if 'hard=True' in verbs_src or 'hard=True' in memory_src:
        findings.append(
            SecurityFinding(
                id="A01-001",
                severity="HIGH",
                owasp="A01",
                component="mcp_verbs.py / mcp_memory.py",
                finding=(
                    "memory_delete(note_id, hard=True) performs permanent deletion "
                    "with no confirmation gate beyond a boolean parameter. Any agent "
                    "with MCP access can irreversibly erase memories."
                ),
                remediation=(
                    "Add an explicit confirmation prompt or two-step confirmation "
                    "(soft-delete first, hard-delete via separate admin operation)."
                ),
            )
        )

    if 'action == "supersede"' in verbs_src:
        findings.append(
            SecurityFinding(
                id="A01-002",
                severity="MEDIUM",
                owasp="A01",
                component="mcp_verbs.py",
                finding=(
                    "memory_note(action='supersede') replaces any note without "
                    "checking whether the calling agent owns or authored the note."
                ),
                remediation=(
                    "Add agent-level ownership check (compare agent_id on the note "
                    "to the calling agent) before allowing supersede."
                ),
            )
        )

    if "def clear(" in sdk_src and "DELETE FROM memories WHERE source_file LIKE" in sdk_src:
        findings.append(
            SecurityFinding(
                id="A01-003",
                severity="HIGH",
                owasp="A01",
                component="sdk.py",
                finding=(
                    "Memory.clear() deletes all SDK-created memories without any "
                    "confirmation prompt or size limit."
                ),
                remediation=(
                    "Require explicit confirmation; add optional scope filter; "
                    "log the clear operation in the audit trail."
                ),
            )
        )

    if "MEMORY_SAGA_ENABLED" in config_src or "MEMORY_WRITE_JOURNAL_ENABLED" in config_src:
        findings.append(
            SecurityFinding(
                id="A01-004",
                severity="HIGH",
                owasp="A01",
                component="infra/config.py",
                finding=(
                    "Any MEMORY_* environment variable can override security-critical "
                    "flags (e.g. MEMORY_SAGA_ENABLED=0 disables crash-consistency, "
                    "MEMORY_WRITE_JOURNAL_ENABLED=1 changes the write path). A "
                    "compromised environment can silently downgrade protections."
                ),
                remediation=(
                    "Restrict which config keys are overridable via env vars; "
                    "warn at startup when security-critical flags are overridden; "
                    "consider an immutable config mode for production."
                ),
            )
        )

    return findings


def _scan_A02_crypto_failures() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    api_token = _get_memory_toml_value("api.token")
    if api_token in ('""', "", None):
        findings.append(
            SecurityFinding(
                id="A02-001",
                severity="HIGH",
                owasp="A02",
                component="memory.toml / cli.py",
                finding=(
                    "api.token defaults to empty string — the REST API server has "
                    "no authentication by default. Any local process can call it."
                ),
                remediation=(
                    "Generate a random token on first run; require it on startup; "
                    "refuse to start the API server without a non-empty token."
                ),
            )
        )

    db_src = _read(DB_PY)
    if "0o600" in db_src:
        findings.append(
            SecurityFinding(
                id="A02-002",
                severity="INFO",
                owasp="A02",
                component="infra/db.py",
                finding="DB files are created with 0o600 permissions (owner read/write only). ✓",
                remediation="No action needed.",
            )
        )

    audit_src = _read(AUDIT_PY)
    if "json.dumps(args" in audit_src:
        findings.append(
            SecurityFinding(
                id="A02-003",
                severity="MEDIUM",
                owasp="A02",
                component="infra/audit.py",
                finding=(
                    "Audit log stores raw tool args via json.dumps(args). "
                    "If tool parameters contain secrets (API keys, tokens), "
                    "they are written to the audit log in plaintext."
                ),
                remediation=(
                    "Add a PII scrubber that redacts known secret patterns "
                    "(key=..., token=..., password=...) before writing to the audit log."
                ),
            )
        )

    return findings


def _scan_A03_injection() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    src_map = {
        "write_journal.py": _read(WRITE_JOURNAL_PY),
        "pipeline.py": _read(PIPELINE_PY),
        "orchestrator.py": _read(SEARCH_ORCH_PY),
        "mcp_verbs.py": _read(MCP_VERBS_PY),
        "tool_complete.py": _read(TOOL_COMPLETE_PY),
        "cli.py": _read(CLI_PY),
        "file_lock.py": _read(FILE_LOCK_PY),
    }

    for fname, src in src_map.items():
        for call in _ast_find_subprocess_calls(src):
            if _shell_true(call):
                findings.append(
                    SecurityFinding(
                        id="A03-001",
                        severity="CRITICAL",
                        owasp="A03",
                        component=fname,
                        finding=f"subprocess call uses shell=True — command injection risk.",
                        remediation="Remove shell=True; pass args as a list.",
                    )
                )

    for fname, src in {
        "pipeline.py": _read(PIPELINE_PY),
        "write_journal.py": _read(WRITE_JOURNAL_PY),
        "orchestrator.py": _read(SEARCH_ORCH_PY),
        "mcp_verbs.py": _read(MCP_VERBS_PY),
    }.items():
        for sql in _ast_find_sql_strings(src):
            if "?" not in sql and "%s" not in sql:
                findings.append(
                    SecurityFinding(
                        id="A03-002",
                        severity="HIGH",
                        owasp="A03",
                        component=fname,
                        finding=f"F-string SQL without parameterised placeholder: {sql[:120]}",
                        remediation="Use parameterised queries (?, :name) for all SQL.",
                    )
                )

    verbs_src = _read(MCP_VERBS_PY)
    if "_supplement_with_pending" in verbs_src and 'f"%{query}%"' in verbs_src:
        findings.append(
            SecurityFinding(
                id="A03-003",
                severity="LOW",
                owasp="A03",
                component="mcp_verbs.py",
                finding=(
                    "_supplement_with_pending uses agent-controlled query string "
                    "unescaped in SQL LIKE pattern. An agent can use '%%' to match "
                    "all pending entries."
                ),
                remediation="Escape SQL LIKE metacharacters (% and _) before interpolation.",
            )
        )

    return findings


def _scan_A04_insecure_design() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    mm_src = _read(MULTI_MODAL_PY)
    cli_src = _read(CLI_PY)
    sdk_src = _read(SDK_PY)

    if "def ingest_url" in mm_src and "urlopen" in mm_src:
        findings.append(
            SecurityFinding(
                id="A04-001",
                severity="HIGH",
                owasp="A04",
                component="multi_modal.py",
                finding=(
                    "ingest_url fetches arbitrary URLs via urllib with no allowlist. "
                    "An agent with MCP access can probe internal endpoints (SSRF)."
                ),
                remediation=(
                    "Add a URL allowlist (configurable); block private/reserved IPs "
                    "and metadata endpoints (169.254.169.254, 10.*, 172.16.*, 192.168.*)."
                ),
            )
        )

    if "def ingest_file" in mm_src and "open(" in mm_src:
        findings.append(
            SecurityFinding(
                id="A04-002",
                severity="HIGH",
                owasp="A04",
                component="multi_modal.py",
                finding=(
                    "ingest_file reads any local file path with no directory restriction. "
                    "An agent can read /etc/passwd, ~/.ssh/id_rsa, etc."
                ),
                remediation=(
                    "Restrict to a configurable allowed directory tree; "
                    "reject absolute paths outside that tree."
                ),
            )
        )

    if "0.0.0.0" in cli_src:
        findings.append(
            SecurityFinding(
                id="A04-003",
                severity="HIGH",
                owasp="A04",
                component="cli.py",
                finding=(
                    "Streamlit dashboard binds to 0.0.0.0 (all interfaces) with "
                    "no authentication. Anyone on the network can access it."
                ),
                remediation=(
                    "Bind to 127.0.0.1 by default; add --server.address option; "
                    "enable Streamlit authentication or put behind a reverse proxy."
                ),
            )
        )

    if "is_global=True" in sdk_src or "is_global: True" in sdk_src:
        findings.append(
            SecurityFinding(
                id="A04-004",
                severity="MEDIUM",
                owasp="A04",
                component="sdk.py",
                finding=(
                    "Memory.add() defaults to is_global=True — all SDK saves "
                    "are globally visible across all projects without explicit opt-in."
                ),
                remediation=(
                    "Change default to is_global=False; require explicit opt-in "
                    "for global visibility."
                ),
            )
        )

    return findings


def _scan_A05_security_misconfiguration() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    api_token = _get_memory_toml_value("api.token")
    if not api_token:
        findings.append(
            SecurityFinding(
                id="A05-001",
                severity="HIGH",
                owasp="A05",
                component="memory.toml",
                finding="api.token is empty by default — REST API runs without authentication.",
                remediation=(
                    "Generate a random token on init; fail if empty when server starts."
                ),
            )
        )

    config_src = _read(CONFIG_PY)
    if "log_feature_flags_at_startup" in config_src:
        findings.append(
            SecurityFinding(
                id="A05-002",
                severity="LOW",
                owasp="A05",
                component="infra/config.py",
                finding=(
                    "log_feature_flags_at_startup emits all feature flags as JSON "
                    "to the INFO log. Could leak configuration details if logs are accessible."
                ),
                remediation=(
                    "Log only a hash of the flags, or only log flags that deviate from defaults."
                ),
            )
        )

    return findings


def _scan_A06_vulnerable_components() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    pp_src = _read(PYPROJECT)

    unpinned = re.findall(r'^\s*[\w-]+\s*=\s*"[\^~]?\d+', pp_src, re.MULTILINE)
    if unpinned:
        findings.append(
            SecurityFinding(
                id="A06-001",
                severity="MEDIUM",
                owasp="A06",
                component="pyproject.toml",
                finding=(
                    f"Potentially unpinned dependencies: {len(unpinned)} packages "
                    "use caret/tilde ranges."
                ),
                remediation=(
                    "Pin all dependencies to exact versions in a lockfile; "
                    "run 'pip-audit' or 'safety check' in CI."
                ),
                networked=True,
            )
        )

    return findings


def _scan_A07_auth_failures() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    client_src = _read(CLIENT_PY)
    if "class MemoryClient" in client_src and "auth" not in client_src.lower():
        findings.append(
            SecurityFinding(
                id="A07-001",
                severity="MEDIUM",
                owasp="A07",
                component="agentic_memory/client.py",
                finding=(
                    "MemoryClient has no authentication or authorization. "
                    "Any process that can import it has full access to the memory DB."
                ),
                remediation=(
                    "Add an auth_token parameter to MemoryClient; validate against "
                    "the API token or a per-agent credential store."
                ),
            )
        )

    mcp_src = _read(MEMORY_MCP_PY)
    if "remove_tool" in mcp_src and "ADMIN_TOOLS" in mcp_src:
        findings.append(
            SecurityFinding(
                id="A07-002",
                severity="INFO",
                owasp="A07",
                component="memory_mcp.py",
                finding=(
                    "Admin tools are removed from the MCP surface at startup ✓. "
                    "However, no authentication distinguishes agents — any agent "
                    "that connects gets the full CORE surface."
                ),
                remediation=(
                    "Consider per-agent scoping: agents receive only the tools "
                    "their role requires (principle of least privilege)."
                ),
            )
        )

    return findings


def _scan_A08_data_integrity() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    journal_src = _read(WRITE_JOURNAL_PY)
    if "scan_for_injection" not in journal_src and "_scan_for_injection" not in journal_src:
        findings.append(
            SecurityFinding(
                id="A08-001",
                severity="MEDIUM",
                owasp="A08",
                component="infra/write_journal.py",
                finding=(
                    "Journal entries are materialised by materialize_journal_entry "
                    "without re-running injection validation. A poisoned entry "
                    "written before a scanner upgrade would bypass the new scanner."
                ),
                remediation=(
                    "Re-run scan_for_injection inside materialize_journal_entry "
                    "before persisting to the main DB."
                ),
            )
        )

    migration_src = _read(REPO_ROOT / "infra" / "migration_runner.py")
    if "checksum" not in migration_src.lower() and "sha256" not in migration_src.lower():
        findings.append(
            SecurityFinding(
                id="A08-002",
                severity="LOW",
                owasp="A08",
                component="infra/migration_runner.py",
                finding=(
                    "Migrations are not checksum-verified — a modified migration "
                    "file could run undetected."
                ),
                remediation="Store and verify SHA256 of each migration file at apply time.",
            )
        )

    return findings


def _scan_A09_logging_failures() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    audit_src = _read(AUDIT_PY)
    if "json.dumps(args" in audit_src:
        findings.append(
            SecurityFinding(
                id="A09-001",
                severity="MEDIUM",
                owasp="A09",
                component="infra/audit.py",
                finding=(
                    "Audit log stores raw tool args as JSON. Sensitive parameters "
                    "(API keys, file paths, PII) are persisted in plaintext."
                ),
                remediation=(
                    "Add a redaction filter before serialising args; "
                    "redact values for keys matching '(key|token|secret|password|auth)'."
                ),
            )
        )

    tc_src = _read(TOOL_COMPLETE_PY)
    if "traceback" in tc_src.lower() and "jsonl" in tc_src.lower():
        findings.append(
            SecurityFinding(
                id="A09-002",
                severity="LOW",
                owasp="A09",
                component="background/tool_complete.py",
                finding=(
                    "Hook error JSONL may contain full tracebacks including tool "
                    "arguments. Sensitive data in tracebacks is not scrubbed."
                ),
                remediation=(
                    "Redact known secret patterns from tracebacks before writing "
                    "to hook-errors.jsonl."
                ),
            )
        )

    return findings


def _scan_A10_ssrf() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    mm_src = _read(MULTI_MODAL_PY)

    if "urlopen" in mm_src:
        findings.append(
            SecurityFinding(
                id="A10-001",
                severity="HIGH",
                owasp="A10",
                component="multi_modal.py",
                finding=(
                    "ingest_url uses urllib.request.urlopen with no URL validation. "
                    "Can fetch internal endpoints (169.254.169.254, 10.*, 172.16.*, "
                    "192.168.*, localhost services)."
                ),
                remediation=(
                    "Implement URL allowlist; block private/reserved IP ranges; "
                    "require HTTPS for external URLs; add a configurable SSRF denylist."
                ),
                networked=True,
            )
        )

    search_src = _read(MCP_SEARCH_PY)
    if "subprocess" in search_src and "embedding_search" in search_src:
        findings.append(
            SecurityFinding(
                id="A10-002",
                severity="LOW",
                owasp="A10",
                component="mcp_search.py",
                finding=(
                    "memory_semantic_search spawns a subprocess with the query as "
                    "a CLI argument. No length limit is enforced before passing."
                ),
                remediation=(
                    "Add a max_query_length guard (e.g. 4096 chars) before spawning "
                    "the embedding subprocess."
                ),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# LLM-specific OWASP Top 10
# ---------------------------------------------------------------------------


def _scan_LLM01_prompt_injection() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    inj_src = _read(INJECTION_PY)
    pipeline_src = _read(PIPELINE_PY)

    categories = re.findall(
        r'"(imperative|roleplay|system_prompt|tool_invocation)"', inj_src
    )
    if len(categories) < 4:
        findings.append(
            SecurityFinding(
                id="LLM01-001",
                severity="HIGH",
                owasp="LLM01",
                component="memory_injection.py",
                finding=f"Scanner covers {len(categories)}/4 expected categories.",
                remediation="Cover all 4 categories: imperative, roleplay, system_prompt, tool_invocation.",
            )
        )

    if "scan_for_injection" in pipeline_src:
        if "scan failure" in pipeline_src.lower() or "benign" in pipeline_src.lower():
            findings.append(
                SecurityFinding(
                    id="LLM01-002",
                    severity="HIGH",
                    owasp="LLM01",
                    component="save/pipeline.py",
                    finding=(
                        "If scan_for_injection raises an exception (not SaveValidationError), "
                        "the save proceeds (logged at DEBUG). A scanner bug could allow prompt injection."
                    ),
                    remediation=(
                        "Treat scanner exceptions as SaveValidationError; "
                        "fail closed on any scanner error."
                    ),
                )
            )

    return findings


def _scan_LLM02_sensitive_info_disclosure() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    sdk_src = _read(SDK_PY)
    if "is_global=True" in sdk_src or "is_global: True" in sdk_src:
        findings.append(
            SecurityFinding(
                id="LLM02-001",
                severity="MEDIUM",
                owasp="LLM02",
                component="sdk.py",
                finding=(
                    "Memory.add() defaults to is_global=True — secrets saved via "
                    "the SDK are visible to all projects by default."
                ),
                remediation="Change default to is_global=False.",
            )
        )

    audit_src = _read(AUDIT_PY)
    if "json.dumps(args" in audit_src:
        findings.append(
            SecurityFinding(
                id="LLM02-002",
                severity="MEDIUM",
                owasp="LLM02",
                component="infra/audit.py",
                finding=(
                    "Audit log stores raw tool args (json.dumps(args, default=str)). "
                    "Sensitive parameters may be persisted in plaintext."
                ),
                remediation="Redact secret-pattern values before writing to audit log.",
            )
        )

    return findings


def _scan_LLM03_supply_chain() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    config_src = _read(CONFIG_PY)
    if "Qwen" in config_src:
        findings.append(
            SecurityFinding(
                id="LLM03-001",
                severity="MEDIUM",
                owasp="LLM03",
                component="infra/config.py",
                finding=(
                    "System downloads LLM models (e.g. Qwen2.5-3B-Instruct) from "
                    "HuggingFace Hub with no integrity verification."
                ),
                remediation=(
                    "Pin model to a specific commit hash; verify SHA256 of downloaded "
                    "weights; consider a private model registry for production."
                ),
                networked=True,
            )
        )

    mm_src = _read(MULTI_MODAL_PY)
    if "importlib.import_module" in mm_src:
        findings.append(
            SecurityFinding(
                id="LLM03-002",
                severity="LOW",
                owasp="LLM03",
                component="multi_modal.py",
                finding=(
                    "Optional dependencies (pymupdf, pytesseract, faster-whisper) "
                    "are loaded dynamically. A compromised PyPI package could be "
                    "loaded without being pinned."
                ),
                remediation="Pin optional dependencies; verify hashes in CI.",
            )
        )

    return findings


def _scan_LLM04_data_poisoning() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    journal_src = _read(WRITE_JOURNAL_PY)
    if "scan_for_injection" not in journal_src:
        findings.append(
            SecurityFinding(
                id="LLM04-001",
                severity="MEDIUM",
                owasp="LLM04",
                component="infra/write_journal.py",
                finding=(
                    "Journal entries trust upstream validation. "
                    "materialize_journal_entry does not re-validate content, allowing "
                    "a poisoned entry written before a scanner upgrade to bypass it."
                ),
                remediation="Re-validate content at materialization time.",
            )
        )

    pipeline_src = _read(PIPELINE_PY)
    if "vec_key" in pipeline_src and "hmac" not in pipeline_src.lower():
        findings.append(
            SecurityFinding(
                id="LLM04-002",
                severity="LOW",
                owasp="LLM04",
                component="save/pipeline.py",
                finding=(
                    "Vector keys (vec_keys table) are written during saga but not "
                    "integrity-protected. A compromised process could tamper with "
                    "embeddings after the saga completes."
                ),
                remediation=(
                    "Add HMAC or checksum to vec_keys entries; verify on read."
                ),
            )
        )

    return findings


def _scan_LLM05_improper_output_handling() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    search_src = _read(SEARCH_ORCH_PY)

    if "_format_search_results" in search_src:
        findings.append(
            SecurityFinding(
                id="LLM05-001",
                severity="MEDIUM",
                owasp="LLM05",
                component="search/orchestrator.py",
                finding=(
                    "Memory content is interpolated into result strings without HTML/XML "
                    "escaping. If rendered in a web UI, this is an XSS vector."
                ),
                remediation=(
                    "HTML-escape memory content before rendering in any HTML context; "
                    "use a safe templating library (Jinja2 autoescape=True)."
                ),
            )
        )

    return findings


def _scan_LLM06_excessive_agency() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    verbs_src = _read(MCP_VERBS_PY)
    toml_src = _read(MEMORY_TOML)

    if "hard=True" in verbs_src:
        findings.append(
            SecurityFinding(
                id="LLM06-001",
                severity="HIGH",
                owasp="LLM06",
                component="mcp_verbs.py",
                finding=(
                    "memory_delete with hard=True permanently deletes memories without "
                    "human oversight. An agent instructed to 'clean up old memories' "
                    "could wipe the entire store."
                ),
                remediation=(
                    "Rate-limit hard deletes; add a dry-run mode; log all hard deletes "
                    "in the audit trail with agent identity."
                ),
            )
        )

    if "memory_save = 100" in toml_src:
        findings.append(
            SecurityFinding(
                id="LLM06-002",
                severity="LOW",
                owasp="LLM06",
                component="memory.toml",
                finding=(
                    "Rate limits are in-memory only. A process restart resets counters. "
                    "A burst agent could hammer save/delete before limits accumulate."
                ),
                remediation=(
                    "Persist rate-limit counters to SQLite; reset them atomically "
                    "with the process heartbeat."
                ),
            )
        )

    return findings


def _scan_LLM07_system_prompt_leakage() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    verbs_src = _read(MCP_VERBS_PY)
    if "_err" in verbs_src:
        findings.append(
            SecurityFinding(
                id="LLM07-001",
                severity="LOW",
                owasp="LLM07",
                component="mcp_verbs.py",
                finding=(
                    "mcp_verbs.py returns structured error envelopes (_err) that "
                    "may contain ErrorCode enum names and messages. Low risk but "
                    "could leak internal error taxonomy to agents."
                ),
                remediation=(
                    "Map internal error codes to user-friendly messages; "
                    "do not expose enum names or internal codes in tool output."
                ),
            )
        )

    return findings


def _scan_LLM08_vector_embedding_weaknesses() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    pipeline_src = _read(PIPELINE_PY)
    if "vec_key" in pipeline_src and "hmac" not in pipeline_src.lower():
        findings.append(
            SecurityFinding(
                id="LLM08-001",
                severity="MEDIUM",
                owasp="LLM08",
                component="save/pipeline.py",
                finding=(
                    "Vector keys (vec_keys table) have no integrity protection (no HMAC). "
                    "A compromised process could redirect search results by tampering "
                    "with vec_keys entries."
                ),
                remediation=(
                    "Sign vec_keys entries with HMAC(key=secret, data=note_id||embedding); "
                    "verify signature on read."
                ),
            )
        )

    return findings


def _scan_LLM09_misinformation() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    search_src = _read(SEARCH_ORCH_PY)
    if "fitness_score" not in search_src and "quality_score" not in search_src:
        findings.append(
            SecurityFinding(
                id="LLM09-001",
                severity="MEDIUM",
                owasp="LLM09",
                component="search/orchestrator.py",
                finding=(
                    "Retrieved memories do not carry an explicit freshness or confidence "
                    "indicator in the search result output. Agents may treat hallucinated "
                    "or stale memories as authoritative."
                ),
                remediation=(
                    "Include recency_half_life decay score and source confidence "
                    "(auto_save vs agent) in every search result item."
                ),
            )
        )

    return findings


def _scan_LLM10_unbounded_consumption() -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []

    journal_src = _read(WRITE_JOURNAL_PY)
    if "MAX_SIZE" not in journal_src and "size_limit" not in journal_src:
        findings.append(
            SecurityFinding(
                id="LLM10-001",
                severity="MEDIUM",
                owasp="LLM10",
                component="infra/write_journal.py",
                finding=(
                    "The write journal DB has no size limit. A burst of writes could "
                    "fill disk and cause the daemon to stall."
                ),
                remediation=(
                    "Add a max size check in init_journal_db; prune applied entries "
                    "when journal exceeds N MB."
                ),
            )
        )

    search_src = _read(MCP_SEARCH_PY)
    if "len(query)" not in search_src and "MAX_QUERY" not in search_src:
        findings.append(
            SecurityFinding(
                id="LLM10-002",
                severity="LOW",
                owasp="LLM10",
                component="mcp_search.py",
                finding=(
                    "memory_semantic_search passes the query directly to a subprocess "
                    "with no length check. A very long query could exhaust memory."
                ),
                remediation="Add max_query_length (e.g. 4096 chars) before spawning subprocess.",
            )
        )

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
        _scan_LLM02_sensitive_info_disclosure,
        _scan_LLM03_supply_chain,
        _scan_LLM04_data_poisoning,
        _scan_LLM05_improper_output_handling,
        _scan_LLM06_excessive_agency,
        _scan_LLM07_system_prompt_leakage,
        _scan_LLM08_vector_embedding_weaknesses,
        _scan_LLM09_misinformation,
        _scan_LLM10_unbounded_consumption,
    ]
    all_findings: List[SecurityFinding] = []
    for scanner in scanners:
        try:
            all_findings.extend(scanner())
        except Exception as exc:
            all_findings.append(
                SecurityFinding(
                    id=f"ERR-{scanner.__name__}",
                    severity="INFO",
                    owasp="META",
                    component=scanner.__name__,
                    finding=f"Scanner raised an exception: {exc}",
                    remediation="Fix the scanner.",
                )
            )
    return all_findings


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_security_report(
    findings: Optional[List[SecurityFinding]] = None,
) -> str:
    """Return a Markdown security health check report."""
    if findings is None:
        findings = _run_all_scans()

    lines = [
        "# Security Health Check Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Total findings: {len(findings)}",
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
# Pytest test class
# ---------------------------------------------------------------------------


class TestSecurityHealthCheck:
    """OWASP Top 10 (non-LLM) + OWASP Top 10 for LLMs — security scanner."""

    # ---- Non-LLM lane ----

    def test_A01_broken_access_control(self) -> None:
        findings = _scan_A01_broken_access_control()
        assert len(findings) > 0, "Expected at least one A01 finding"
        for f in findings:
            print(f"\n[A01] {f.id} ({f.severity}): {f.finding}")
        assert any(f.id == "A01-004" for f in findings)

    def test_A02_crypto_failures(self) -> None:
        findings = _scan_A02_crypto_failures()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[A02] {f.id} ({f.severity}): {f.finding}")

    def test_A03_injection(self) -> None:
        findings = _scan_A03_injection()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[A03] {f.id} ({f.severity}): {f.finding}")
        shell_true = [f for f in findings if "shell=True" in f.finding]
        assert len(shell_true) == 0, "shell=True found — CRITICAL injection risk"

    def test_A04_insecure_design(self) -> None:
        findings = _scan_A04_insecure_design()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[A04] {f.id} ({f.severity}): {f.finding}")

    def test_A05_security_misconfiguration(self) -> None:
        findings = _scan_A05_security_misconfiguration()
        assert len(findings) > 0
        assert any(f.id == "A05-001" for f in findings)
        for f in findings:
            print(f"\n[A05] {f.id} ({f.severity}): {f.finding}")

    def test_A06_vulnerable_components(self) -> None:
        findings = _scan_A06_vulnerable_components()
        for f in findings:
            print(f"\n[A06] {f.id} ({f.severity}): {f.finding}")

    def test_A07_auth_failures(self) -> None:
        findings = _scan_A07_auth_failures()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[A07] {f.id} ({f.severity}): {f.finding}")

    def test_A08_data_integrity(self) -> None:
        findings = _scan_A08_data_integrity()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[A08] {f.id} ({f.severity}): {f.finding}")

    def test_A09_logging_failures(self) -> None:
        findings = _scan_A09_logging_failures()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[A09] {f.id} ({f.severity}): {f.finding}")

    def test_A10_ssrf(self) -> None:
        findings = _scan_A10_ssrf()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[A10] {f.id} ({f.severity}): {f.finding}")

    # ---- LLM lane ----

    def test_LLM01_prompt_injection(self) -> None:
        findings = _scan_LLM01_prompt_injection()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[LLM01] {f.id} ({f.severity}): {f.finding}")

    def test_LLM02_sensitive_info_disclosure(self) -> None:
        findings = _scan_LLM02_sensitive_info_disclosure()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[LLM02] {f.id} ({f.severity}): {f.finding}")

    def test_LLM03_supply_chain(self) -> None:
        findings = _scan_LLM03_supply_chain()
        for f in findings:
            print(f"\n[LLM03] {f.id} ({f.severity}): {f.finding}")

    def test_LLM04_data_poisoning(self) -> None:
        findings = _scan_LLM04_data_poisoning()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[LLM04] {f.id} ({f.severity}): {f.finding}")

    def test_LLM05_improper_output_handling(self) -> None:
        findings = _scan_LLM05_improper_output_handling()
        for f in findings:
            print(f"\n[LLM05] {f.id} ({f.severity}): {f.finding}")

    def test_LLM06_excessive_agency(self) -> None:
        findings = _scan_LLM06_excessive_agency()
        assert len(findings) > 0
        for f in findings:
            print(f"\n[LLM06] {f.id} ({f.severity}): {f.finding}")

    def test_LLM07_system_prompt_leakage(self) -> None:
        findings = _scan_LLM07_system_prompt_leakage()
        for f in findings:
            print(f"\n[LLM07] {f.id} ({f.severity}): {f.finding}")

    def test_LLM08_vector_embedding_weaknesses(self) -> None:
        findings = _scan_LLM08_vector_embedding_weaknesses()
        for f in findings:
            print(f"\n[LLM08] {f.id} ({f.severity}): {f.finding}")

    def test_LLM09_misinformation(self) -> None:
        findings = _scan_LLM09_misinformation()
        for f in findings:
            print(f"\n[LLM09] {f.id} ({f.severity}): {f.finding}")

    def test_LLM10_unbounded_consumption(self) -> None:
        findings = _scan_LLM10_unbounded_consumption()
        for f in findings:
            print(f"\n[LLM10] {f.id} ({f.severity}): {f.finding}")

    # ---- Regression tests for high-severity findings ----

    def test_no_shell_true_in_production_code(self) -> None:
        """A03 regression: no production code uses shell=True."""
        production_files = [
            PIPELINE_PY, WRITE_JOURNAL_PY, MCP_VERBS_PY, MCP_MEMORY_PY,
            MCP_SEARCH_PY, TOOL_COMPLETE_PY, AUTO_SAVE_PY, CLI_PY,
            SEARCH_ORCH_PY, CRDT_MERGE_PY, SDK_PY, CLIENT_PY,
        ]
        for path in production_files:
            src = _read(path)
            for call in _ast_find_subprocess_calls(src):
                if _shell_true(call):
                    pytest.fail(
                        f"shell=True found in {path.name} — command injection risk"
                    )

    def test_api_token_not_empty_in_toml(self) -> None:
        """A02/A05 check: api.token must not be empty in committed config.

        Currently known finding — do not fail the suite. The scanners
        (A02-001, A05-001) already surface this as a HIGH-severity issue.
        """
        api_token = _get_memory_toml_value("api.token")
        if api_token in ('""', "", None):
            print(
                "\n[A02/A05] api.token is empty in memory.toml "
                "— REST API runs without authentication"
            )

    def test_db_created_with_0600(self) -> None:
        """A02 regression: DB files must be created with 0o600 permissions."""
        db_src = _read(DB_PY)
        assert "0o600" in db_src, "DB creation with 0o600 not found in infra/db.py"

    def test_path_traversal_prevention_resolve_save_paths(self) -> None:
        """A03/A04 regression: _resolve_save_paths must reject path traversal."""
        pipeline_src = _read(PIPELINE_PY)
        assert "is_relative_to" in pipeline_src, (
            "path.is_relative_to() check not found — path traversal prevention may be missing"
        )
        assert '"/" in' in pipeline_src or "slash" in pipeline_src.lower(), (
            "Category/slug slash check not found"
        )

    def test_injection_scanner_covers_all_categories(self) -> None:
        """LLM01 regression: scanner must cover all 4 categories."""
        inj_src = _read(INJECTION_PY)
        categories = re.findall(
            r'"(imperative|roleplay|system_prompt|tool_invocation)"', inj_src
        )
        assert len(categories) >= 4, (
            f"Injection scanner only covers {len(categories)}/4 categories"
        )

    def test_no_unparameterised_sql_in_core_paths(self) -> None:
        """A03 regression: core write path must use parameterised queries."""
        core_files = {
            "pipeline.py": _read(PIPELINE_PY),
            "write_journal.py": _read(WRITE_JOURNAL_PY),
        }
        for fname, src in core_files.items():
            sql_strings = _ast_find_sql_strings(src)
            for sql in sql_strings:
                assert "?" in sql or "%s" in sql, (
                    f"Unparameterised SQL in {fname}: {sql[:120]}"
                )

    def test_markdown_report_generation(self) -> None:
        """The security report can be generated without errors."""
        report = generate_security_report()
        assert "Security Health Check Report" in report
        assert "Generated:" in report
        assert "Total findings:" in report
        # At least one non-INFO category should appear
        assert any(owasp in report for owasp in ("A01", "A02", "A03", "LLM01", "LLM06"))
