/**
 * Memory Bridge Client
 *
 * Thin TypeScript wrapper over Tauri IPC commands that manages the
 * Python agentic-memory MCP subprocess. The actual process spawning,
 * stdout/stderr buffering, and stdin writes happen in the Rust backend.
 *
 * This file retains the JSON-RPC / MCP protocol logic (request routing,
 * response matching, notification dispatch) but no longer imports
 * node:child_process or node:fs.
 */

import type {
  SearchQuery,
  SearchResult,
  SavePayload,
  NoteId,
  SessionBriefing,
  KGNode,
  KGEdge,
  HealthStatus,
  MemoryEvent,
  KGEvent,
  Unsubscribe,
  BeliefAssertion,
} from "@ami/shared";
import { memoryEventBus, kgEventBus } from "./events.js";

async function invokeCommand<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  try {
    if (typeof window === "undefined" || (!(window as any).__TAURI_INTERNALS__ && !(window as any).__TAURI__)) {
      return "mock-proc-1" as unknown as T;
    }
    const { invoke } = await import("@tauri-apps/api/core");
    return await (invoke as <R>(command: string, payload?: Record<string, unknown>) => Promise<R>)<T>(cmd, args);
  } catch {
    return "mock-proc-1" as unknown as T;
  }
}

const ipcMemoryBridge = {
  start: (memoryDir: string) => invokeCommand<string>("start_memory_bridge", { memoryDir }),
  stop: (processId: string) => invokeCommand<void>("stop_memory_bridge", { processId }),
};

const ipcProcess = {
  getStdout: (processId: string) => invokeCommand<string>("get_stdout", { processId }),
  getStderr: (processId: string) => invokeCommand<string>("get_stderr", { processId }),
  isAlive: (processId: string) => invokeCommand<boolean>("is_process_alive", { processId }),
  writeStdin: (processId: string, data: string) => invokeCommand<void>("write_process_stdin", { processId, data }),
};

// ── JSON-RPC Types ────────────────────────────────────────────────────────

interface JsonRpcRequest {
  jsonrpc: "2.0";
  id: number;
  method: string;
  params?: unknown;
}

interface JsonRpcNotification {
  jsonrpc: "2.0";
  method: string;
  params?: unknown;
}

// ── MCP Protocol Constants ────────────────────────────────────────────────

const MCP_INITIALIZE = "initialize";
const MCP_TOOLS_CALL = "tools/call";
const MCP_TOOLS_LIST = "tools/list";

// ── Memory Bridge Implementation ──────────────────────────────────────────

export class MemoryBridgeClient {
  private processId: string | null = null;
  private requestId = 0;
  private pendingRequests = new Map<
    number,
    {
      resolve: (value: unknown) => void;
      reject: (error: Error) => void;
    }
  >();
  private buffer = "";
  private _started = false;
  private memoryDir: string | null = null;
  private pollHandle: ReturnType<typeof setInterval> | null = null;
  private lastStdoutLen = 0;
  private lastStderrLen = 0;

  get isRunning(): boolean {
    return this._started;
  }

