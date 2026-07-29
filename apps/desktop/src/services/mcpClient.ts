/**
 * External MCP Client
 *
 * Connects to external MCP (Model Context Protocol) servers and
 * merges their tools into the agent's tool registry.
 *
 * Supports stdio and HTTP/SSE transports with full JSON-RPC protocol.
 */

import type { Tool, ToolDefinition, JSONSchema } from "@ami/shared";

// ── JSON-RPC Types ────────────────────────────────────────────────────────

interface JsonRpcRequest {
  jsonrpc: "2.0";
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

interface JsonRpcResponse {
  jsonrpc: "2.0";
  id: number;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

interface JsonRpcNotification {
  jsonrpc: "2.0";
  method: string;
  params?: Record<string, unknown>;
}

// ── Types ────────────────────────────────────────────────────────────────

export interface McpServerConfig {
  id: string;
  name: string;
  transport: "stdio" | "sse";
  /** For stdio: the command to run (e.g. "npx @modelcontextprotocol/server-filesystem"). */
  command?: string;
  /** For stdio: arguments to pass. */
  args?: string[];
  /** For stdio: environment variables. */
  env?: Record<string, string>;
  /** For SSE: the server URL. */
  url?: string;
  /** Whether this server is enabled. */
  enabled: boolean;
}

export interface McpServerStatus {
  id: string;
  connected: boolean;
  tools: ToolDefinition[];
  error?: string;
}

// ── Connection State ─────────────────────────────────────────────────────

interface McpConnection {
  config: McpServerConfig;
  status: McpServerStatus;
  requestId: number;
  pendingRequests: Map<number, { resolve: (value: unknown) => void; reject: (reason: unknown) => void }>;
  processId?: string;
  eventSource?: EventSource;
  initialized: boolean;
  stdoutOffset: number;
}

const servers = new Map<string, McpServerConfig>();
const connections = new Map<string, McpConnection>();
let statusListener: ((statuses: McpServerStatus[]) => void) | null = null;

function notifyListener() {
  if (statusListener) {
    statusListener(getAllStatuses());
  }
}

// ── JSON-RPC Helpers ─────────────────────────────────────────────────────

function nextId(conn: McpConnection): number {
  return ++conn.requestId;
}

async function sendRequest(conn: McpConnection, method: string, params?: Record<string, unknown>): Promise<unknown> {
  const id = nextId(conn);
  const request: JsonRpcRequest = { jsonrpc: "2.0", id, method, params };

  return new Promise((resolve, reject) => {
    conn.pendingRequests.set(id, { resolve, reject });

    if (conn.config.transport === "stdio") {
      sendStdioRequest(conn, request);
    } else if (conn.config.transport === "sse" && conn.eventSource) {
      sendSseRequest(conn, request);
    } else {
      reject(new Error("Transport not available"));
    }

    setTimeout(() => {
      if (conn.pendingRequests.has(id)) {
        conn.pendingRequests.delete(id);
        reject(new Error(`Request timeout: ${method}`));
      }
    }, 30000);
  });
}

async function sendNotification(conn: McpConnection, method: string, params?: Record<string, unknown>): Promise<void> {
  const notification: JsonRpcNotification = { jsonrpc: "2.0", method, params };

  if (conn.config.transport === "stdio") {
    sendStdioNotification(conn, notification);
  } else if (conn.config.transport === "sse" && conn.eventSource) {
    sendSseNotification(conn, notification);
  }
}

// ── Stdio Transport ──────────────────────────────────────────────────────

async function sendStdioRequest(conn: McpConnection, request: JsonRpcRequest): Promise<void> {
  if (!conn.processId) throw new Error("No process ID");
  const { invoke } = await import("@tauri-apps/api/core");
  const payload = JSON.stringify(request) + "\n";
  await invoke("write_process_stdin", { processId: conn.processId, data: payload });
}

async function sendStdioNotification(conn: McpConnection, notification: JsonRpcNotification): Promise<void> {
  if (!conn.processId) throw new Error("No process ID");
  const { invoke } = await import("@tauri-apps/api/core");
  const payload = JSON.stringify(notification) + "\n";
  await invoke("write_process_stdin", { processId: conn.processId, data: payload });
}

async function pollStdioResponses(conn: McpConnection): Promise<void> {
  if (conn.config.transport !== "stdio" || !conn.processId) return;

  const { invoke } = await import("@tauri-apps/api/core");
  const stdout = await invoke<string>("get_stdout", { processId: conn.processId });

  const newData = stdout.slice(conn.stdoutOffset);
  conn.stdoutOffset = stdout.length;

  const lines = newData.split("\n");
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const message: JsonRpcResponse = JSON.parse(line);
      if (conn.pendingRequests.has(message.id)) {
        const { resolve, reject } = conn.pendingRequests.get(message.id)!;
        conn.pendingRequests.delete(message.id);
        if (message.error) {
          reject(new Error(message.error.message || "MCP error"));
        } else {
          resolve(message.result);
        }
      }
    } catch {
      // Not a valid JSON-RPC response, ignore
    }
  }
}

// ── SSE Transport ────────────────────────────────────────────────────────

function sendSseRequest(conn: McpConnection, request: JsonRpcRequest): void {
  if (!conn.eventSource) throw new Error("No SSE connection");
  const message = JSON.stringify(request);
  fetch(conn.config.url!, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: message,
  }).catch(() => {
    // SSE request failed, will be handled by timeout
  });
}

function sendSseNotification(conn: McpConnection, notification: JsonRpcNotification): void {
  if (!conn.eventSource) throw new Error("No SSE connection");
  const message = JSON.stringify(notification);
  fetch(conn.config.url!, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: message,
  }).catch(() => {});
}

