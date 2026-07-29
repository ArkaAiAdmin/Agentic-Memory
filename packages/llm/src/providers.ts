/**
 * Direct HTTP LLM Providers
 *
 * Provider implementations that call LLM APIs directly via fetch().
 * These work in the Tauri webview without Node.js APIs or Python subprocesses.
 *
 * Supported providers:
 *   - OpenAI
 *   - Anthropic
 *   - Google Gemini
 *   - LM Studio (local, OpenAI-compatible)
 *   - Ollama (local, OpenAI-compatible)
 *
 * Architecture:
 *   TypeScript (IDE) <--HTTP/SSE--> LLM API
 *
 * Each provider implements the LLMProvider interface from provider.ts.
 */

import type { LLMProvider } from "./provider.js";
import type { ChatParams, ChatChunk } from "@ami/shared";
import {
  parseSSELine,
  normalizeOpenAIChunk,
  normalizeAnthropicChunk,
  normalizeGoogleChunk,
} from "./streaming.js";

// ── Configuration ──────────────────────────────────────────────────────────

export interface ProviderConfig {
  type: "openai" | "anthropic" | "google" | "lmstudio" | "ollama" | "litellm";
  apiKey?: string;
  baseUrl?: string;
  model?: string;
}

export const PROVIDER_DEFAULTS: Record<ProviderConfig["type"], { baseUrl: string; defaultModel: string }> = {
  openai: { baseUrl: "https://api.openai.com/v1", defaultModel: "gpt-4o" },
  anthropic: { baseUrl: "https://api.anthropic.com/v1", defaultModel: "claude-sonnet-4-20250514" },
  google: { baseUrl: "https://generativelanguage.googleapis.com/v1beta", defaultModel: "gemini-2.0-flash" },
  lmstudio: { baseUrl: "http://localhost:1234/v1", defaultModel: "local-model" },
  ollama: { baseUrl: "http://localhost:11434/v1", defaultModel: "llama3.1" },
  litellm: { baseUrl: "http://localhost:4000", defaultModel: "gpt-4o" },
};

// ── Injectable fetch transport ───────────────────────────────────────────
//
// Cloud LLM endpoints reject browser-origin requests (CORS) and expose keys to
// the webview network stack. The desktop app injects a Rust-backed fetch (via
// `setFetchImpl`) that proxies the call through the native backend. In non-Tauri
// contexts (tests, web) this falls back to the global `fetch`.

export type FetchImpl = (url: string, init?: RequestInit) => Promise<Response>;

let fetchImpl: FetchImpl = (url, init) => fetch(url, init);

/** Override the HTTP transport used by all providers (e.g. a Tauri proxy). */
export function setFetchImpl(fn: FetchImpl): void {
  fetchImpl = fn;
}

/** The active HTTP transport. Defaults to the global `fetch`. */
export function getFetchImpl(): FetchImpl {
  return fetchImpl;
}

// ── SSE Stream Utilities ───────────────────────────────────────────────────

async function* sseStream(
  response: Response,
  parse: (data: unknown) => ChatChunk | null,
): AsyncIterable<ChatChunk> {
  const reader = response.body?.getReader();
  if (!reader) {
    yield { type: "error", error: "No response body" };
    return;
  }

  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith(":")) continue;

        const parsed = parseSSELine(trimmed);
        if (parsed === null) continue;

        // Handle [DONE] sentinel
        if ((parsed as any).type === "done") {
          yield { type: "done", reason: "stop" } as ChatChunk;
          continue;
        }

        const chunk = parse(parsed);
        if (chunk) yield chunk;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// ── Base HTTP Provider ─────────────────────────────────────────────────────

abstract class BaseHttpProvider implements LLMProvider {
  protected apiKey: string;
  protected baseUrl: string;

  constructor(apiKey: string, baseUrl: string) {
    this.apiKey = apiKey;
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  abstract chat(params: ChatParams): AsyncIterable<ChatChunk>;
  abstract getModelLimits(model: string): { context: number; output: number };

  supportsTool(): boolean { return true; }
  supportsStreaming(): boolean { return true; }
  supportsVision(): boolean { return false; }

  maxContextTokens(): number { return 128000; }
  maxOutputTokens(): number { return 16384; }

  async start(): Promise<void> {}
  async stop(): Promise<void> {}

  protected async postJson(url: string, body: unknown): Promise<Response> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (this.apiKey) {
      headers["Authorization"] = `Bearer ${this.apiKey}`;
    }
    const res = await fetchImpl(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "Unknown error");
      throw new Error(`HTTP ${res.status}: ${text}`);
    }
    return res;
  }

  protected buildMessages(params: ChatParams): Array<{ role: string; content: string }> {
    const messages: Array<{ role: string; content: string }> = [];
    if (params.systemPrompt) {
      messages.push({ role: "system", content: params.systemPrompt });
    }
    for (const msg of params.messages) {
      messages.push({ role: msg.role, content: msg.content });
    }
    return messages;
  }
}