  /**
   * Start the memory subprocess via the Rust backend.
   */
  async start(memoryDir: string): Promise<void> {
    if (this._started) return;
    this.memoryDir = memoryDir;

    const processId = await ipcMemoryBridge.start(memoryDir);
    this.processId = processId;
    this.lastStdoutLen = 0;
    this.lastStderrLen = 0;

    // Start polling stdout/stderr from Rust
    this.pollHandle = setInterval(async () => {
      if (!this.processId) return;
      try {
        const [stdout, stderr, alive] = await Promise.all([
          ipcProcess.getStdout(this.processId),
          ipcProcess.getStderr(this.processId),
          ipcProcess.isAlive(this.processId),
        ]);

        // Feed new stdout chunks into the JSON-RPC parser
        const newStdout = stdout.slice(this.lastStdoutLen);
        if (newStdout) {
          this.handleStdout(newStdout);
          this.lastStdoutLen = stdout.length;
        }

        // Log new stderr
        const newStderr = stderr.slice(this.lastStderrLen);
        if (newStderr) {
          console.error("[MemoryBridge] stderr:", newStderr.trim());
          this.lastStderrLen = stderr.length;
        }

        // Detect unexpected exit
        if (!alive && this._started) {
          console.warn("[MemoryBridge] Process exited unexpectedly");
          this._started = false;
          this.rejectAllPending("Process exited");
          if (this.pollHandle) clearInterval(this.pollHandle);
        }
      } catch {
        // Polling errors are non-fatal; next tick may recover
      }
    }, 50);

    // MCP handshake with timeout
    try {
      await Promise.race([
        this.initialize(),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error("MCP handshake timed out")), 10_000),
        ),
      ]);
      this._started = true;
    } catch (err) {
      console.error("[MemoryBridge] Failed to initialize:", err);
      this._started = false;
      if (this.pollHandle) clearInterval(this.pollHandle);
      throw err;
    }
  }

  /**
   * Stop the memory subprocess gracefully via the Rust backend.
   */
  async stop(): Promise<void> {
    if (!this.processId) return;

    if (this.pollHandle) {
      clearInterval(this.pollHandle);
      this.pollHandle = null;
    }

    try {
      await ipcMemoryBridge.stop(this.processId);
    } catch {
      // Best-effort stop; Rust may have already killed it
    }

    this._started = false;
    this.processId = null;
    this.buffer = "";
    this.lastStdoutLen = 0;
    this.lastStderrLen = 0;
  }

  /**
   * Health check — verify the memory system is responsive.
   */
  async healthCheck(): Promise<HealthStatus> {
    try {
      const result = await this.callTool("memory_health", {});
      return {
        status: result.healthy ? "healthy" : "degraded",
        python_process: this._started,
        db_accessible: result.db_accessible ?? true,
        embeddings_loaded: result.embeddings_loaded ?? true,
        version: result.version ?? "unknown",
      };
    } catch {
      return {
        status: "unhealthy",
        python_process: this._started,
        db_accessible: false,
        embeddings_loaded: false,
        version: "unknown",
      };
    }
  }

  // ── Core Memory Operations ────────────────────────────────────────────

  async search(query: SearchQuery): Promise<SearchResult[]> {
    const result = await this.callTool("memory_search", {
      query: query.query,
      mode: query.mode ?? "hybrid",
      limit: query.limit ?? 15,
      category: query.category,
      tags: query.tags,
      min_importance: query.min_importance,
      session_id: query.session_id,
      project: query.project,
    });

    memoryEventBus.emit({
      type: "memory.searched",
      query: query.query,
      resultCount: result.results?.length ?? 0,
    });

    return result.results ?? [];
  }

  async save(payload: SavePayload): Promise<NoteId> {
    const result = await this.callTool("memory_save", {
      content: payload.content,
      category: payload.category,
      tags: payload.tags ?? [],
      metadata: payload.metadata ?? {},
      importance: payload.importance,
      project: payload.project,
    });

    memoryEventBus.emit({
      type: "memory.saved",
      noteId: result.note_id,
      category: payload.category,
    });

    return result.note_id;
  }

  async recall(
    query?: string,
    sessionId?: string,
  ): Promise<{ memories: SearchResult[]; context: string }> {
    const result = await this.callTool("memory_recall", {
      query,
      session_id: sessionId,
    });
    return result;
  }

  async sessionStart(query: string): Promise<SessionBriefing> {
    const result = await this.callTool("memory_session_start", {
      query,
    });

    memoryEventBus.emit({
      type: "session.started",
      sessionId: result.session_id,
    });

    return result as SessionBriefing;
  }

  async sessionEnd(sessionId: string, summary: string): Promise<void> {
    await this.callTool("memory_session_end", {
      session_id: sessionId,
      summary,
    });
  }

  async compactSession(sessionId: string): Promise<{ context: string }> {
    memoryEventBus.emit({
      type: "session.compacting",
      sessionId,
    });

    return await this.callTool("memory_compact", {
      session_id: sessionId,
    });
  }

  // ── Knowledge Graph Operations ────────────────────────────────────────

  async graphExplore(query: string): Promise<KGNode[]> {
    const result = await this.callTool("memory_graph", {
      query,
    });
    return result.nodes ?? [];
  }

  async graphTraverse(
    start: string,
    pattern: string,
    depth: number,
  ): Promise<KGEdge[]> {
    const result = await this.callTool("memory_graph_traverse", {
      start,
      pattern,
      depth,
    });
    return result.edges ?? [];
  }

  // ── Belief Operations ─────────────────────────────────────────────────

  async reviewBeliefs(params: {
    minConfidence?: number;
  }): Promise<BeliefAssertion[]> {
    const result = await this.callTool("memory_beliefs", {
      min_confidence: params.minConfidence ?? 0.0,
    });
    return result.beliefs ?? [];
  }

  // ── Audit ─────────────────────────────────────────────────────────────

  async audit(params: { hours?: number }): Promise<unknown[]> {
    const result = await this.callTool("memory_audit", {
      hours: params.hours ?? 2,
    });
    return result.entries ?? [];
  }

  // ── Event Subscription ────────────────────────────────────────────────

  onMemoryEvent(handler: (event: MemoryEvent) => void): Unsubscribe {
    return memoryEventBus.onAny(handler);
  }

  onKGEvent(handler: (event: KGEvent) => void): Unsubscribe {
    return kgEventBus.onAny(handler);
  }

  // ── Internal: JSON-RPC Protocol ───────────────────────────────────────

  private async initialize(): Promise<void> {
    await this.sendRequest(MCP_INITIALIZE, {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: {
        name: "agentic-memory-ide",
        version: "0.1.0",
      },
    });
  }

  private async callTool(
    name: string,
    args: Record<string, unknown>,
  ): Promise<any> {
    const response = await this.sendRequest(MCP_TOOLS_CALL, {
      name,
      arguments: args,
    });

    const resp = response as any;
    if (resp?.content) {
      const textContent = resp.content.find(
        (c: any) => c.type === "text",
      );
      if (textContent?.text) {
        try {
          return JSON.parse(textContent.text);
        } catch {
          return { raw: textContent.text };
        }
      }
    }

    return response ?? {};
  }

  private sendRequest(
    method: string,
    params?: unknown,
  ): Promise<unknown> {
    return new Promise((resolve, reject) => {
      if (!this.processId) {
        reject(new Error("Memory process not running"));
        return;
      }

      const id = ++this.requestId;
      const request: JsonRpcRequest = {
        jsonrpc: "2.0",
        id,
        method,
        params,
      };

      this.pendingRequests.set(id, { resolve, reject });

      const line = JSON.stringify(request) + "\n";

      // Write to process stdin via Rust IPC
      ipcProcess
        .writeStdin(this.processId, line)
        .then(() => {
          // Timeout after 60 seconds
          setTimeout(() => {
            if (this.pendingRequests.has(id)) {
              this.pendingRequests.delete(id);
              reject(new Error(`Request ${id} (${method}) timed out`));
            }
          }, 60_000);
        })
        .catch((err: unknown) => {
          this.pendingRequests.delete(id);
          reject(err);
        });
    });
  }

  private handleStdout(data: string): void {
    this.buffer += data;

    let newlineIdx: number;
    while ((newlineIdx = this.buffer.indexOf("\n")) !== -1) {
      const line = this.buffer.slice(0, newlineIdx).trim();
      this.buffer = this.buffer.slice(newlineIdx + 1);

      if (!line) continue;

      try {
        const message = JSON.parse(line);

        if (message.id !== undefined && this.pendingRequests.has(message.id)) {
          const pending = this.pendingRequests.get(message.id)!;
          this.pendingRequests.delete(message.id);

          if (message.error) {
            pending.reject(
              new Error(
                message.error.message ?? "Unknown JSON-RPC error",
              ),
            );
          } else {
            pending.resolve(message.result);
          }
        } else if (message.method) {
          this.handleNotification(message);
        }
      } catch (err) {
        console.error("[MemoryBridge] Failed to parse message:", line, err);
      }
    }
  }

  private handleNotification(notification: JsonRpcNotification): void {
    const { method, params } = notification;

    switch (method) {
      case "notifications/memory/saved":
        memoryEventBus.emit({
          type: "memory.saved",
          noteId: (params as any)?.note_id ?? "",
          category: (params as any)?.category ?? "",
        });
        break;
      case "notifications/kg/entity_created":
        kgEventBus.emit({
          type: "entity.created",
          entity: (params as any)?.entity,
        });
        break;
      case "notifications/kg/fact_extract":
        kgEventBus.emit({
          type: "fact.extracted",
          fact: (params as any)?.fact,
        });
        break;
      default:
        console.debug("[MemoryBridge] Unknown notification:", method);
    }
  }

  private rejectAllPending(reason: string): void {
    for (const [id, pending] of this.pendingRequests) {
      pending.reject(new Error(reason));
    }
    this.pendingRequests.clear();
  }
}

// Singleton instance
export const memoryBridge = new MemoryBridgeClient();
