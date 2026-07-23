/**
 * SSE/Streaming Response Handler
 *
 * Handles SSE streams from LiteLLM, normalizes across providers,
 * and emits typed chunks. Handles provider-specific quirks:
 * - OpenAI function calling
 * - Anthropic tool_use
 * - Google function_call
 */

import type { ChatChunk, ToolCall } from "@ami/shared";

// ── SSE Line Parser ──────────────────────────────────────────────────────

/**
 * Parse a single SSE line into a data payload.
 * SSE format: "data: {json}\n\n"
 */
export function parseSSELine(line: string): unknown | null {
  const trimmed = line.trim();
  if (!trimmed || trimmed.startsWith(":")) return null; // comment or empty
  if (trimmed === "data: [DONE]") return { type: "done", reason: "stop" };
  if (trimmed.startsWith("data: ")) {
    try {
      return JSON.parse(trimmed.slice(6));
    } catch {
      return null;
    }
  }
  // Some providers send raw JSON without "data: " prefix
  try {
    return JSON.parse(trimmed);
  } catch {
    return null;
  }
}

// ── Stream Chunk Normalizers ─────────────────────────────────────────────

/**
 * Normalize an OpenAI streaming chunk.
 */
export function normalizeOpenAIChunk(data: any): ChatChunk | null {
  if (!data?.choices?.[0]) return null;
  const choice = data.choices[0];
  const delta = choice.delta ?? {};

  // Finish reason
  if (choice.finish_reason) {
    return { type: "done", reason: choice.finish_reason };
  }

  // Tool calls (streamed incrementally)
  if (delta.tool_calls?.length) {
    const tc = delta.tool_calls[0];
    return {
      type: "tool_call",
      id: tc.id ?? "",
      name: tc.function?.name ?? "",
      arguments: tc.function?.arguments ?? "",
    };
  }

  // Text content
  if (delta.content) {
    return { type: "text", text: delta.content };
  }

  return null;
}

/**
 * Normalize an Anthropic streaming event.
 */
export function normalizeAnthropicChunk(data: any): ChatChunk | null {
  if (!data) return null;

  // Content block start
  if (data.type === "content_block_start") {
    const block = data.content_block;
    if (block?.type === "tool_use") {
      return {
        type: "tool_call",
        id: block.id ?? "",
        name: block.name ?? "",
        arguments: "",
      };
    }
  }

  // Content block delta (text)
  if (data.type === "content_block_delta") {
    const delta = data.delta;
    if (delta?.type === "text_delta") {
      return { type: "text", text: delta.text ?? "" };
    }
    if (delta?.type === "input_json_delta") {
      return {
        type: "tool_call",
        id: "",
        name: "",
        arguments: delta.partial_json ?? "",
      };
    }
  }

  // Message stop
  if (data.type === "message_stop") {
    return { type: "done", reason: "stop" };
  }

  return null;
}

/**
 * Normalize a Google Gemini streaming chunk.
 */
export function normalizeGoogleChunk(data: any): ChatChunk | null {
  if (!data?.candidates?.[0]) return null;
  const candidate = data.candidates[0];

  // Finish reason
  if (candidate.finishReason && candidate.finishReason !== "STOP") {
    return { type: "done", reason: candidate.finishReason.toLowerCase() };
  }
  if (candidate.finishReason === "STOP") {
    return { type: "done", reason: "stop" };
  }

  const parts = candidate.content?.parts ?? [];
  for (const part of parts) {
    if (part.text) {
      return { type: "text", text: part.text };
    }
    if (part.functionCall) {
      return {
        type: "tool_call",
        id: `call_${Date.now()}`,
        name: part.functionCall.name ?? "",
        arguments: JSON.stringify(part.functionCall.args ?? {}),
      };
    }
  }

  return null;
}

// ── Accumulator for streamed tool calls ──────────────────────────────────

/**
 * Accumulates streamed tool call fragments into complete ToolCall objects.
 * Handles the incremental nature of streaming tool calls where arguments
 * arrive as JSON string fragments.
 */
export class ToolCallAccumulator {
  private calls = new Map<
    number,
    { id: string; name: string; argsBuffer: string }
  >();

  /**
   * Feed a chunk. Returns a completed ToolCall when a tool call is finished.
   */
  feed(chunk: ChatChunk): ToolCall | null {
    if (chunk.type !== "tool_call") return null;

    // Find or create accumulator for this tool call
    let idx = this.calls.size - 1;
    if (chunk.id) {
      // New tool call starting
      idx = this.calls.size;
      this.calls.set(idx, { id: chunk.id, name: chunk.name, argsBuffer: "" });
    }

    const entry = this.calls.get(idx);
    if (!entry) return null;

    // Append arguments fragment
    entry.argsBuffer += chunk.arguments;
    if (chunk.name) entry.name = chunk.name;
    if (chunk.id) entry.id = chunk.id;

    // Try to parse accumulated args
    try {
      const args = JSON.parse(entry.argsBuffer);
      this.calls.delete(idx);
      return { id: entry.id, name: entry.name, arguments: args };
    } catch {
      // Not yet complete JSON
      return null;
    }
  }

  /** Reset the accumulator. */
  reset(): void {
    this.calls.clear();
  }
}

// ── Provider-aware stream normalizer ─────────────────────────────────────

export type StreamProvider = "openai" | "anthropic" | "google";

/**
 * Normalize a raw SSE data object into a ChatChunk based on provider format.
 */
export function normalizeStreamChunk(
  data: unknown,
  provider: StreamProvider,
): ChatChunk | null {
  switch (provider) {
    case "openai":
      return normalizeOpenAIChunk(data);
    case "anthropic":
      return normalizeAnthropicChunk(data);
    case "google":
      return normalizeGoogleChunk(data);
    default:
      return normalizeOpenAIChunk(data);
  }
}
