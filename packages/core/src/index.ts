// Core package — the kernel of the Memory-First Agent IDE

export { ConversationLoop } from "./conversation/loop.js";
export type { ConversationConfig } from "./conversation/loop.js";

export { ContextBuilder } from "./context/builder.js";
export type { RulesLoader } from "./context/builder.js";

export { ToolRegistry } from "./tools/registry.js";
export { ToolExecutor } from "./tools/executor.js";
export { ToolMiddlewareChain } from "./tools/middleware.js";
export type { MiddlewareFn, MiddlewareNext } from "./tools/middleware.js";

export { SessionManager } from "./session/manager.js";

export { AgentOrchestrator } from "./agent/orchestrator.js";

export { WorkspaceManager } from "./workspace/manager.js";
