/**
 * agentic-memory OpenCode plugin — pure implementation
 *
 * This module contains the actual hook logic — what happens when
 * OpenCode fires a tool call, session start, compaction, etc.
 *
 * It knows NOTHING about OpenCode's plugin system. It exports plain
 * functions with plain arguments. The OpenCode-facing adapter lives
 * in index.ts and is the ONLY file that imports from @opencode-ai/plugin.
 *
 * This separation means:
 * - OpenCode API changes only require rewriting index.ts
 * - Hook behavior is versioned, tested, and reviewed independently
 * - This file can be reused by other harnesses (Claude Code, etc.)
 */

import * as fs from "fs"
import * as path from "path"
import { spawn } from "child_process"

// ── Path resolution ──────────────────────────────────────────────────────────

const AGENTIC_MEMORY_DIR = process.env.AGENTIC_MEMORY_DIR || path.join(
  process.env.HOME || "/Users/arka",
  ".config",
  "agentic-memory"
)

function resolveVenvPython(): string {
  for (const sub of ["venv", ".venv"]) {
    const candidate = path.join(AGENTIC_MEMORY_DIR, sub, "bin", "python")
    if (fs.existsSync(candidate)) return candidate
  }
  return process.execPath
}

const VENV = resolveVenvPython()
const CONTEXT_MONITOR = path.join(AGENTIC_MEMORY_DIR, "context_monitor.py")
const AUTO_SAVE = path.join(AGENTIC_MEMORY_DIR, "auto_save.py")
const MEMORY_SESSION_START = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-session-start.py")
const MEMORY_RECALL = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-recall-session.py")
const PROACTIVE_CONTEXT = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-proactive-context.py")
const MEMORY_SESSION_END = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-session-end.py")
const MEMORY_PRECOMPACT_SNAPSHOT = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-precompact-snapshot.py")
const STATE_FILE = path.join(AGENTIC_MEMORY_DIR, "memory", "sessions", ".context_monitor_state.json")
const ERROR_LOG = path.join(AGENTIC_MEMORY_DIR, "memory", "hook-errors.jsonl")
const CIRCUIT_SENTINEL = path.join(AGENTIC_MEMORY_DIR, "memory", ".auto_save_circuit_sentinel")

// ── Circuit breaker ──────────────────────────────────────────────────────────

const CIRCUIT_THRESHOLD = 10
const COOL_DOWN_MS = 5 * 60 * 1000
const failureCounts = new Map<string, number>()
const circuitOpenTimes = new Map<string, number>()

function isCircuitOpen(label: string): boolean {
  const failures = failureCounts.get(label) || 0
  if (failures < CIRCUIT_THRESHOLD) return false
  const openTime = circuitOpenTimes.get(label) || 0
  return Date.now() - openTime < COOL_DOWN_MS
}

function recordSuccess(label: string): void {
  const existing = failureCounts.get(label)
  if (existing) {
    failureCounts.set(label, 0)
    circuitOpenTimes.delete(label)
  }
}

function isAutoSaveCircuitOpen(log: (msg: string) => void): boolean {
  if (!fs.existsSync(CIRCUIT_SENTINEL)) return false
  if (isCircuitOpen("auto-save")) {
    log(`[agentic-memory] Circuit OPEN for auto-save (TS + sentinel) — skipping`)
    return true
  }
  log(`[agentic-memory] Circuit OPEN for auto-save (Python CB via sentinel) — skipping`)
  return true
}

function recordFailure(log: (msg: string) => void, label: string, err: unknown, extra?: Record<string, unknown>): void {
  const count = (failureCounts.get(label) || 0) + 1
  failureCounts.set(label, count)
  if (count === CIRCUIT_THRESHOLD) {
    circuitOpenTimes.set(label, Date.now())
    log(`[agentic-memory] Circuit breaker OPENED for ${label} after ${count} consecutive failures`)
  } else if (count > CIRCUIT_THRESHOLD) {
    log(`[agentic-memory] ${label} failed (${count}/${CIRCUIT_THRESHOLD}): ${err instanceof Error ? err.message : String(err)}`)
  }
  try {
    fs.appendFileSync(ERROR_LOG, JSON.stringify({ ts: Date.now(), label, error: err instanceof Error ? err.message : String(err), failureCount: count, ...extra }) + "\n")
  } catch { /* ignore */ }
}

