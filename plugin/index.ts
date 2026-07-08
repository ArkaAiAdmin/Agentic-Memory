/**
 * agentic-memory OpenCode plugin — thin adapter
 *
 * The ONLY file that imports from @opencode-ai/plugin.
 * All hook logic lives in ./agentic-memory-hooks.ts and is agnostic
 * to any harness.
 */

import type { PluginInput } from "@opencode-ai/plugin"
import type { HarnessAdapter, HookContext } from "./types.js"
import { state } from "./agentic-memory-hooks.js"
import * as hooks from "./agentic-memory-hooks.js"

const HANDLER_MAP: Record<string, keyof typeof hooks> = {
  "tool.execute.after": "onToolAfter",
  "tool.execute.before": "beforeTool",
  "session.created": "startSession",
  "session.idle": "onIdle",
  "session.deleted": "endSession",
  "experimental.chat.system.transform": "injectSystemPrompt",
  "experimental.session.compacting": "onCompacting",
}

function buildAdapter(input: PluginInput): HarnessAdapter {
  const log = (msg: string) =>
    input.client.app.log({ body: { service: "agentic-memory", level: "debug" as const, message: msg } }).catch(() => {})

  return {
    log,
    eventName: (_event: string) => "opencode",
    injectIntoSystemPrompt: (_lines: string[]) => { void _lines },
    getState: () => state,
    spawn: async (args, label) => {
      const { spawn } = await import("node:child_process")
      return await new Promise<string>((resolve) => {
        const child = spawn(input.venvPython ?? process.execPath, args, { stdio: ["pipe", "pipe", "pipe"] })
        let out = ""
        child.stdout?.on("data", (d: Buffer) => { out += d })
        child.on("close", (code) => resolve(code === 0 ? out : ""))
        child.on("error", () => resolve(""))
      })
    },
    fireAndForget: async (args, label) => {
      const { spawn } = await import("node:child_process")
      const child = spawn(input.venvPython ?? process.execPath, args, { stdio: ["ignore", "ignore", "pipe"], detached: true })
      child.on("error", (err) => log(`[agentic-memory] [opencode] ${label} error: ${err}`))
      child.on("close", (code) => {
        if (code !== 0) log(`[agentic-memory] [opencode] ${label} exited ${code}`)
      })
      child.unref()
    },
  }
}

export default async function AgenticMemoryPlugin(
  _input: PluginInput
): Promise<Record<string, unknown>> {
  const adapter = buildAdapter(_input)
  const result: Record<string, unknown> = {}

  for (const [event, fnName] of Object.entries(HANDLER_MAP)) {
    result[event] = async (raw: Record<string, unknown>) => {
      const ctx: HookContext = { adapter, ...raw }
      return (hooks[fnName] as (ctx: HookContext) => void | Promise<void>)(ctx)
    }
  }
  return result
}
