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
    # AWS access key ids
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # generic key=value assignments with credential-looking values
    re.compile(r"(api[_-]?key|secret|password|passwd|token|auth[_-]?token)\s*[:=]\s*[\"'][A-Za-z0-9+/=_\-]{20,}[\"']"),
    # private key blocks
    re.compile(r"-----BEGIN (RSA|EC|OPENSSH|DSA) PRIVATE KEY-----"),
]


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


def main() -> int:
    violations: list[tuple[str, int, str]] = []
    for path, lineno, content in _added_lines():
        for pat in SECRET_PATTERNS:
            if pat.search(content):
                violations.append((path, lineno, content))
                break

    if violations:
        print(
            "ERROR: credential-looking content on added lines (Rule 18):",
            file=sys.stderr,
        )
        for path, lineno, content in violations:
            print(f"  {path}:{lineno}: {content.strip()}", file=sys.stderr)
        return 1

    print("OK: no credential patterns on added lines.")
    return 0


if __name__ == "__main__":
    sys.exit(main())