function surfaceRecentHookErrors(log: (msg: string) => void, maxAgeMs: number = 5 * 60 * 1000): void {
  if (!fs.existsSync(ERROR_LOG)) return
  const cutoff = Date.now() - maxAgeMs
  try {
    const lines = fs.readFileSync(ERROR_LOG, "utf8").split("\n").filter(Boolean)
    const recent = lines.filter(line => {
      try {
        const entry = JSON.parse(line)
        return entry.ts >= cutoff
      } catch {
        return false
      }
    })
    if (recent.length > 0) {
      log(`[agentic-memory] ${recent.length} recent hook error(s) — check hook-errors.jsonl for details`)
    }
  } catch { /* ignore */ }
}

// ── Subprocess helpers ───────────────────────────────────────────────────────

async function captureOutput(
  args: string[],
  stdinData?: string,
  label = "unknown",
  log: (msg: string) => void
): Promise<string> {
  if (isCircuitOpen(label)) {
    log(`[agentic-memory] Circuit OPEN for ${label} — skipping`)
    return ""
  }
  return await new Promise<string>((resolve) => {
    const child = spawn(VENV, args, { stdio: ["pipe", "pipe", "pipe"] })
    let stdout = ""
    child.stdout?.on("data", (d: Buffer) => { stdout += d })
    if (stdinData) {
      child.stdin?.write(stdinData)
      child.stdin?.end()
    }
    child.on("close", (code) => {
      if (code === 0) { recordSuccess(label); resolve(stdout) }
      else { recordFailure(log, label, `Exit code ${code}`, { code }); resolve("") }
    })
    child.on("error", (err) => { recordFailure(log, label, err); resolve("") })
  })
}

function fireAndForget(args: string[], label = "unknown", log: (msg: string) => void): void {
  if (isCircuitOpen(label)) {
    log(`[agentic-memory] Circuit OPEN for ${label} — skipping`)
    return
  }
  try {
    const child = spawn(VENV, args, { stdio: ["ignore", "ignore", "pipe"], detached: true })
    child.on("error", (err) => recordFailure(log, label, err))
    child.on("close", (code) => {
      if (code === 0) recordSuccess(label)
      else recordFailure(log, label, `Exit code ${code}`, { code })
    })
    child.unref()
  } catch (err) {
    recordFailure(log, label, err)
  }
}

// ── MCP prefix handling ──────────────────────────────────────────────────────

function stripMcpPrefix(tool: string): string {
  const PREFIX = "agentic-memory_"
  return tool.startsWith(PREFIX) ? tool.slice(PREFIX.length) : tool
}

// ── Feature flags ────────────────────────────────────────────────────────────

const DISABLED_HOOKS = new Set(
  (process.env.ECC_DISABLED_HOOKS || "")
    .split(",").map((s) => s.trim()).filter(Boolean)
)

const profileOrder: Record<string, number> = { minimal: 0, standard: 1, strict: 2 }
const ECC_HOOK_PROFILE = process.env.ECC_HOOK_PROFILE || "standard"

// Throttle auto-saves: at most one per tool per AUTO_SAVE_THROTTLE_MS.
// Prevents bash/edit/read spam from flooding the sessions category.
const _lastAutoSaveTime = new Map<string, number>()
const AUTO_SAVE_THROTTLE_MS = (() => {
  const env = parseInt(process.env.AUTO_SAVE_THROTTLE_MS || "", 10)
  return Number.isFinite(env) && env > 0 ? env : 60000
})()

function hookEnabled(id: string, required: string | string[] = "standard"): boolean {
  if (DISABLED_HOOKS.has(id)) return false
  const req = Array.isArray(required) ? required : [required]
  return req.some((r) => (profileOrder[r] || 0) <= (profileOrder[ECC_HOOK_PROFILE] || 0))
}

function isAutoSaveAllowed(tool: string): boolean {
  const DENIED = new Set(["ls", "cd", "pwd", "echo", "which", "type", "clear", "env", "printenv", "date", "sleep", "source", "."])
  return !DENIED.has(stripMcpPrefix(tool))
}

// ── Shared mutable state ─────────────────────────────────────────────────────
// Written by startSession(), read + cleared by injectSystemPrompt() on the
// first LLM call after session start.

export const state = {
  sessionContext: "",
  proactiveContext: "",
}

// ── Hook implementations ─────────────────────────────────────────────────────
// Each function is a self-contained unit: plain args, plain return value,
// no framework dependencies. Callable from any harness.

export function startSession(log: (msg: string) => void): Promise<void> {
  return runSessionStart(log)
}

