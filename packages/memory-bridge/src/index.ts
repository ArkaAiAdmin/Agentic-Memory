export { MemoryBridgeClient, memoryBridge } from "./client.js";
export { memoryEventBus, kgEventBus } from "./events.js";
export type {
  JsonRpcRequest,
  JsonRpcResponse,
  JsonRpcError,
  JsonRpcNotification,
  MCPServerInfo,
  MCPToolInfo,
  BridgeEvent,
  HealthCheckResult,
  MemoryBridgeConfig,
  CoordinateAction,
  TaskParams,
  LockParams,
  MessageParams,
  ProjectStateParams,
  AgentInitOptions,
} from "./types.js";
export { DEFAULT_BRIDGE_CONFIG } from "./types.js";
