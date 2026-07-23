/**
 * Conversation Loop
 *
 * The heart of the agent. Every user message triggers:
 * 1. Build context via ContextBuilder (memory-driven, not raw history)
 * 2. Send to LLM with tool definitions
 * 3. Process tool calls (each one is a memory event)
 * 4. Write tool results to memory
 * 5. Loop until LLM produces final response or hits turn limit
 */

import type {
  Message,
  TurnEvent,
  ToolResult,
  ToolDefinition,
  SessionId,
  ContextParams,
} from "@ami/shared";
import type { LLMProvider } from "@ami/llm";
import type { MemoryBridgeClient } from "@ami/memory-bridge";
import type { ContextBuilder } from "../context/builder.js";
import type { ToolExecutor } from "../tools/executor.js";
import type { ToolRegistry } from "../tools/registry.js";

export interface ConversationConfig {
  model: string;
  maxTurns: number;
  temperature: number;
  systemPrompt?: string;
}

export class ConversationLoop {
  private messages: Message[] = [];
  private turnCount = 0;

  constructor(
    private readonly config: ConversationConfig,
    private readonly llm: LLMProvider,
    private readonly contextBuilder: ContextBuilder,
    private readonly toolRegistry: ToolRegistry,
    private readonly toolExecutor: ToolExecutor,
    private readonly memory: MemoryBridgeClient,
    private readonly sessionId: SessionId,
  ) {}

  /**
   * Process a user message through the full conversation loop.
   * Yields typed events for streaming to the UI.
   */
  async *turn(userMessage: string): AsyncIterable<TurnEvent> {
    this.turnCount = 0;

    // Add user message
    this.messages.push({ role: "user", content: userMessage });

    while (this.turnCount < this.config.maxTurns) {
      this.turnCount++;

      try {
        // 1. Build context from memory (the key differentiator)
        const context = await this.contextBuilder.build({
          sessionId: this.sessionId,
          userMessage,
          activeFiles: [], // Populated by workspace manager
          recentToolResults: this.toolExecutor.getRecentResults(),
        });

        // 2. Send to LLM with tool definitions
        const toolDefs = this.toolRegistry.getDefinitions();
        const response = this.llm.chat({
          model: this.config.model,
          messages: [...context.messages, ...this.messages],
          tools: toolDefs,
          systemPrompt: context.systemPrompt,
          temperature: this.config.temperature,
        });

        // 3. Process streaming response
        let hasToolCalls = false;
        const toolCalls: Array<{
          id: string;
          name: string;
          args: Record<string, unknown>;
        }> = [];
        let textBuffer = "";

        for await (const chunk of response) {
          switch (chunk.type) {
            case "text":
              textBuffer += chunk.text;
              yield { type: "text", text: chunk.text };
              break;

            case "tool_call":
              hasToolCalls = true;
              // Accumulate tool call arguments (streaming)
              const existing = toolCalls.find((tc) => tc.id === chunk.id);
              if (existing) {
                // Merge arguments
                try {
                  const newArgs = JSON.parse(chunk.arguments);
                  Object.assign(existing.args, newArgs);
                } catch {
                  // Partial JSON — will be completed in next chunk
                }
              } else {
                try {
                  toolCalls.push({
                    id: chunk.id,
                    name: chunk.name,
                    args: JSON.parse(chunk.arguments || "{}"),
                  });
                } catch {
                  toolCalls.push({
                    id: chunk.id,
                    name: chunk.name,
                    args: {},
                  });
                }
              }
              break;

            case "done":
              break;

            case "error":
              yield { type: "error", error: chunk.error };
              return;
          }
        }

        // 4. Add assistant message
        if (textBuffer) {
          this.messages.push({ role: "assistant", content: textBuffer });
        }

        // 5. Process tool calls
        if (hasToolCalls && toolCalls.length > 0) {
          for (const tc of toolCalls) {
            yield { type: "tool_call", toolName: tc.name, args: tc.args };

            // Execute tool
            const result = await this.toolExecutor.execute(tc.name, tc.args);

            // Write tool execution to memory (this is the key difference)
            await this.memory.save({
              content: `Tool: ${tc.name}\nArgs: ${JSON.stringify(tc.args).slice(0, 500)}\nResult: ${result.preview}`,
              category: "auto_save",
              tags: [tc.name, "tool-result"],
            });

            // Add tool result to messages
            this.messages.push({
              role: "tool",
              content: result.output,
              tool_call_id: tc.id,
            });

            yield { type: "tool_result", toolName: tc.name, result };
          }

          // Continue the loop — feed tool results back to LLM
          continue;
        }

        // No tool calls — LLM produced a final response
        yield { type: "done" };
        return;
      } catch (err) {
        const errorMsg =
          err instanceof Error ? err.message : "Unknown error";
        yield { type: "error", error: errorMsg };
        return;
      }
    }

    // Hit turn limit
    yield {
      type: "error",
      error: `Reached maximum turn limit (${this.config.maxTurns})`,
    };
  }

  /**
   * Get the current conversation history.
   */
  getMessages(): Message[] {
    return [...this.messages];
  }

  /**
   * Clear conversation history (for new session).
   */
  clearHistory(): void {
    this.messages = [];
    this.turnCount = 0;
  }

  /**
   * Set messages (for restoring from compaction).
   */
  setMessages(messages: Message[]): void {
    this.messages = messages;
  }
}
