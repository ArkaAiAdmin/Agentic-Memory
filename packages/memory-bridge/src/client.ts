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

export class MemoryBridgeError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = "MemoryBridgeError";
  }
}

export class MemoryBridgeNotAvailableError extends MemoryBridgeError {
  constructor() {
    super(
      "Memory bridge is not available because Tauri is not detected. " +
      "Run `cargo tauri dev` to start the desktop app with full functionality.",
    );
    this.name = "MemoryBridgeNotAvailableError";
  }
}

async function invokeCommand<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (typeof window === "undefined") {
    throw new MemoryBridgeNotAvailableError();
  }
  const hasTauri = (window as any).__TAURI_INTERNALS__ || (window as any).__TAURI__;
  if (!hasTauri) {
    throw new MemoryBridgeNotAvailableError();
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await (invoke as <R>(command: string, payload?: Record<string, unknown>) => Promise<R>)<T>(cmd, args);
  } catch (err) {
    throw new MemoryBridgeError(`Tauri command '${cmd}' failed`, err);
  }
}

const ipcMemoryBridge = {
  start: (memoryDir: string, agentId?: string) =>
    invokeCommand<string>("start_memory_bridge", { memoryDir, agentId }),
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
  private _starting = false;
  private memoryDir: string | null = null;
  private _agentId: string = "IDE";
  private pollHandle: ReturnType<typeof setInterval> | null = null;
  private lastStdoutLen = 0;
  private lastStderrLen = 0;
  private readonly maxRetries: number;

  constructor(maxRetries: number = 2) {
    this.maxRetries = maxRetries;
  }

  get isRunning(): boolean {
    return this._started;
  }

  /**
   * Start the memory subprocess via the Rust backend.
   * Spawns a new process with unique agent ID to avoid conflicts.
   * Retries up to 3 times with exponential backoff.
   */
  /** Get the current agent ID this bridge was started with. */
  get agentId(): string {
    return this._agentId;
  }

  async start(memoryDir: string, agentId?: string): Promise<void> {
    if (this._started || this._starting) return;
    this._starting = true;
    this.memoryDir = memoryDir;
    if (agentId) this._agentId = agentId;

    let lastError: Error | null = null;

    for (let attempt = 1; attempt <= this.maxRetries; attempt++) {
      try {
        console.log(`[MemoryBridge] Starting (attempt ${attempt}/${this.maxRetries}) with memoryDir:`, memoryDir, "agentId:", this._agentId);
        const processId = await ipcMemoryBridge.start(memoryDir, this._agentId);
        console.log("[MemoryBridge] Process started, ID:", processId);
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
              console.warn("[MemoryBridge] stderr:", newStderr.trim());
              this.lastStderrLen = stderr.length;
            }

            // Detect unexpected exit → attempt reconnection
            if (!alive && this._started) {
              console.warn("[MemoryBridge] Process exited unexpectedly, attempting reconnect...");
              this._started = false;
              this.rejectAllPending("Process exited");
              if (this.pollHandle) clearInterval(this.pollHandle);
              this.pollHandle = null;
              // Auto-reconnect after 2s
              setTimeout(() => this.reconnect(), 2000);
            }
          } catch {
            // Polling errors are non-fatal; next tick may recover
          }
        }, 500);

        // Wait for Python process to finish importing modules before sending
        // the MCP initialize request. Give it 5s — imports are fast from venv.
        await new Promise(r => setTimeout(r, 5000));

        // Capture stderr + alive status BEFORE attempting handshake
        let preHandshakeStderr = "";
        let preHandshakeAlive = true;
        if (this.processId) {
          try {
            const [stderrSnap, aliveSnap] = await Promise.all([
              ipcProcess.getStderr(this.processId),
              ipcProcess.isAlive(this.processId),
            ]);
            preHandshakeStderr = stderrSnap.slice(this.lastStderrLen);
            preHandshakeAlive = aliveSnap;
          } catch { /* best-effort */ }
        }

        if (!preHandshakeAlive) {
          const errDetail = preHandshakeStderr
            ? `\nPython stderr:\n${preHandshakeStderr.trim()}`
            : "\n(no stderr captured)";
          throw new Error(`Python process exited during startup${errDetail}`);
        }

        // MCP handshake with timeout (15s — should be enough after import)
        await Promise.race([
          this.initialize(),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error("MCP handshake timed out")), 15_000),
          ),
        ]);
        this._starting = false;
        this._started = true;
        return; // Success
      } catch (err) {
        lastError = err instanceof Error ? err : new Error(String(err));
        // Capture stderr for diagnostic — this is the KEY missing piece
        let stderrContent = "";
        if (this.processId) {
          try {
            stderrContent = await ipcProcess.getStderr(this.processId);
          } catch { /* process may already be gone */ }
        }
        const stderrSnippet = stderrContent
          ? `\nPython stderr:\n${stderrContent.slice(-2000).trim()}`
          : "";
        console.error(`[MemoryBridge] Attempt ${attempt} failed:`, lastError.message + stderrSnippet);
        // Enrich the error so the user sees stderr in the UI
        if (stderrSnippet && !lastError.message.includes("Python stderr")) {
          lastError = new Error(lastError.message + stderrSnippet);
        }

        if (this.pollHandle) clearInterval(this.pollHandle);
        this.pollHandle = null;

        // Kill the orphaned process so it releases the flock
        if (this.processId) {
          try {
            await ipcMemoryBridge.stop(this.processId);
            console.log("[MemoryBridge] Killed orphan process:", this.processId);
          } catch {
            // Best-effort cleanup
          }
        }
        this.processId = null;

        if (attempt < this.maxRetries) {
          const delay = Math.pow(2, attempt) * 1000; // 2s, 4s
          console.log(`[MemoryBridge] Retrying in ${delay}ms...`);
          await new Promise(r => setTimeout(r, delay));
        }
      }
    }

    // All retries exhausted
    this._starting = false;
    this._started = false;
    throw lastError ?? new Error("Failed to start memory bridge after retries");
  }

  /**
   * Attempt to reconnect if the process dies mid-session.
   */
  private async reconnect(): Promise<void> {
    if (this._started || !this.memoryDir) return;
    console.log("[MemoryBridge] Reconnecting...");
    try {
      await this.start(this.memoryDir);
      console.log("[MemoryBridge] Reconnected successfully");
    } catch (err) {
      console.error("[MemoryBridge] Reconnection failed:", err);
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
      const result = await this.callTool("memory_health_check", {});
      // memory_health_check returns: { db: { accessible }, vec_index: {...}, schema_version, ... }
      const dbAccessible = result?.db?.accessible ?? (result?.raw ? true : false);
      const hasErrors = result?.vec_index?.error || result?.fts?.error;
      return {
        status: dbAccessible ? (hasErrors ? "degraded" : "healthy") : "unhealthy",
        python_process: this._started,
        db_accessible: dbAccessible,
        embeddings_loaded: !result?.vec_index?.error,
        version: result?.schema_version ? `v${result.schema_version}` : "unknown",
      };
    } catch {
      // Even if health check fails, if the process is running, report degraded (not unhealthy)
      return {
        status: this._started ? "degraded" : "unhealthy",
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
      include_global: true, // Search across all agents, not just this agent's namespace
    });

    // The MCP tool returns human-readable text (not JSON), so callTool wraps it as { raw: "..." }.
    // Parse the raw text output to extract result items.
    let results: SearchResult[] = [];
    console.log("[MemoryBridge] search result shape:", JSON.stringify(result).slice(0, 300));
    if (result.results && Array.isArray(result.results)) {
      // Structured JSON response (ideal)
      results = result.results;
    } else if (result.raw && typeof result.raw === "string") {
      // Parse text output: lines like "1. [note_id] category/title — content..."
      results = this.parseTextSearchResults(result.raw);
    }

    memoryEventBus.emit({
      type: "memory.searched",
      query: query.query,
      resultCount: results.length,
    });

    return results;
  }

  /**
   * Parse human-readable search output from the MCP tool into structured results.
   * Format: numbered lines like "1. [id] category/slug — content preview..."
   */
  private parseTextSearchResults(raw: string): SearchResult[] {
    const results: SearchResult[] = [];
    // Match lines like: "  1. [abc123] lessons/some-title (score=0.85, importance=3)"
    // or "  1. content preview here... [category: lessons, tags: ...]"
    const lines = raw.split("\n");
    let currentResult: Partial<SearchResult> | null = null;

    for (const line of lines) {
      // Match numbered result line: "  N. [note_id] ..."
      const match = line.match(/^\s*\d+\.\s*\[([^\]]+)\]\s*(.+)/);
      if (match) {
        if (currentResult?.note_id) {
          results.push(currentResult as SearchResult);
        }
        const noteId = match[1];
        const rest = match[2];
        // Try to split "category/title — content"
        const catMatch = rest.match(/^(\w+)\/([^—–-]+)[—–-]\s*(.*)/);
        currentResult = {
          note_id: noteId,
          category: (catMatch?.[1] ?? "lessons") as any,
          content: catMatch?.[3]?.trim() ?? rest,
          score: 0,
          tags: [],
          source: "fts",
          metadata: {},
          created_at: Date.now(),
        } as Partial<SearchResult>;
      } else if (currentResult && line.trim() && !line.startsWith("Search results") && !line.startsWith("---")) {
        // Continuation line — append to content
        currentResult.content = (currentResult.content || "") + " " + line.trim();
      }
    }
    if (currentResult?.note_id) {
      results.push(currentResult as SearchResult);
    }
    return results;
  }

  async save(payload: SavePayload): Promise<NoteId> {
    if (!payload.content) {
      console.warn("[MemoryBridge] save called without content, returning 'unknown'");
      return "unknown" as NoteId;
    }
    const result = await this.callTool("memory_save", {
      content: payload.content,
      category: payload.category,
      tags: payload.tags ?? [],
      importance: payload.importance,
    });

    let noteId: string;
    if (result && typeof result.note_id === "string") {
      noteId = result.note_id;
    } else if (result && typeof result.raw === "string") {
      const m = result.raw.match(/saved (?:memory|note):?\s*\S+\/(.*?)(?:\.md)?(?:\s|$)/i);
      noteId = m ? m[1] : result.raw;
    } else {
      noteId = "unknown";
    }

    memoryEventBus.emit({
      type: "memory.saved",
      noteId,
      category: payload.category,
    });

    return noteId as NoteId;
  }

  async recall(
    query?: string,
    sessionId?: string,
  ): Promise<{ memories: SearchResult[]; context: string }> {
    const result = await this.callTool("memory_recall", {
      query,
      session_id: sessionId,
      include_global: true,
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
    const result = await this.callTool("memory_review_beliefs", {
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

  // ── Multi-Agent Coordination Operations ─────────────────────────────

  /**
   * Execute multi-agent coordination tool (memory_coordinate).
   * Supports actions: create_task, claim_task, update_task_status, release_task, complete_task, list_tasks, lock_file, unlock_file, check_lock, send_message, read_messages, get_project_state, update_project_state.
   */
  async coordinate(action: string, params: Record<string, unknown> = {}): Promise<any> {
    return await this.callTool("memory_coordinate", {
      action,
      ...params,
    });
  }

  async coordinateTask(
    action: "create_task" | "claim_task" | "update_task_status" | "release_task" | "complete_task" | "list_tasks",
    params: Record<string, unknown> = {},
  ): Promise<any> {
    return await this.coordinate(action, params);
  }

  async coordinateLock(
    action: "lock_file" | "unlock_file" | "check_lock",
    params: Record<string, unknown> = {},
  ): Promise<any> {
    return await this.coordinate(action, params);
  }

  async coordinateMessage(
    action: "send_message" | "read_messages",
    params: Record<string, unknown> = {},
  ): Promise<any> {
    return await this.coordinate(action, params);
  }

  async coordinateState(
    action: "get_project_state" | "update_project_state",
    params: Record<string, unknown> = {},
  ): Promise<any> {
    return await this.coordinate(action, params);
  }

  // ── Memory Sharing Operations ──────────────────────────────────────────

  /**
   * Share a memory entry with another agent or globally (memory_share CORE tool).
   */
  async shareMemory(noteId: string, shareWith?: string): Promise<any> {
    return await this.callTool("memory_share", {
      note_id: noteId,
      share_with: shareWith ?? "",
      action: "share",
    });
  }

  // ── Maintenance Router Operations (ADMIN Tools) ───────────────────────

  /**
   * Execute admin/maintenance operations via the memory_maintenance CORE tool router.
   */
  async maintenance(operation: string, params: Record<string, unknown> = {}): Promise<any> {
    return await this.callTool("memory_maintenance", {
      operation,
      ...params,
    });
  }

  async listAgents(): Promise<any> {
    return await this.maintenance("agent_list");
  }

  async initAgent(
    agentId: string,
    options?: { displayName?: string; parentAgent?: string; namespace?: string },
  ): Promise<any> {
    return await this.maintenance("agent_init", {
      agent_id: agentId,
      display_name: options?.displayName ?? "",
      parent_agent: options?.parentAgent ?? "",
      namespace: options?.namespace ?? "",
    });
  }

  async clearAgent(): Promise<any> {
    return await this.maintenance("agent_clear", {
      confirm: true,
    });
  }

  async listSharedMemories(shareAgentId?: string): Promise<any> {
    return await this.maintenance("shared_list", {
      share_agent_id: shareAgentId,
    });
  }

  async importSharedMemory(sharedId: string, targetAgentId: string): Promise<any> {
    return await this.maintenance("shared_import", {
      shared_id: sharedId,
      target_agent_id: targetAgentId,
      confirm: true,
    });
  }

  // ── Delete ────────────────────────────────────────────────────────────

  async delete(noteId: string): Promise<void> {
    await this.callTool("memory_delete", { note_id: noteId });
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

  async callTool(
    name: string,
    args: Record<string, unknown>,
  ): Promise<any> {
    const response = await this.sendRequest(MCP_TOOLS_CALL, {
      name,
      arguments: args,
    });

    const resp = response as any;
    // DEBUG: log raw MCP response shape
    console.log("[MemoryBridge] callTool raw response keys:", Object.keys(resp), "content type:", typeof resp?.content, "content length:", resp?.content?.length);
    if (resp?.content?.[0]) {
      console.log("[MemoryBridge] content[0]:", JSON.stringify(resp.content[0]).slice(0, 500));
    }
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

  private async sendRequest(
    method: string,
    params?: unknown,
  ): Promise<unknown> {
    const id = ++this.requestId;
    const request: JsonRpcRequest = {
      jsonrpc: "2.0",
      id,
      method,
      params,
    };

    // Use stdio transport via Rust IPC
    return new Promise((resolve, reject) => {
      if (!this.processId) {
        reject(new Error("Memory process not running"));
        return;
      }

      this.pendingRequests.set(id, { resolve, reject });

      const line = JSON.stringify(request) + "\n";

      ipcProcess
        .writeStdin(this.processId, line)
        .then(() => {
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
    for (const [, pending] of this.pendingRequests) {
      pending.reject(new Error(reason));
    }
    this.pendingRequests.clear();
  }
}

// Singleton instance
export const memoryBridge = new MemoryBridgeClient();
