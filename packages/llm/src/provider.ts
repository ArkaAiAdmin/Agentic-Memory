/**
 * LLM Provider Layer
 *
 * Abstracts LLM communication through a Python LiteLLM subprocess.
 * The TypeScript layer never talks to LLM APIs directly — it always
 * goes through the Python LiteLLM bridge for unified tool-calling
 * normalization across 100+ providers.
 *
 * Architecture:
 *   TypeScript (IDE) <--JSON-RPC--> Python (LiteLLM bridge) <--HTTP--> LLM APIs
 *
 * Process management is handled by the Rust backend via Tauri IPC.
 * The TypeScript layer polls stdout/stderr and writes to stdin via IPC.
 */

import type {
  ChatParams,
  ChatChunk,
} from "@ami/shared";

// ── Provider Interface ────────────────────────────────────────────────────

export interface LLMProvider {
  /** Send a message with tool definitions, get a streaming response. */
  chat(params: ChatParams): AsyncIterable<ChatChunk>;

  /** Check if this provider supports tool/function calling. */
  supportsTool(): boolean;

  /** Check if this provider supports streaming. */
  supportsStreaming(): boolean;

  /** Check if this provider supports vision (image input). */
  supportsVision(): boolean;

  /** Maximum context window in tokens. */
  maxContextTokens(): number;

  /** Maximum output tokens. */
  maxOutputTokens(): number;

  /** Initialize the provider (no-op for HTTP providers, starts subprocess for bridge). */
  start(): Promise<void>;

  /** Shutdown the provider (no-op for HTTP providers, kills subprocess for bridge). */
  stop(): Promise<void>;
}

export interface ProviderConfig {
  type: "openai" | "anthropic" | "google" | "lmstudio" | "ollama" | "litellm";
  apiKey?: string;
  baseUrl?: string;
  model?: string;
}

export { createProvider, providerRegistry, PROVIDER_DEFAULTS } from "./providers.js";
export type { ProviderConfig as HttpProviderConfig } from "./providers.js";
export {
  OpenAIProvider,
  AnthropicProvider,
  GoogleProvider,
  LMStudioProvider,
  OllamaProvider,
  LiteLLMProxyProvider,
} from "./providers.js";

// ── IPC Client ─────────────────────────────────────────────────────────────

async function invokeCommand<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  try {
    if (typeof window === "undefined" || (!(window as any).__TAURI_INTERNALS__ && !(window as any).__TAURI__)) {
      return getMockFallback<T>(cmd, args);
    }
    const { invoke } = await import("@tauri-apps/api/core");
    return await (invoke as <R>(command: string, payload?: Record<string, unknown>) => Promise<R>)<T>(cmd, args);
  } catch {
    return getMockFallback<T>(cmd, args);
  }
}

function getMockFallback<T>(cmd: string, _args?: Record<string, unknown>): T {
  switch (cmd) {
    case "run_background":
      return "mock-llm-proc" as unknown as T;
    case "get_stdout":
    case "get_stderr":
      return "" as unknown as T;
    case "is_process_alive":
      return true as unknown as T;
    case "write_process_stdin":
      return undefined as unknown as T;
    case "kill_process":
      return undefined as unknown as T;
    default:
      return undefined as unknown as T;
  }
}

const ipcProcess = {
  runBackground: (command: string, cwd: string) => invokeCommand<string>("run_background", { command, cwd }),
  getStdout: (processId: string) => invokeCommand<string>("get_stdout", { processId }),
  getStderr: (processId: string) => invokeCommand<string>("get_stderr", { processId }),
  isAlive: (processId: string) => invokeCommand<boolean>("is_process_alive", { processId }),
  writeStdin: (processId: string, data: string) => invokeCommand<void>("write_process_stdin", { processId, data }),
  kill: (processId: string) => invokeCommand<void>("kill_process", { processId }),
};

// ── Bridge Script Resolution ──────────────────────────────────────────────

function getBridgeCommandSync(): string {
  try {
    const { fileURLToPath } = require("node:url");
    const { dirname, join } = require("node:path");
    const __dirname = dirname(fileURLToPath(import.meta.url));
    const scriptPath = join(__dirname, "../../scripts/litellm_bridge.py");
    return `python3 "${scriptPath}"`;
  } catch {
    return "python3 -m agentic_memory.llm.litellm_bridge";
  }
}

const BRIDGE_COMMAND = getBridgeCommandSync();

// ── LiteLLM Bridge Provider ──────────────────────────────────────────────

/**
 * LiteLLM subprocess bridge.
 * Spawns a Python process running LiteLLM via the Rust backend and
 * communicates via JSON-RPC over stdin/stdout.
 */
export class LiteLLMBridgeProvider implements LLMProvider {
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
  private pollHandle: ReturnType<typeof setInterval> | null = null;
  private lastStdoutLen = 0;
  private lastStderrLen = 0;
  private _started = false;

  private modelLimits: Map<string, { context: number; output: number }> =
    new Map([
      ["gpt-4o", { context: 128000, output: 16384 }],
      ["gpt-4o-mini", { context: 128000, output: 16384 }],
      ["claude-sonnet-4-20250514", { context: 200000, output: 8192 }],
      ["claude-3-5-sonnet-20241022", { context: 200000, output: 8192 }],
      ["gemini-2.0-flash", { context: 1048576, output: 8192 }],
    ]);

