/**
 * agentic-memory OpenCode plugin — pure implementation
 *
 * This module contains the actual hook logic — what happens when
 * a harness fires a tool call, session start, compaction, etc.
 *
 * It knows NOTHING about any specific harness. It exports plain
 * functions that accept a HookContext, which wraps a HarnessAdapter
 * providing logging, spawning, and state access. The harness-facing
 * adapter lives in index.ts and is the ONLY file that imports from
 * a harness SDK.
 *
 * This separation means:
 * - Harness API changes only require rewriting index.ts
 * - Hook behavior is versioned, tested, and reviewed independently
 * - This file can be reused by any harness (OpenCode, Claude Code, etc.)
 */

import * as fs from "fs"
import * as path from "path"
import { spawn } from "child_process"
import type { HookContext, HarnessAdapter } from "./types.js"

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
  throw new Error(
    `No Python venv found at ${path.join(AGENTIC_MEMORY_DIR, "venv/bin/python")} ` +
    `or ${path.join(AGENTIC_MEMORY_DIR, ".venv/bin/python")}. ` +
    `Run: cd ${AGENTIC_MEMORY_DIR} && python -m venv venv && venv/bin/pip install -e .`
  )
}

export { resolveVenvPython }

const VENV = resolveVenvPython()
const CONTEXT_MONITOR = path.join(AGENTIC_MEMORY_DIR, "context_monitor.py")
const AUTO_SAVE = path.join(AGENTIC_MEMORY_DIR, "auto_save.py")
const MEMORY_SESSION_START = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-session-start.py")
const MEMORY_RECALL = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-recall-session.py")
const PROACTIVE_CONTEXT = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-proactive-context.py")
const MEMORY_SESSION_END = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-session-end.py")
const MEMORY_PRECOMPACT_SNAPSHOT = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-precompact-snapshot.py")
const MEMORY_COORDINATION = path.join(AGENTIC_MEMORY_DIR, "hooks", "memory-coordination.py")
const AGENT_CONTRACT_FILE = path.join(AGENTIC_MEMORY_DIR, "AGENT_CONTRACT.md")
const STATE_FILE = path.join(AGENTIC_MEMORY_DIR, "memory", "sessions", ".context_monitor_state.json")
const ERROR_LOG = path.join(AGENTIC_MEMORY_DIR, "memory", "hook-errors.jsonl")
const AUTO_SAVE_RESULTS = path.join(AGENTIC_MEMORY_DIR, "memory", ".auto_save_results.jsonl")
const CIRCUIT_SENTINEL = path.join(AGENTIC_MEMORY_DIR, "memory", ".auto_save_circuit_sentinel")

// ── Circuit breaker ──────────────────────────────────────────────────────────

// Z-6 fix: lowered from 10 to match Python's default max_retries=3 in
// background/circuit_breaker.py._auto_save_record_failure_and_maybe_trip,
// which trips at 4 failures (n_failures > max_retries).  Both sides must
// use the same threshold: the TS counter is independent of the Python
// sentinel, so if TS allows 10 spawns while Python circuit is already
// open, 6 unnecessary subprocesses will be launched before TS trips.
const CIRCUIT_THRESHOLD = 4
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

