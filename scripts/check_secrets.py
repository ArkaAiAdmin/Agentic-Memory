"""Pre-commit guard: reject credential-looking patterns on added lines (Rule 18).

Rule 18: security by default — never commit credentials, tokens, or keys.
Scans only lines added in the staged diff (git diff --cached) with
length thresholds so test fixtures like `api_key = "dummy"` do not
false-positive; a short value is not a credential.

Exit 0 = OK, 1 = violation (lists file:line of each offending added line).
"""

from __future__ import annotations

import re
import subprocess
import sys

SECRET_PATTERNS: list[re.Pattern[str]] = [
    # sk- prefixed keys (OpenAI-style) — long enough to be real
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    # Stripe secret keys (live and test)
    re.compile(r"\bsk_(?:live|test)_[0-9a-zA-Z]{24,}\b"),
    # GitHub tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b"),
    # Slack tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    # Google API keys
    re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    # AWS access key ids
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # generic key=value assignments with credential-looking values
    re.compile(r"(api[_-]?key|secret|password|passwd|token|auth[_-]?token)\s*[:=]\s*[\"'][A-Za-z0-9+/=_\-]{20,}[\"']"),
    # private key blocks
    re.compile(r"-----BEGIN (RSA|EC|OPENSSH|DSA) PRIVATE KEY-----"),
]

# Patterns explicitly allowed in test fixtures and local configs
ALLOWLIST_PATTERNS = [
    re.compile(r"am-local-stable-token-"),
    re.compile(r"test-api-token-"),
    re.compile(r"phase2-test-token-"),
    re.compile(r"test-tenant-isolation-token-"),
    re.compile(r"test-sync-token-"),
    re.compile(r"example"),
    re.compile(r"dummy"),
    re.compile(r"placeholder"),
    re.compile(r"sk_test_mock"),
]


def _is_allowlisted(line: str) -> bool:
    return any(pat.search(line) for pat in ALLOWLIST_PATTERNS)


def _added_lines() -> list[tuple[str, int, str]]:
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--diff-filter=ACM", "--unified=0"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []

    hits: list[tuple[str, int, str]] = []
    path: str | None = None
    new_line: int | None = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
        elif line.startswith("@@ "):
            m = re.search(r"\+(\d+)(?:,\d+)? @@", line)
            new_line = int(m.group(1)) if m else None
        elif line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            if path is not None and new_line is not None:
                hits.append((path, new_line, content))
                if new_line is not None:
                    new_line += 1
    return hits


def _full_tree_lines() -> list[tuple[str, int, str]]:
    try:
        files = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.splitlines()
    except Exception:
        return []

    hits: list[tuple[str, int, str]] = []
    skip_exts = (".png", ".jpg", ".jpeg", ".ico", ".lock", ".db", ".sqlite", ".bin", ".pyc")
    for f in files:
        if any(f.endswith(ext) for ext in skip_exts):
            continue
        # Skip benchmark evaluation dataset dumps
        if f.startswith("eval/longmemeval"):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fp:
                for idx, line in enumerate(fp, start=1):
                    hits.append((f, idx, line))
        except Exception:
            continue
    return hits


def main() -> int:
    is_full_scan = "--full" in sys.argv
    lines = _full_tree_lines() if is_full_scan else _added_lines()

    violations: list[tuple[str, int, str]] = []
    for path, lineno, content in lines:
        if _is_allowlisted(content):
            continue
        for pat in SECRET_PATTERNS:
            if pat.search(content):
                violations.append((path, lineno, content))
                break

    if violations:
        scope = "tracked files (--full)" if is_full_scan else "added lines"
        print(
            f"ERROR: credential-looking content found in {scope} (Rule 18):",
            file=sys.stderr,
        )
        for path, lineno, content in violations:
            print(f"  {path}:{lineno}: {content.strip()}", file=sys.stderr)
        return 1

    scope_msg = "full tree" if is_full_scan else "added lines"
    print(f"OK: no credential patterns in {scope_msg}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())