function setupSseHandlers(conn: McpConnection): void {
  if (!conn.eventSource) return;

  conn.eventSource.onmessage = (event) => {
    try {
      const message: JsonRpcResponse = JSON.parse(event.data);
      if (conn.pendingRequests.has(message.id)) {
        const { resolve, reject } = conn.pendingRequests.get(message.id)!;
        conn.pendingRequests.delete(message.id);
        if (message.error) {
          reject(new Error(message.error.message || "MCP error"));
        } else {
          resolve(message.result);
        }
      }
    } catch {
      // Not a valid JSON-RPC response, ignore
    }
  };

  conn.eventSource.onerror = () => {
    conn.status.connected = false;
    conn.status.error = "SSE connection error";
    notifyListener();
  };
}

// ── MCP Protocol Implementation ─────────────────────────────────────────

async function mcpInitialize(conn: McpConnection): Promise<void> {
  const result = await sendRequest(conn, "initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "ami-desktop", version: "1.0.0" },
  });

  const caps = result as { capabilities?: { tools?: unknown } };
  if (caps?.capabilities?.tools) {
    conn.status.tools = await listTools(conn);
  }

  await sendNotification(conn, "notifications/initialized");
  conn.initialized = true;
}

async function listTools(conn: McpConnection): Promise<ToolDefinition[]> {
  const result = await sendRequest(conn, "tools/list");
  const parsed = result as { tools?: Array<{ name: string; description: string; inputSchema: JSONSchema }> };
  return (parsed.tools || []).map((tool) => ({
    name: tool.name,
    description: tool.description,
    inputSchema: tool.inputSchema,
  }));
}

async function callTool(conn: McpConnection, name: string, args: Record<string, unknown>): Promise<ToolDefinition["inputSchema"]> {
  const result = await sendRequest(conn, "tools/call", { name, arguments: args });
  return result as ToolDefinition["inputSchema"];
}

// ── Public API ───────────────────────────────────────────────────────────

/** Get all registered MCP server configs. */
export function getAllServers(): McpServerConfig[] {
  return Array.from(servers.values());
}

/** Get all MCP server statuses. */
export function getAllStatuses(): McpServerStatus[] {
  return Array.from(connections.values()).map((c) => c.status);
}

/** Subscribe to status changes. */
export function onMcpStatusChange(fn: (statuses: McpServerStatus[]) => void): () => void {
  statusListener = fn;
  return () => { statusListener = null; };
}

/** Add or update an MCP server config. */
export function addServer(config: McpServerConfig): void {
  servers.set(config.id, config);
  connections.set(config.id, {
    config,
    status: { id: config.id, connected: false, tools: [] },
    requestId: 0,
    pendingRequests: new Map(),
    initialized: false,
    stdoutOffset: 0,
  });
  notifyListener();
}

/** Remove an MCP server. */
export function removeServer(id: string): void {
  servers.delete(id);
  disconnectServer(id).then(() => connections.delete(id));
  notifyListener();
}

/** Connect to an MCP server and discover its tools. */
export async function connectServer(id: string): Promise<boolean> {
  const conn = connections.get(id);
  if (!conn || !conn.config.enabled) return false;

  try {
    if (conn.config.transport === "stdio" && conn.config.command) {
      const { invoke } = await import("@tauri-apps/api/core");
      const cmd = [conn.config.command, ...(conn.config.args || [])].join(" ");
      const processId = await invoke<string>("run_background", {
        command: cmd,
        cwd: ".",
      });
      conn.processId = processId;
      conn.stdoutOffset = 0;

      const pollInterval = setInterval(async () => {
        await pollStdioResponses(conn);
      }, 100);

      await mcpInitialize(conn);
      clearInterval(pollInterval);
    } else if (conn.config.transport === "sse" && conn.config.url) {
      const eventSource = new EventSource(conn.config.url);
      conn.eventSource = eventSource;
      setupSseHandlers(conn);
      await mcpInitialize(conn);
    }

    conn.status.connected = true;
    conn.status.error = undefined;
  } catch (err) {
    conn.status.connected = false;
    conn.status.error = err instanceof Error ? err.message : "Connection failed";
    conn.status.tools = [];
  }

  notifyListener();
  return conn.status.connected;
}

/** Disconnect from an MCP server. */
export async function disconnectServer(id: string): Promise<void> {
  const conn = connections.get(id);
  if (!conn) return;

  if (conn.eventSource) {
    conn.eventSource.close();
    conn.eventSource = undefined;
  }

  if (conn.processId) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("kill_process", { processId: conn.processId });
    } catch {
      // Process may have already exited
    }
    conn.processId = undefined;
  }

  conn.status.connected = false;
  conn.status.tools = [];
  conn.initialized = false;
  conn.pendingRequests.clear();
  notifyListener();
}

/**
 * Get all tools from connected MCP servers as Tool objects
 * that can be registered in the ToolRegistry.
 */
export function getMcpTools(): Tool[] {
  const tools: Tool[] = [];

  for (const [id, conn] of connections) {
    if (!conn.status.connected) continue;

    for (const def of conn.status.tools) {
      tools.push({
        name: `mcp_${id}_${def.name}`,
        description: def.description,
        category: "agent",
        inputSchema: def.inputSchema,
        execute: async (args, _ctx) => {
          try {
            const result = await callTool(conn, def.name, args);
            return {
              success: true,
              output: JSON.stringify(result),
              preview: JSON.stringify(result).slice(0, 500),
            };
          } catch (err) {
            return {
              success: false,
              output: "",
              preview: "",
              error: err instanceof Error ? err.message : `MCP tool ${def.name} failed`,
            };
          }
        },
      });
    }
  }

  return tools;
}
