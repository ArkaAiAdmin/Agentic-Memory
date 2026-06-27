# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.x     | ✅ Yes             |
| < 1.0   | ❌ No              |

## Reporting a Vulnerability

We take security seriously. If you discover a vulnerability in Agentic Memory, please report it **privately** so we can fix it before public disclosure.

### Preferred channel

Email the maintainers directly at the address listed in the GitHub repository's security tab (or open a **private** security advisory via GitHub).

**Do not open a public issue for security vulnerabilities.**

### What to include

1. Description of the vulnerability and affected component (e.g. save pipeline, sync server, MCP tool)
2. Step-by-step reproduction (or a test case in `eval/`)
3. Impact assessment — what can an attacker do? Data loss? Code execution? Information disclosure?
4. Suggested fix if you have one

### Response timeline

- **48 hours** — initial acknowledgment
- **7 days** — triage decision (accept / request more info / won't fix)
- **30 days** — patch released (or interim mitigation if fix requires more time)

### Scope

In scope:

- Code in this repository (not a fork)
- The MCP server, sync server, CLI, hooks, and cron jobs
- Local SQLite storage and file-system interactions
- The auto-save daemon and background worker

Out of scope:

- CVEs in upstream dependencies (report to upstream instead; we track via Dependabot)
- Issues requiring local access to a running installation (assumes trusted localhost)
- Social engineering / phishing the user into running malicious commands

### Disclosure policy

We follow **coordinated disclosure**:

1. Reporter submits privately.
2. We confirm and fix within 30 days.
3. We publish a security advisory and credit the reporter (unless they prefer anonymity).
4. CVE requested if applicable.

### Past advisories

<!-- Will be populated as advisories are published. -->

_(None yet — this project is early stage.)_