  get isRunning(): boolean {
    return this._started;
  }

  async start(): Promise<void> {
    if (this._started) return;

    const cwd =
      (globalThis as any).process?.env?.HOME ??
      (globalThis as any).process?.env?.USERPROFILE ??
      "/";

    this.processId = await ipcProcess.runBackground(BRIDGE_COMMAND, cwd);
    this.lastStdoutLen = 0;
    this.lastStderrLen = 0;

    // Start polling stdout/stderr
    this.pollHandle = setInterval(async () => {
      if (!this.processId) return;
      try {
        const [stdout, stderr, alive] = await Promise.all([
          ipcProcess.getStdout(this.processId),
          ipcProcess.getStderr(this.processId),
          ipcProcess.isAlive(this.processId),
        ]);

        const newStdout = stdout.slice(this.lastStdoutLen);
        if (newStdout) {
          this.handleStdout(newStdout);
          this.lastStdoutLen = stdout.length;
        }

        const newStderr = stderr.slice(this.lastStderrLen);
        if (newStderr) {
          console.error("[LiteLLM] stderr:", newStderr.trim());
          this.lastStderrLen = stderr.length;
        }

        if (!alive && this._started) {
          console.warn("[LiteLLM] Process exited unexpectedly");
          this._started = false;
          this.rejectAllPending("Process exited");
          if (this.pollHandle) clearInterval(this.pollHandle);
        }
      } catch {
        // Polling errors are non-fatal
      }
    }, 50);

    // MCP initialize handshake
    try {
      await this.sendRequest("initialize", {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "ami-ide", version: "0.1.0" },
      });

      if (this.processId) {
        const initNotification =
          JSON.stringify({
            jsonrpc: "2.0",
            method: "notifications/initialized",
          }) + "\n";
        await ipcProcess.writeStdin(this.processId, initNotification);
      }

      this._started = true;
    } catch (err) {
      if (this.pollHandle) {
        clearInterval(this.pollHandle);
        this.pollHandle = null;
      }
      throw err;
    }
  }

  async stop(): Promise<void> {
    if (!this.processId) return;

    if (this.pollHandle) {
      clearInterval(this.pollHandle);
      this.pollHandle = null;
    }

    try {
      await ipcProcess.kill(this.processId);
    } catch {
      // Best-effort kill
    }

    this._started = false;
    this.processId = null;
    this.buffer = "";
    this.lastStdoutLen = 0;
    this.lastStderrLen = 0;
  }

  async *chat(params: ChatParams): AsyncIterable<ChatChunk> {
    const response = await this.sendRequest("chat", {
      model: params.model,
      messages: params.messages,
      tools: params.tools.map((t) => ({
        name: t.name,
        description: t.description,
        input_schema: t.inputSchema,
      })),
      system_prompt: params.systemPrompt,
      temperature: params.temperature,
      max_tokens: params.maxTokens,
      stop: params.stop,
      stream: true,
    });

    const resp = response as any;
    if (resp && typeof resp[Symbol.asyncIterator] === "function") {
      for await (const chunk of resp) {
        yield chunk as ChatChunk;
      }
    } else if (response) {
      yield { type: "text", text: String(response) };
      yield { type: "done", reason: "stop" };
    }
  }

  supportsTool(): boolean {
    return true;
  }

  supportsStreaming(): boolean {
    return true;
  }

  supportsVision(): boolean {
    return true;
  }

  maxContextTokens(): number {
    return 128000;
  }

  maxOutputTokens(): number {
    return 16384;
  }

  // ── Internal ──────────────────────────────────────────────────────────

  private sendRequest(
    method: string,
    params: Record<string, unknown>,
  ): Promise<unknown> {
    return new Promise((resolve, reject) => {
      if (!this.processId) {
        reject(new Error("LiteLLM process not running"));
        return;
      }

      const id = ++this.requestId;
      const request = {
        jsonrpc: "2.0",
        id,
        method,
        params,
      };

      this.pendingRequests.set(id, { resolve, reject });

      const line = JSON.stringify(request) + "\n";

      ipcProcess
        .writeStdin(this.processId!, line)
        .then(() => {
          setTimeout(() => {
            if (this.pendingRequests.has(id)) {
              this.pendingRequests.delete(id);
              reject(new Error(`Request ${id} (${method}) timed out`));
            }
          }, 120_000);
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
            pending.reject(new Error(message.error.message));
          } else {
            pending.resolve(message.result);
          }
        }
      } catch {
        // Not JSON — might be a log line
      }
    }
  }

  private rejectAllPending(reason: string): void {
    for (const [, pending] of this.pendingRequests) {
      pending.reject(new Error(reason));
    }
    this.pendingRequests.clear();
  }
}

// ── Tool Calling Normalization ────────────────────────────────────────────
// Re-exported from tool-calling.ts for backward compatibility
export {
  normalizeToolsForProvider,
  normalizeToolCallResponse,
} from "./tool-calling.js";
