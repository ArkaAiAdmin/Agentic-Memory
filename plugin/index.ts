/**
 * agentic-memory OpenCode plugin — thin adapter
 *
 * This is the ONLY file that imports from @opencode-ai/plugin.
 * It translates OpenCode's event names and input/output types into
 * plain function calls on the hook implementation.
 *
 * The actual logic lives in ./agentic-memory-hooks.ts which knows
 * nothing about OpenCode's plugin system.
 */

import type { PluginInput } from "@opencode-ai/plugin"
import {
  startSession,
  onToolAfter,
  beforeTool,
  onIdle,
  endSession,
  injectSystemPrompt,
  onCompacting,
} from "./agentic-memory-hooks.js"

export default async function AgenticMemoryPlugin(
  _input: PluginInput
): Promise<Record<string, unknown>> {
  const log = (msg: string) => _input.client.app.log({ body: { service: "agentic-memory", level: "debug" as const, message: msg } }).catch(() => {})

  return {
    "tool.execute.after": async (input: { tool: string; args?: Record<string, unknown> }, output: unknown) => {
      await onToolAfter(input.tool, input.args, output, log)
    },

    "tool.execute.before": async (input: { tool: string; args?: Record<string, unknown> }) => {
      await beforeTool(input.tool, input.args, log)
    },

    "session.created": async () => {
      await startSession(log)
    },

    "session.idle": async () => {
      onIdle(log)
    },

    "session.deleted": async (input: { sessionID?: string }) => {
      await endSession(input.sessionID || "unknown", log)
    },

    "experimental.chat.system.transform": async (
      _input: { sessionID?: string; model: { providerID: string; modelID: string } },
      output: { system: string[] }
    ) => {
      injectSystemPrompt(output.system)
    },

    "experimental.session.compacting": async (
      input: { sessionID: string },
      output: { context: string[]; prompt?: string }
    ) => {
      await onCompacting(input.sessionID || "unknown", output, log)
    },
  }
}
