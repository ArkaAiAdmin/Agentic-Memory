/**
 * Tool Calling Normalization
 *
 * Each LLM provider has a different tool-calling format.
 * This module normalizes across providers:
 * - OpenAI: `function` / `tool_calls` in messages
 * - Anthropic: `tool_use` / `tool_result` content blocks
 * - Google: `function_call` / `function_response` parts
 * - Local models: varies (some use OpenAI format, some use raw JSON)
 */

import type { ToolDefinition } from "@ami/shared";

export type ProviderFormat = "openai" | "anthropic" | "google";

/**
 * Normalize tool definitions for different provider formats.
 */
export function normalizeToolsForProvider(
  tools: ToolDefinition[],
  provider: ProviderFormat,
): unknown[] {
  switch (provider) {
    case "openai":
      return tools.map((t) => ({
        type: "function",
        function: {
          name: t.name,
          description: t.description,
          parameters: t.inputSchema,
        },
      }));

    case "anthropic":
      return tools.map((t) => ({
        name: t.name,
        description: t.description,
        input_schema: t.inputSchema,
      }));

    case "google":
      return tools.map((t) => ({
        name: t.name,
        description: t.description,
        parameters: t.inputSchema,
      }));

    default:
      return tools;
  }
}

/**
 * Normalize tool call responses from different provider formats.
 */
export function normalizeToolCallResponse(
  response: unknown,
  provider: ProviderFormat,
): Array<{ id: string; name: string; arguments: Record<string, unknown> }> {
  if (!response || typeof response !== "object") return [];

  switch (provider) {
    case "openai": {
      // OpenAI format: message.tool_calls
      const msg = response as any;
      const calls = msg.tool_calls ?? [];
      return calls.map((tc: any) => {
        let args: Record<string, unknown> = {};
        try {
          args = JSON.parse(tc.function.arguments);
        } catch {
          // Partial JSON from streaming — use empty object
        }
        return {
          id: tc.id,
          name: tc.function.name,
          arguments: args,
        };
      });
    }

    case "anthropic": {
      // Anthropic format: content blocks with type=tool_use
      const content = (response as any).content ?? [];
      return content
        .filter((block: any) => block.type === "tool_use")
        .map((block: any) => ({
          id: block.id,
          name: block.name,
          arguments: block.input,
        }));
    }

    case "google": {
      // Google format: function_call parts
      const candidates = (response as any).candidates ?? [];
      const parts = candidates[0]?.content?.parts ?? [];
      return parts
        .filter((part: any) => part.functionCall)
        .map((part: any) => ({
          id: `call_${Date.now()}`,
          name: part.functionCall.name,
          arguments: part.functionCall.args,
        }));
    }

    default:
      return [];
  }
}

/**
 * Detect the provider format from a model name.
 */
export function detectProviderFormat(model: string): ProviderFormat {
  if (model.startsWith("gpt-") || model.startsWith("o1") || model.startsWith("o3")) {
    return "openai";
  }
  if (model.startsWith("claude-")) {
    return "anthropic";
  }
  if (model.startsWith("gemini-")) {
    return "google";
  }
  // Default to OpenAI format (most local models support it)
  return "openai";
}