function isAutoSaveCircuitOpen(adapter: HarnessAdapter): boolean {
  const jsCircuitOpen = isCircuitOpen("auto-save")

  if (jsCircuitOpen) {
    adapter.log("[agentic-memory] Circuit OPEN for auto-save (TS circuit breaker) — skipping")
    return true
  }

  // 2026-07-08: read sentinel JSON — it now carries pid + ts so we can
  // distinguish a stale sentinel (crashed daemon) from an active circuit.
  let sentinelPid: number | undefined
  try {
    const raw = fs.readFileSync(CIRCUIT_SENTINEL, "utf-8")
    const data = JSON.parse(raw)
    if (data.status === "open") {
      sentinelPid = data.pid
    }
  } catch {
    // No sentinel, corrupt, or legacy format (bare "open") — fall through
    // to the bare-exists check below for backward compat.
  }

  if (sentinelPid !== undefined) {
    // New-style sentinel: check if the owning process is still alive.
    try {
      process.kill(sentinelPid, 0)
    } catch {
      adapter.log(`[agentic-memory] Stale sentinel (pid=${sentinelPid} dead) — clearing, treating circuit as CLOSED`)
      try { fs.unlinkSync(CIRCUIT_SENTINEL) } catch {}
      return false
    }
    adapter.log(`[agentic-memory] Circuit OPEN for auto-save (Python CB via sentinel pid=${sentinelPid}) — skipping`)
    return true
  }

  // Fallback: bare file exists (legacy sentinel without PID).
  if (fs.existsSync(CIRCUIT_SENTINEL)) {
    adapter.log("[agentic-memory] Circuit OPEN for auto-save (Python CB via sentinel) — skipping")
    return true
  }

  return false
}

function recordFailure(adapter: HarnessAdapter, label: string, err: unknown, extra?: Record<string, unknown>): void {
  const count = (failureCounts.get(label) || 0) + 1
  failureCounts.set(label, count)
  if (count === CIRCUIT_THRESHOLD) {
    circuitOpenTimes.set(label, Date.now())
    adapter.log(`[agentic-memory] Circuit breaker OPENED for ${label} after ${count} consecutive failures`)
  } else if (count > CIRCUIT_THRESHOLD) {
    adapter.log(`[agentic-memory] ${label} failed (${count}/${CIRCUIT_THRESHOLD}): ${err instanceof Error ? err.message : String(err)}`)
  }
  try {
    fs.appendFileSync(ERROR_LOG, JSON.stringify({ ts: Date.now(), label, error: err instanceof Error ? err.message : String(err), failureCount: count, ...extra }) + "\n")
  } catch { /* ignore */ }
}

function surfaceRecentHookErrors(adapter: HarnessAdapter, maxAgeMs: number = 5 * 60 * 1000): void {
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
      adapter.log(`[agentic-memory] ${recent.length} recent hook error(s) — check hook-errors.jsonl for details`)
    }
  } catch { /* ignore */ }
}

function surfaceRecentAutoSaveResults(adapter: HarnessAdapter, maxAgeMs: number = 5 * 60 * 1000): void {
  if (!fs.existsSync(AUTO_SAVE_RESULTS)) return
  const cutoff = Date.now() - maxAgeMs
  try {
    const lines = fs.readFileSync(AUTO_SAVE_RESULTS, "utf8").split("\n").filter(Boolean)
    const recent = lines.filter(line => {
      try {
        const entry = JSON.parse(line)
        return entry.ts >= cutoff
      } catch {
        return false
      }
    })
    const failed = recent.filter(e => e.status === "failed")
    const saved = recent.filter(e => e.status === "saved")
    if (failed.length > 0) {
      const previews = failed.slice(0, 3).map((e: Record<string, unknown>) => (e.error || e.tool) as string).join(", ")
      adapter.log(`[agentic-memory] ${failed.length} auto-save failure(s) recently: ${previews}`)
    }
    if (saved.length > 0) {
      adapter.log(`[agentic-memory] ${saved.length} auto-save note(s) confirmed`)
    }
    // Trim old entries to bound file size.
    try {
      const kept = lines.filter(line => {
        try {
          const entry = JSON.parse(line)
          return entry.ts >= cutoff
        } catch {
          return false
        }
      })
      if (kept.length < lines.length) {
        fs.writeFileSync(AUTO_SAVE_RESULTS, kept.join("\n") + (kept.length ? "\n" : ""))
      }
    } catch { /* ignore */ }
  } catch { /* ignore */ }
}

