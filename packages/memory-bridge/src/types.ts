/**
 * Memory Bridge Types
 *
 * TypeScript types mirroring the Python agentic-memory models.
 * These re-export from @ami/shared and add bridge-specific types
 * for the JSON-RPC protocol between TypeScript and Python.
 *
 * Generated from the Python Pydantic models where possible.
 */

// Re-export core memory types from shared
export type {
  NoteId,
  SessionId,
  AgentId,
  MemoryNote,
  NoteCategory,
  SearchQuery,
  SearchResult,
  SavePayload,
  Session,
  SessionBriefing,
  DecisionThread,
  ThreadEvent,
  ThreadEventType,
  ContextWindowState,
  KGEntity,
  KGFact,
  KGNode,
  KGEdge,
  BeliefAssertion,
  MemoryEvent,
  KGEvent,
  HealthStatus,
  Unsubscribe,
} from "@ami/shared";

// ── JSON-RPC Protocol Types ──────────────────────────────────────────────

export interface JsonRpcRequest {
  jsonrpc: "2.0";
  id: number;
  method: string;
  params?: unknown;
}

export interface JsonRpcResponse {
  jsonrpc: "2.0";
  id: number;
  result?: unknown;
  error?: JsonRpcError;
}

export interface JsonRpcError {
  code: number;
  message: string;
  data?: unknown;
}

export interface JsonRpcNotification {
  jsonrpc: "2.0";
  method: string;
  params?: unknown;
}

// ── MCP Protocol Types ───────────────────────────────────────────────────

export interface MCPServerInfo {
  name: string;
  version: string;
  capabilities: string[];
}

export interface MCPToolInfo {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

// ── Bridge-Specific Event Types ──────────────────────────────────────────

export type BridgeEvent =
  | { type: "bridge.started"; pid: number }
  | { type: "bridge.stopped"; exitCode: number | null }
  | { type: "bridge.error"; error: string }
  | { type: "bridge.health"; status: HealthCheckResult };

export interface HealthCheckResult {
  alive: boolean;
  latencyMs: number;
  memoryUsageMb?: number;
  uptimeSeconds: number;
}

// ── Memory Bridge Configuration ──────────────────────────────────────────

export interface MemoryBridgeConfig {
  memoryDir: string;
  pythonPath?: string;
  transport: "stdio" | "http";
  httpPort?: number;
  startupTimeoutMs: number;
  healthCheckIntervalMs: number;
}

export const DEFAULT_BRIDGE_CONFIG: MemoryBridgeConfig = {
  memoryDir: "",
  transport: "stdio",
  startupTimeoutMs: 30_000,
  healthCheckIntervalMs: 60_000,
};

// ── Coordination & Multi-Agent Types ────────────────────────────────────

export type CoordinateAction =
  | "create_task"
  | "claim_task"
  | "update_task_status"
  | "release_task"
  | "complete_task"
  | "list_tasks"
  | "lock_file"
  | "unlock_file"
  | "check_lock"
  | "send_message"
  | "read_messages"
  | "get_project_state"
  | "update_project_state";

export interface TaskParams {
  project_id?: string;
  task_type?: string;
  description?: string;
  assigned_to?: string;
  task_id?: string;
  status?: string;
}

export interface LockParams {
  file_path: string;
}

export interface MessageParams {
  to_agent?: string;
  message_type?: string;
  payload?: Record<string, unknown>;
}

export interface ProjectStateParams {
  project_id: string;
  key?: string;
  value?: unknown;
}

export interface AgentInitOptions {
  displayName?: string;
  parentAgent?: string;
  namespace?: string;
}