// ── OpenAI Provider ────────────────────────────────────────────────────────

export class OpenAIProvider extends BaseHttpProvider {
  getModelLimits(model: string): { context: number; output: number } {
    const limits: Record<string, { context: number; output: number }> = {
      "gpt-4o": { context: 128000, output: 16384 },
      "gpt-4o-mini": { context: 128000, output: 16384 },
      "gpt-4-turbo": { context: 128000, output: 4096 },
      "o1-preview": { context: 128000, output: 32768 },
      "o1-mini": { context: 128000, output: 65536 },
    };
    return limits[model] ?? { context: 128000, output: 4096 };
  }

  async *chat(params: ChatParams): AsyncIterable<ChatChunk> {
    const url = `${this.baseUrl}/chat/completions`;
    const body = {
      model: params.model,
      messages: this.buildMessages(params),
      stream: true,
      temperature: params.temperature ?? 0.7,
      max_tokens: params.maxTokens ?? 4096,
      stop: params.stop,
      tools: params.tools.length
        ? params.tools.map((t) => ({
            type: "function",
            function: {
              name: t.name,
              description: t.description,
              parameters: t.inputSchema,
            },
          }))
        : undefined,
    };

    const response = await this.postJson(url, body);
    yield* sseStream(response, normalizeOpenAIChunk);
  }
}

// ── Anthropic Provider ─────────────────────────────────────────────────────

export class AnthropicProvider extends BaseHttpProvider {
  getModelLimits(model: string): { context: number; output: number } {
    const limits: Record<string, { context: number; output: number }> = {
      "claude-sonnet-4-20250514": { context: 200000, output: 16384 },
      "claude-3-5-sonnet-20241022": { context: 200000, output: 8192 },
      "claude-3-5-haiku-20241022": { context: 200000, output: 8192 },
    };
    return limits[model] ?? { context: 200000, output: 4096 };
  }

  async *chat(params: ChatParams): AsyncIterable<ChatChunk> {
    const url = `${this.baseUrl}/messages`;
    const body = {
      model: params.model,
      max_tokens: params.maxTokens ?? 4096,
      system: params.systemPrompt || undefined,
      messages: params.messages.map((m) => {
        if (m.role === "tool") {
          return {
            role: "user",
            content: [{
              type: "tool_result",
              tool_use_id: m.tool_call_id,
              content: m.content,
            }],
          };
        }
        if (m.role === "assistant" && m.tool_calls?.length) {
          return {
            role: m.role,
            content: m.tool_calls.map((tc) => ({
              type: "tool_use",
              id: tc.id,
              name: tc.name,
              input: tc.arguments,
            })),
          };
        }
        return {
          role: m.role,
          content: m.content,
        };
      }),
      stream: true,
      tools: params.tools.length
        ? params.tools.map((t) => ({
            name: t.name,
            description: t.description,
            input_schema: t.inputSchema,
          }))
        : undefined,
    };

    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "anthropic-version": "2023-06-01",
      "x-api-key": this.apiKey,
    };

    const res = await fetchImpl(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "Unknown error");
      throw new Error(`HTTP ${res.status}: ${text}`);
    }

    const reader = res.body?.getReader();
    if (!reader) {
      yield { type: "error", error: "No response body" };
      return;
    }

    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || trimmed.startsWith(":")) continue;
          if (!trimmed.startsWith("data: ")) continue;

          const dataStr = trimmed.slice(6);
          if (dataStr === "[DONE]") {
            yield { type: "done", reason: "stop" } as ChatChunk;
            continue;
          }

          try {
            const data = JSON.parse(dataStr);
            const chunk = normalizeAnthropicChunk(data);
            if (chunk) yield chunk;
          } catch {
            // skip invalid JSON
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }
}

// ── Google Provider ────────────────────────────────────────────────────────

export class GoogleProvider extends BaseHttpProvider {
  getModelLimits(model: string): { context: number; output: number } {
    const limits: Record<string, { context: number; output: number }> = {
      "gemini-2.0-flash": { context: 1048576, output: 8192 },
      "gemini-1.5-pro": { context: 2097152, output: 8192 },
    };
    return limits[model] ?? { context: 128000, output: 4096 };
  }

  async *chat(params: ChatParams): AsyncIterable<ChatChunk> {
    const modelPath = params.model.replace(/^models\//, "");
    const url = `${this.baseUrl}/models/${modelPath}:streamGenerateContent?alt=sse&key=${encodeURIComponent(this.apiKey)}`;

    const contents = params.messages.map((m) => ({
      role: m.role === "assistant" ? "model" : m.role,
      parts: [{ text: m.content }],
    }));

    if (params.systemPrompt) {
      contents.unshift({
        role: "user",
        parts: [{ text: `[System instruction: ${params.systemPrompt}]` }],
      });
    }

    const body: Record<string, unknown> = {
      contents,
      generationConfig: {
        temperature: params.temperature ?? 0.7,
        maxOutputTokens: params.maxTokens ?? 4096,
        stopSequences: params.stop,
      },
    };

    if (params.tools.length) {
      body.tools = [
        {
          functionDeclarations: params.tools.map((t) => ({
            name: t.name,
            description: t.description,
            parameters: t.inputSchema,
          })),
        },
      ];
    }

    const res = await fetchImpl(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "Unknown error");
      throw new Error(`HTTP ${res.status}: ${text}`);
    }

    yield* sseStream(res, normalizeGoogleChunk);
  }
}