function getAutoSaveResult(entryId: string): any | null {
  if (!fs.existsSync(AUTO_SAVE_RESULTS)) return null
  try {
    const lines = fs.readFileSync(AUTO_SAVE_RESULTS, "utf8").split("\n").filter(Boolean)
    for (let i = lines.length - 1; i >= 0; i--) {
      try {
        const entry = JSON.parse(lines[i])
        if (entry.entry_id === entryId) return entry
      } catch {
        continue
      }
    }
  } catch { /* ignore */ }
  return null
}

async function waitForAutoSave(
  entryId: string,
  timeoutMs: number,
  adapter: HarnessAdapter
): Promise<any | null> {
  const deadline = Date.now() + timeoutMs
  let last: any = null
  while (Date.now() < deadline) {
    last = getAutoSaveResult(entryId)
    if (last !== null) break
    await new Promise(r => setTimeout(r, 50))
  }
  if (last === null) {
    adapter.log(`[agentic-memory] auto-save wait timed out after ${timeoutMs}ms for ${entryId}`)
  }
  return last
}

// ── Subprocess helpers ───────────────────────────────────────────────────────

async function captureOutput(
  args: string[],
  stdinData?: string,
  label = "unknown",
  adapter: HarnessAdapter
): Promise<string> {
  if (isCircuitOpen(label)) {
    adapter.log(`[agentic-memory] Circuit OPEN for ${label} — skipping`)
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
      else { recordFailure(adapter, label, `Exit code ${code}`, { code }); resolve("") }
    })
    child.on("error", (err) => { recordFailure(adapter, label, err); resolve("") })
  })
}

