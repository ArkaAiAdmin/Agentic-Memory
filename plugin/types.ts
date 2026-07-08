export interface HarnessAdapter {
  readonly log: (msg: string) => void
  readonly eventName: (event: string) => string
  readonly injectIntoSystemPrompt: (lines: string[]) => void
  readonly getState: () => { sessionContext: string; proactiveContext: string }
  readonly spawn: (args: string[], label: string) => Promise<string>
  readonly fireAndForget: (args: string[], label: string) => void
}

export interface HookContext {
  readonly adapter: HarnessAdapter
  readonly toolName?: string
  readonly toolArgs?: Record<string, unknown>
  readonly sessionId?: string
  readonly output?: unknown
}