async function runSessionStart(log: (msg: string) => void): Promise<void> {
  // Session context is a one-time bootstrap: recall + pinned/high-
  // importance notes. It's injected once at session creation and then
  // cleared. Proactive context (per-tool) is a separate mechanism
  // handled by beforeTool + injectSystemPrompt.
  if (!hookEnabled("session:start", ["minimal", "standard", "strict"])) return

  const contexts: Promise<string>[] = []

  if (hookEnabled("session:start:memory-recall", ["minimal"])) {
    contexts.push(captureOutput([MEMORY_RECALL], undefined, "recall", log))
  }
  if (hookEnabled("session:start:memory-bootstrap", ["minimal"])) {
    contexts.push(captureOutput([MEMORY_SESSION_START], undefined, "memory-session-start", log))
  }

  if (contexts.length > 0) {
    state.sessionContext = (await Promise.all(contexts)).filter(Boolean).join("\n\n")
  }
}

export function onToolAfter(tool: string, args: Record<string, unknown> | undefined, output: unknown, log: (msg: string) => void): void {
  if (!hookEnabled("post:memory:auto-save", ["minimal"])) return
  if (!isAutoSaveAllowed(tool)) return

  surfaceRecentHookErrors(log)

  const paramsJson = JSON.stringify(args ?? {}).slice(0, 2000)
  const preview = typeof output === "string" ? output.slice(0, 200) : ""

  fireAndForget([CONTEXT_MONITOR, "track", "--tool", tool, "--params", paramsJson, "--result-preview", preview], "track", log)
  if (!isAutoSaveCircuitOpen(log)) {
    const now = Date.now()
    const last = _lastAutoSaveTime.get(tool) || 0
    if (now - last < AUTO_SAVE_THROTTLE_MS) {
      log(`[agentic-memory] auto-save throttled for ${tool} (${now - last}ms since last)`)
      return
    }
    _lastAutoSaveTime.set(tool, now)
    fireAndForget([AUTO_SAVE, "tool-complete", "--tool", tool, "--params", paramsJson, "--result-preview", preview], "auto-save", log)
  }
}

export function beforeTool(tool: string, args: Record<string, unknown> | undefined, log: (msg: string) => void): Promise<void> {
  // Proactive context is captured here and injected into the system
  // prompt on the NEXT LLM call (via injectSystemPrompt). If no LLM
  // call occurs before the next tool, the context is overwritten by
  // the next beforeTool call. The agent always sees the latest
  // proactive context in this hook's stdout stream regardless.
  return captureOutput([PROACTIVE_CONTEXT], JSON.stringify({ tool_name: tool, tool_input: args ?? {} }), "memory-proactive-context", log).then((out) => {
    if (out.trim()) state.proactiveContext = out.trim()
  })
}

export function onIdle(log: (msg: string) => void): void {
  if (!hookEnabled("session:idle:memory-checkpoint", ["minimal"])) return
  try { fireAndForget([CONTEXT_MONITOR, "idle"], "idle", log) } catch { /* ignore */ }
}

export function endSession(sessionId: string, log: (msg: string) => void): Promise<void> {
  if (!hookEnabled("session:end:memory-summary", ["minimal"])) return Promise.resolve()

  log("[agentic-memory] Flushing final session summary (blocking)...")
  return new Promise<void>((resolve, reject) => {
    const child = spawn(VENV, [CONTEXT_MONITOR, "end", "--session-id", sessionId || "unknown"], { stdio: ["ignore", "pipe", "ignore"] })
    let stdout = ""
    child.stdout?.on("data", (d: Buffer) => { stdout += d })
    child.on("close", (code) => {
      if (code === 0) { log(`[agentic-memory] Session end summary saved: ${stdout.trim()}`); resolve() }
      else reject(new Error(`Exit code ${code}: ${stdout}`))
    })
    child.on("error", reject)
  }).then(() => {
    // Blocking: save session memory note (Rule #7 compliance).
    // Previously fire-and-forget, which meant the agent got no
    // confirmation whether the session save succeeded. Now we wait
    // up to 10s for the script to write the session summary to the
    // memory DB so silent data loss is impossible.
    return new Promise<void>((resolve, reject) => {
      const sessionChild = spawn(VENV, [MEMORY_SESSION_END], {
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env, MEMORY_HOOK_EVENT: "stop" },
      })
      sessionChild.stdin?.write(JSON.stringify({ session_id: sessionId || "unknown" }))
      sessionChild.stdin?.end()

      let stdout = ""
      let stderr = ""
      sessionChild.stdout?.on("data", (d: Buffer) => { stdout += d })
      sessionChild.stderr?.on("data", (d: Buffer) => { stderr += d })

      const timeout = setTimeout(() => {
        reject(new Error("Session-end save timed out after 10s"))
      }, 10000)

      sessionChild.on("close", (code) => {
        clearTimeout(timeout)
        if (code === 0) {
          log(`[agentic-memory] Session memory saved: ${stdout.trim()}`)
          resolve()
        } else {
          reject(new Error(`Session-end save failed (exit ${code}): ${stderr.trim()}`))
        }
      })
      sessionChild.on("error", (err) => {
        clearTimeout(timeout)
        reject(err)
      })
    })
  }).catch((e) => log(`[agentic-memory] Session end save failed: ${e}`))
}

