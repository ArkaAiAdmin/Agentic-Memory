# Operator-Triggered Reload Commands

Two OpenCode slash commands let a human operator (via the OpenCode CLI) reload the
`memory.toml` drift policy and patch flag drift tiers at runtime, without waiting for
the cron-driven hot-reload poller.

## Commands

| Command | File | What it does |
| --- | --- | --- |
| `/reload-toml` | `~/.config/opencode/commands/reload-toml.md` | Runs `hooks/memory_toml_reload.py` to re-read `memory.toml`, reset config + policy cache, and re-apply tier overrides. Prints `before_policy_hash` / `after_policy_hash` / `changed`. |
| `/tier-patch` | `~/.config/opencode/commands/tier-patch.md` | Runs `hooks/memory_tier_patch.py` to set/remove a flag's drift tier (or reset all to hardcoded defaults) in the running process. Prints `patched` / `rejected`. |

Both command files live in the **global** OpenCode commands directory
(`~/.config/opencode/commands/`), so they are available regardless of which project
directory OpenCode is launched in. The filename (minus `.md`) is the command name.

## Usage

```text
# Reload with no reason (or a free-text reason):
/reload-toml
/reload-toml manual operator reload after config edit

# Patch a single flag's tier:
/tier-patch {"flag": "MEMORY_SAGA_ENABLED", "tier": "stability"}

# Remove a runtime override (restore built-in tier):
/tier-patch {"flag": "MEMORY_SAGA_ENABLED", "tier": null}

# Restore all hardcoded default tiers:
/tier-patch {"reset": true}
```

## How they work

OpenCode custom commands are markdown files whose body is a template. The body uses
the `!`...`` shell-output directive, which **executes the wrapped command as a real
subprocess** (not just a prompt) and injects its stdout into the conversation. The
JSON result from the Python script is therefore shown to the operator verbatim.

- `$ARGUMENTS` is OpenCode's placeholder for the text typed after the command name. It
  is substituted textually into the template before the `!`...`` block is executed.
- For `/tier-patch` the argument JSON is passed through `printf '%s' '$ARGUMENTS'`
  (single-quoted so embedded double quotes in the JSON survive bash unmolested) and
  piped to the script's stdin.
- For `/reload-toml` the optional reason is wrapped into `{"reason": "..."}` by a tiny
  inline Python JSON builder (again single-quoting `$ARGUMENTS` for quote safety); an
  empty argument yields `{"reason": ""}`, which the script treats as "no reason".

All invocations use the repo interpreter, matching `opencode.jsonc` line 7:

```text
/Users/arka/.config/agentic-memory/venv/bin/python
```

## Scope / notes

- These are **runtime / in-process** mutations only. They change the live Python
  process's config and `_FLAG_TIERS`; they do **not** persist to disk or the database,
  and a process restart restores the on-disk `memory.toml` state. Use them for
  immediate operator overrides between cron reloads or restarts.
- `hooks/memory_toml_reload.py` and `hooks/memory_tier_patch.py` are unchanged by this
  task — only the command files were added.
- The commands were not added to `opencode.jsonc` (no `command`/`commands` key was
  required; the markdown-file mechanism is sufficient and keeps the config untouched).
