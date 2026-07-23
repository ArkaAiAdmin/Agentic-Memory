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
 */

import type {
  ChatParams,
  ChatChunk,
  ToolDefinition,
  Message,
} from "@ami/shared";
import type { ChildProcess } from "node:child_process";

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
}

// ── LiteLLM Bridge Provider ──────────────────────────────────────────────

/**
 * LiteLLM subprocess bridge.
 * Spawns a Python process running LiteLLM and communicates via JSON-RPC.
 */
export class LiteLLMBridgeProvider implements LLMProvider {
  private process: ChildProcess | null = null;
  private requestId = 0;
  private pendingRequests = new Map<
    number,
    {
      resolve: (value: unknown) => void;
      reject: (error: Error) => void;
    }
  >();
  private buffer = "";
  private modelLimits: Map<string, { context: number; output: number }> =
    new Map([
      ["gpt-4o", { context: 128000, output: 16384 }],
      ["gpt-4o-mini", { context: 128000, output: 16384 }],
      ["claude-sonnet-4-20250514", { context: 200000, output: 8192 }],
      ["claude-3-5-sonnet-20241022", { context: 200000, output: 8192 }],
      ["gemini-2.0-flash", { context: 1048576, output: 8192 }],
    ]);

  async start(): Promise<void> {
    // Dynamic imports — Node.js APIs not available at bundle time in Vite
    const { spawn } = await import("node:child_process");
    const { resolve } = await import("node:path");
    const { fileURLToPath } = await import("node:url");

    // The LiteLLM bridge script
    const __filename = fileURLToPath(import.meta.url);
    const __dirname = resolve(__filename, "..");
    const bridgeScript = resolve(__dirname, "../../../packages/llm/scripts/litellm_bridge.py");

    const env: Record<string, string> = {};
    try {
      Object.assign(env, (globalThis as any).process.env);
    } catch { /* browser context */ }
    env.PYTHONUNBUFFERED = "1";

    this.process = spawn("python3", [bridgeScript], {
      stdio: ["pipe", "pipe", "pipe"],
      env,
    });

    this.process.stdout?.on("data", (chunk: Buffer | string) => {
      this.handleStdout(chunk.toString("utf-8"));
    });

    this.process.stderr?.on("data", (chunk: Buffer | string) => {
      console.error("[LiteLLM] stderr:", chunk.toString("utf-8").trim());
    });

    this.process.on("exit", (code: number | null) => {
      console.warn(`[LiteLLM] Process exited with code ${code}`);
      this.process = null;
      this.rejectAllPending("Process exited");
    });
  }

  async stop(): Promise<void> {
    if (!this.process) return;
    this.process.kill("SIGTERM");
    this.process = null;
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

    // Response is an async iterable of chunks
    const resp = response as any;
    if (resp && typeof resp[Symbol.asyncIterator] === "function") {
      for await (const chunk of resp) {
        yield chunk as ChatChunk;
      }
    } else if (response) {
      // Non-streaming fallback
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
    return 128000; // Default, overridden per-model
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
      if (!this.process?.stdin) {
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
      this.process.stdin.write(line, (err: Error | null | undefined) => {
        if (err) {
          this.pendingRequests.delete(id);
          reject(err);
        }
      });

      setTimeout(() => {
        if (this.pendingRequests.has(id)) {
          this.pendingRequests.delete(id);
          reject(new Error(`Request ${id} (${method}) timed out`));
        }
      }, 120_000); // 2 minute timeout for LLM calls
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