export function injectSystemPrompt(system: string[]): void {
  // NOTE: OpenCode fires `experimental.chat.system.transform` before
  // each LLM call (not just once at session start). Each call pushes
  // whatever is currently in state.sessionContext / state.proactiveContext
  // and then clears it so we don't inject stale context on the next turn.
  // If OpenCode ever changes this to fire only once, proactive context
  // would only be delivered on the very first LLM call.
  if (state.sessionContext) {
    system.push(state.sessionContext)
    state.sessionContext = ""
  }
  if (state.proactiveContext) {
    system.push(state.proactiveContext)
    state.proactiveContext = ""
  }
}

export async function onCompacting(sessionId: string, output: { context: string[]; prompt?: string }, log: (msg: string) => void): Promise<void> {
  if (!hookEnabled("session:compacting:memory-save", ["minimal"])) return

  let lastCompactionTime = 0
  try {
    if (fs.existsSync(STATE_FILE)) {
      const stateContent = fs.readFileSync(STATE_FILE, "utf8")
      const state = JSON.parse(stateContent)
      lastCompactionTime = state.last_compaction_time || 0
    }
  } catch { /* state file may not exist yet */ }

  const now = Date.now() / 1000
  const isThrottled = now - lastCompactionTime < 45

  if (isThrottled) {
    log("[agentic-memory] Skipping pre-compaction context save (throttled)")
    output.context.push("\n\n⚠️ COMPACTION SURVIVAL: A pre-compaction context note was saved. After compaction, search for 'pre-compaction context save' to recover what was happening before this compaction event.")
  } else {
    log("[agentic-memory] Context compaction detected — saving pre-compaction context")

    let messageCount = "0"
    try {
      if (fs.existsSync(STATE_FILE)) {
        const stateContent = fs.readFileSync(STATE_FILE, "utf8")
        const state = JSON.parse(stateContent)
        messageCount = String(state.total_tool_calls || state.tool_call_count || 0)
      }
    } catch { /* state file may not exist yet */ }

    // Blocking: snapshot raw events.jsonl before compaction destroys it.
    // Previously fire-and-forget, which meant snapshot failures were
    // invisible to the agent. Now we capture the JSON result and
    // surface any failure in the compaction warning that the agent
    // sees after compaction completes.
    let snapshotOk = true
    try {
      const snapshotRaw = await captureOutput([MEMORY_PRECOMPACT_SNAPSHOT, JSON.stringify({ session_id: sessionId || "unknown" })], "precompact-snapshot", log)
      const snapshotParsed = JSON.parse(snapshotRaw.trim() || "{}")
      if (!snapshotParsed.ok) {
        snapshotOk = false
        log(`[agentic-memory] Pre-compaction snapshot failed: ${snapshotParsed.error || "unknown error"}`)
      }
    } catch (e) {
      snapshotOk = false
      log(`[agentic-memory] Pre-compaction snapshot error: ${e}`)
    }

    const result = await captureOutput([CONTEXT_MONITOR, "compact", "--session-id", sessionId || "unknown", "--message-count", messageCount], undefined, "compact", log)
    log(`[agentic-memory] Pre-compaction context saved: ${result.trim()}`)

    const snapshotWarning = snapshotOk ? "" : " (snapshot failed — data may be incomplete)"
    output.context.push(`\n\n⚠️ COMPACTION SURVIVAL: A pre-compaction context note was saved.${snapshotWarning} After compaction, search for 'pre-compaction context save' to recover what was happening before this compaction event.`)
  }

  output.prompt = "Focus on preserving: 1) Current task status and progress, 2) Key decisions made, 3) Files created/modified, 4) Remaining work items, 5) Any security concerns flagged. Discard: verbose tool outputs, intermediate exploration, redundant file listings."
}