// ── LM Studio Provider ─────────────────────────────────────────────────────

export class LMStudioProvider extends BaseHttpProvider {
  constructor(apiKey: string, baseUrl: string) {
    // Auto-append /v1 if user provides bare host URL (e.g. http://192.168.0.6:1234)
    let normalizedUrl = baseUrl.replace(/\/$/, "");
    if (!normalizedUrl.endsWith("/v1")) {
      normalizedUrl += "/v1";
    }
    super(apiKey, normalizedUrl);
  }

  getModelLimits(_model: string): { context: number; output: number } {
    return { context: 32768, output: 4096 };
  }

  async *chat(params: ChatParams): AsyncIterable<ChatChunk> {
    const url = `${this.baseUrl}/chat/completions`;
    const body = {
      model: params.model,
      messages: this.buildMessages(params),
      stream: true,
      temperature: params.temperature ?? 0.7,
      max_tokens: params.maxTokens ?? 4096,
      stop: params.stop,
      tools: params.tools.length
        ? params.tools.map((t) => ({
            type: "function",
            function: {
              name: t.name,
              description: t.description,
              parameters: t.inputSchema,
            },
          }))
        : undefined,
    };

    const response = await this.postJson(url, body);
    yield* sseStream(response, normalizeOpenAIChunk);
  }
}

// ── Ollama Provider ────────────────────────────────────────────────────────

export class OllamaProvider extends BaseHttpProvider {
  getModelLimits(_model: string): { context: number; output: number } {
    return { context: 32768, output: 4096 };
  }

  async *chat(params: ChatParams): AsyncIterable<ChatChunk> {
    const url = `${this.baseUrl}/chat/completions`;
    const body = {
      model: params.model,
      messages: this.buildMessages(params),
      stream: true,
      options: {
        temperature: params.temperature ?? 0.7,
        num_predict: params.maxTokens ?? 4096,
        stop: params.stop,
      },
      tools: params.tools.length
        ? params.tools.map((t) => ({
            type: "function",
            function: {
              name: t.name,
              description: t.description,
              parameters: t.inputSchema,
            },
          }))
        : undefined,
    };

    const response = await this.postJson(url, body);
    yield* sseStream(response, normalizeOpenAIChunk);
  }
}

// ── LiteLLM Proxy Provider ─────────────────────────────────────────────────

export class LiteLLMProxyProvider extends BaseHttpProvider {
  getModelLimits(_model: string): { context: number; output: number } {
    return { context: 32768, output: 4096 };
  }

  async *chat(params: ChatParams): AsyncIterable<ChatChunk> {
    const url = `${this.baseUrl}/v1/chat/completions`;
    const body = {
      model: params.model,
      messages: this.buildMessages(params),
      stream: true,
      temperature: params.temperature ?? 0.7,
      max_tokens: params.maxTokens ?? 4096,
      stop: params.stop,
      tools: params.tools.length
        ? params.tools.map((t) => ({
            type: "function",
            function: {
              name: t.name,
              description: t.description,
              parameters: t.inputSchema,
            },
          }))
        : undefined,
    };

    const response = await this.postJson(url, body);
    yield* sseStream(response, normalizeOpenAIChunk);
  }
}

// ── Provider Registry ──────────────────────────────────────────────────────

export const providerRegistry: Record<
  ProviderConfig["type"],
  (config: ProviderConfig) => LLMProvider
> = {
  openai: (config) => new OpenAIProvider(config.apiKey || "", config.baseUrl || PROVIDER_DEFAULTS.openai.baseUrl),
  anthropic: (config) => new AnthropicProvider(config.apiKey || "", config.baseUrl || PROVIDER_DEFAULTS.anthropic.baseUrl),
  google: (config) => new GoogleProvider(config.apiKey || "", config.baseUrl || PROVIDER_DEFAULTS.google.baseUrl),
  lmstudio: (config) => new LMStudioProvider(config.apiKey || "", config.baseUrl || PROVIDER_DEFAULTS.lmstudio.baseUrl),
  ollama: (config) => new OllamaProvider(config.apiKey || "", config.baseUrl || PROVIDER_DEFAULTS.ollama.baseUrl),
  litellm: (config) => new LiteLLMProxyProvider(config.apiKey || "", config.baseUrl || PROVIDER_DEFAULTS.litellm.baseUrl),
};

export function createProvider(config: ProviderConfig): LLMProvider {
  const factory = providerRegistry[config.type];
  if (!factory) {
    throw new Error(`Unknown provider type: ${config.type}`);
  }
  return factory(config);
}