function fireAndForget(args: string[], label = "unknown", adapter: HarnessAdapter): void {
  if (isCircuitOpen(label)) {
    adapter.log(`[agentic-memory] Circuit OPEN for ${label} — skipping`)
    return
  }
  try {
    const child = spawn(VENV, args, { stdio: ["ignore", "ignore", "pipe"], detached: true })
    child.on("error", (err) => recordFailure(adapter, label, err))
    child.on("close", (code) => {
      if (code === 0) recordSuccess(label)
      else recordFailure(adapter, label, `Exit code ${code}`, { code })
    })
    child.unref()
  } catch (err) {
    recordFailure(adapter, label, err)
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
// first LLM call after session start. Managed via the adapter's getState()
// so hook functions are agnostic to the harness.

export const state = {
  sessionContext: "",
  proactiveContext: "",
}

// Cached agent contract content — read once from disk, injected on every LLM call.
let _agentContractContent: string | null = null

function loadAgentContract(): string {
  if (_agentContractContent === null) {
    try {
      _agentContractContent = fs.readFileSync(AGENT_CONTRACT_FILE, "utf8").trim()
    } catch {
      _agentContractContent = ""
    }
  }
  return _agentContractContent
}

// ── Hook implementations ─────────────────────────────────────────────────────
// Each function receives a HookContext with a HarnessAdapter. Plain args,
// plain return value, no harness-specific dependencies. Callable from any
// harness that provides a HarnessAdapter implementation.

export async function startSession(ctx: HookContext): Promise<void> {
  // ── Phase 3: Auto-bootstrap on first run ────────────────────────────────
  // If the memory DB hasn't been created yet, run init silently so the
  // agent can start using memory without manual setup. Idempotent: the
  // init script skips steps whose outputs already exist.
  try {
    const memDir = path.join(AGENTIC_MEMORY_DIR, "memory")
    const dbPath = path.join(memDir, "memory.db")
    if (!fs.existsSync(dbPath)) {
      ctx.adapter.log("[agentic-memory] First run detected — auto-initializing memory directory...")
      const initScript = path.join(AGENTIC_MEMORY_DIR, "cli.py")
      const result = await captureOutput(
        [VENV, initScript, "init", "--no-install"],
        undefined,
        "auto-init",
        ctx.adapter
      )
      const firstLine = result.trim().split("\n")[0]
      if (firstLine) {
        ctx.adapter.log(`[agentic-memory] Init: ${firstLine}`)
      } else {
        ctx.adapter.log("[agentic-memory] Init complete")
      }
    }
  } catch (e) {
    ctx.adapter.log(`[agentic-memory] Auto-init skipped: ${e instanceof Error ? e.message : String(e)}`)
  }
  // ── End auto-bootstrap ───────────────────────────────────────────────────

  return runSessionStart(ctx)
}

async function runSessionStart(ctx: HookContext): Promise<void> {
  const s = ctx.adapter.getState()
  // Session context is a one-time bootstrap: recall + pinned/high-
  // importance notes. It's injected once at session creation and then
  // cleared. Proactive context (per-tool) is a separate mechanism
  // handled by beforeTool + injectSystemPrompt.
  if (!hookEnabled("session:start", ["minimal", "standard", "strict"])) return

  const contexts: Promise<string>[] = []

  if (hookEnabled("session:start:memory-recall", ["minimal"])) {
    contexts.push(captureOutput([MEMORY_RECALL], undefined, "recall", ctx.adapter))
  }
  if (hookEnabled("session:start:memory-bootstrap", ["minimal"])) {
    contexts.push(captureOutput([MEMORY_SESSION_START], undefined, "memory-session-start", ctx.adapter))
  }

  // Coordination: check pending messages, load project state
  if (hookEnabled("session:start:coordination", ["standard", "strict"])) {
    const coordData = JSON.stringify({ action: "session_start", agent_id: sessionId })
    contexts.push(captureOutput([MEMORY_COORDINATION], coordData, "coordination-start", ctx.adapter))
  }

  if (contexts.length > 0) {
    s.sessionContext = (await Promise.all(contexts)).filter(Boolean).join("\n\n")
  }
}

export async function onToolAfter(ctx: HookContext): Promise<void> {
  const tool = ctx.toolName ?? ""
  const args = ctx.toolArgs
  const output = ctx.output

  if (!hookEnabled("post:memory:auto-save", ["minimal"])) return
  if (!tool) return
  if (!isAutoSaveAllowed(tool)) return

  surfaceRecentHookErrors(ctx.adapter)
  surfaceRecentAutoSaveResults(ctx.adapter)

  const paramsJson = JSON.stringify(args ?? {}).slice(0, 2000)
  const preview = typeof output === "string" ? output.slice(0, 200) : ""

  await ctx.adapter.fireAndForget([CONTEXT_MONITOR, "track", "--tool", tool, "--params", paramsJson, "--result-preview", preview], "track")
  if (!isAutoSaveCircuitOpen(ctx.adapter)) {
    const now = Date.now()
    const last = _lastAutoSaveTime.get(tool) || 0
    if (now - last < AUTO_SAVE_THROTTLE_MS) {
      ctx.adapter.log(`[agentic-memory] auto-save throttled for ${tool} (${now - last}ms since last)`)
      return
    }
    _lastAutoSaveTime.set(tool, now)

    const waitTimeoutMs = parseInt(process.env.AUTO_SAVE_WAIT_TIMEOUT_MS || "", 10)
    const blocking = Number.isFinite(waitTimeoutMs) && waitTimeoutMs > 0
    const entryId = `${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

    if (blocking) {
      try {
        const result = await captureOutput(
          [AUTO_SAVE, "tool-complete", "--tool", tool, "--params", paramsJson, "--result-preview", preview, "--entry-id", entryId, "--wait-timeout", String(waitTimeoutMs / 1000)],
          undefined,
          "auto-save-wait",
          ctx.adapter
        )
        const parsed = result ? JSON.parse(result) : {}
        if (parsed.status === "saved" || parsed.saved === true) {
          ctx.adapter.log(`[agentic-memory] auto-save confirmed for ${entryId}`)
        } else if (parsed.status === "timeout") {
          ctx.adapter.log(`[agentic-memory] auto-save wait timed out for ${entryId}`)
        }
      } catch (e) {
        ctx.adapter.log(`[agentic-memory] auto-save blocking call failed: ${e instanceof Error ? e.message : String(e)}`)
      }
    } else {
      await ctx.adapter.fireAndForget([AUTO_SAVE, "tool-complete", "--tool", tool, "--params", paramsJson, "--result-preview", preview, "--entry-id", entryId], "auto-save")
    }
  }
}

export function beforeTool(ctx: HookContext): Promise<void> {
  // Proactive context is captured here and injected into the system
  // prompt on the NEXT LLM call (via injectSystemPrompt). If no LLM
  // call occurs before the next tool, the context is overwritten by
  // the next beforeTool call. The agent always sees the latest
  // proactive context in this hook's stdout stream regardless.
  const tool = ctx.toolName ?? ""
  if (!tool) return Promise.resolve()
  const args = ctx.toolArgs
  return captureOutput([PROACTIVE_CONTEXT], JSON.stringify({ tool_name: tool, tool_input: args ?? {} }), "memory-proactive-context", ctx.adapter).then((out) => {
    if (out.trim()) ctx.adapter.getState().proactiveContext = out.trim()
  })
}

export async function onIdle(ctx: HookContext): Promise<void> {
  if (!hookEnabled("session:idle:memory-checkpoint", ["minimal"])) return
  try { await ctx.adapter.fireAndForget([CONTEXT_MONITOR, "idle"], "idle") } catch { /* ignore */ }
}

export function endSession(ctx: HookContext): Promise<void> {
  const sessionId = ctx.sessionId ?? "unknown"

  if (!hookEnabled("session:end:memory-summary", ["minimal"])) return Promise.resolve()

  ctx.adapter.log("[agentic-memory] Flushing final session summary (blocking)...")
  let contextEndOk = false
  let sessionEndOk = false
  return new Promise<void>((resolve, reject) => {
    const child = spawn(VENV, [CONTEXT_MONITOR, "end", "--session-id", sessionId], { stdio: ["ignore", "pipe", "ignore"] })
    let stdout = ""
    child.stdout?.on("data", (d: Buffer) => { stdout += d })
    child.on("close", (code) => {
      if (code === 0) {
        ctx.adapter.log(`[agentic-memory] Session end summary saved: ${stdout.trim()}`)
        contextEndOk = true
        resolve()
      } else {
        reject(new Error(`Exit code ${code}: ${stdout}`))
      }
    })
    child.on("error", reject)
  }).catch((e) => {
    ctx.adapter.log(`[agentic-memory] context_monitor end failed: ${e}`)
  }).finally(() =>
    new Promise<void>((resolve, reject) => {
      const sessionChild = spawn(VENV, [MEMORY_SESSION_END], {
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env, MEMORY_HOOK_EVENT: "stop" },
      })
      sessionChild.stdin?.write(JSON.stringify({ session_id: sessionId }))
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
          ctx.adapter.log(`[agentic-memory] Session memory saved: ${stdout.trim()}`)
          sessionEndOk = true
          resolve()
        } else {
          reject(new Error(`Session-end save failed (exit ${code}): ${stderr.trim()}`))
        }
      })
      sessionChild.on("error", (err) => {
        clearTimeout(timeout)
        reject(err)
      })
    }).catch((e2) => ctx.adapter.log(`[agentic-memory] Session end save failed: ${e2}`))
  ).finally(() =>
    // Coordination: update project state, release locks, notify other agents
    hookEnabled("session:end:coordination", ["standard", "strict"])
      ? captureOutput([MEMORY_COORDINATION], JSON.stringify({ action: "session_end", agent_id: sessionId }), "coordination-end", ctx.adapter).catch(() => {})
      : Promise.resolve()
  )
}

export function injectSystemPrompt(ctx: HookContext): void {
  // The OpenCode SDK fires `experimental.chat.system.transform` before
  // each LLM call with `output.system: string[]`.  We push content
  // directly into that array — the adapter.injectIntoSystemPrompt is
  // a no-op placeholder; real injection goes through output.system.

  const output = (ctx as any).output as { system?: string[] } | undefined
  if (!output || !Array.isArray(output.system)) return

  // Agent contract: injected on every LLM call as a persistent reminder.
  const contract = loadAgentContract()
  if (contract) {
    output.system.push(contract)
  }

  const s = ctx.adapter.getState()
  if (s.sessionContext) {
    output.system.push(s.sessionContext)
    s.sessionContext = ""
  }
  if (s.proactiveContext) {
    output.system.push(s.proactiveContext)
    s.proactiveContext = ""
  }
}

export async function onCompacting(ctx: HookContext): Promise<void> {
  const sessionId = ctx.sessionId ?? "unknown"
  const output = ctx.output as { context: string[]; prompt?: string } | undefined

  if (!hookEnabled("session:compacting:memory-save", ["minimal"])) return
  if (!output) return

  let lastCompactionTime = 0
  try {
    if (fs.existsSync(STATE_FILE)) {
      const stateContent = fs.readFileSync(STATE_FILE, "utf8")
      const parsed = JSON.parse(stateContent)
      lastCompactionTime = parsed.last_compaction_time || 0
    }
  } catch { /* state file may not exist yet */ }

  const now = Date.now() / 1000
  const isThrottled = now - lastCompactionTime < 45

  if (isThrottled) {
    ctx.adapter.log("[agentic-memory] Skipping pre-compaction context save (throttled)")
    output.context.push("\n\n⚠️ COMPACTION SURVIVAL: A pre-compaction context note was saved. After compaction, search for 'pre-compaction context save' to recover what was happening before this compaction event.")
  } else {
    ctx.adapter.log("[agentic-memory] Context compaction detected — saving pre-compaction context")

    let messageCount = "0"
    try {
      if (fs.existsSync(STATE_FILE)) {
        const stateContent = fs.readFileSync(STATE_FILE, "utf8")
        const parsed = JSON.parse(stateContent)
        messageCount = String(parsed.total_tool_calls || parsed.tool_call_count || 0)
      }
    } catch { /* state file may not exist yet */ }

    // Blocking: snapshot raw events.jsonl before compaction destroys it.
    // Previously fire-and-forget, which meant snapshot failures were
    // invisible to the agent. Now we capture the JSON result and
    // surface any failure in the compaction warning that the agent
    // sees after compaction completes.
    let snapshotOk = true
    try {
      const snapshotRaw = await captureOutput(
        [MEMORY_PRECOMPACT_SNAPSHOT, JSON.stringify({ session_id: sessionId })],
        undefined,
        "precompact-snapshot",
        ctx.adapter
      )
      const snapshotParsed = JSON.parse(snapshotRaw.trim() || "{}")
      if (!snapshotParsed.ok) {
        snapshotOk = false
        ctx.adapter.log(`[agentic-memory] Pre-compaction snapshot failed: ${snapshotParsed.error || "unknown error"}`)
      }
    } catch (e) {
      snapshotOk = false
      ctx.adapter.log(`[agentic-memory] Pre-compaction snapshot error: ${e}`)
    }

    const result = await captureOutput([CONTEXT_MONITOR, "compact", "--session-id", sessionId, "--message-count", messageCount], undefined, "compact", ctx.adapter)
    ctx.adapter.log(`[agentic-memory] Pre-compaction context saved: ${result.trim()}`)

    const snapshotWarning = snapshotOk ? "" : " (snapshot failed — data may be incomplete)"
    output.context.push(`\n\n⚠️ COMPACTION SURVIVAL: A pre-compaction context note was saved.${snapshotWarning} After compaction, search for 'pre-compaction context save' to recover what was happening before this compaction event.`)
  }

  output.prompt = "Focus on preserving: 1) Current task status and progress, 2) Key decisions made, 3) Files created/modified, 4) Remaining work items, 5) Any security concerns flagged. Discard: verbose tool outputs, intermediate exploration, redundant file listings."
}
