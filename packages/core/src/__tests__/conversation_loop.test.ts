import { describe, it, expect, vi } from "vitest";
import { ConversationLoop } from "../conversation/loop.js";
import { ContextBuilder } from "../context/builder.js";
import { ToolRegistry } from "../tools/registry.js";
import { ToolExecutor } from "../tools/executor.js";
import type { LLMProvider } from "@ami/llm";
import type { MemoryBridgeClient } from "@ami/memory-bridge";
import type { Tool } from "@ami/shared";

function createMockMemory(): MemoryBridgeClient {
  return {
    search: vi.fn().mockResolvedValue([]),
    save: vi.fn().mockResolvedValue("note-1"),
    recall: vi.fn().mockResolvedValue({ memories: [], context: "" }),
    sessionStart: vi.fn().mockResolvedValue({ session_id: "s1", context: "", active_threads: [], recent_memories: [], project_facts: [] }),
    sessionEnd: vi.fn().mockResolvedValue(undefined),
    compactSession: vi.fn().mockResolvedValue({ context: "" }),
    graphExplore: vi.fn().mockResolvedValue([]),
    graphTraverse: vi.fn().mockResolvedValue([]),
    reviewBeliefs: vi.fn().mockResolvedValue([]),
    audit: vi.fn().mockResolvedValue([]),
    healthCheck: vi.fn().mockResolvedValue({ status: "healthy", python_process: true, db_accessible: true, embeddings_loaded: true, version: "0.1.0" }),
    isRunning: true,
  } as any;
}

describe("ConversationLoop & ToolExecutor", () => {
  it("should stream text responses from LLM provider", async () => {
    const mockLlm: LLMProvider = {
      name: "mock-llm",
      chat: async function* () {
        yield { type: "text", text: "Hello " };
        yield { type: "text", text: "World!" };
        yield { type: "done", reason: "stop" };
      },
      maxContextTokens: () => 4000,
    } as any;

    const mockMemory = createMockMemory();
    const contextBuilder = new ContextBuilder(mockMemory, 4000);
    const registry = new ToolRegistry();
    const executor = new ToolExecutor(registry, mockMemory);

    const loop = new ConversationLoop(
      { model: "mock", maxTurns: 10, temperature: 0.7 },
      mockLlm,
      contextBuilder,
      registry,
      executor,
      mockMemory,
      "test-session",
    );

    const events: any[] = [];
    for await (const event of loop.turn("Hello")) {
      events.push(event);
    }

    expect(events).toEqual([
      { type: "text", text: "Hello " },
      { type: "text", text: "World!" },
      { type: "done" },
    ]);
  });

  it("should execute tool calls requested by LLM provider", async () => {
    const mockTool: Tool = {
      name: "echoTool",
      description: "Echoes input",
      category: "terminal",
      inputSchema: { type: "object" },
      execute: async (args) => ({
        success: true,
        output: `Echo: ${args.msg}`,
        preview: `Echo: ${args.msg}`,
      }),
    };

    const registry = new ToolRegistry();
    registry.register(mockTool);

    let callCount = 0;
    const mockLlm: LLMProvider = {
      name: "mock-llm",
      chat: async function* () {
        callCount++;
        if (callCount === 1) {
          yield {
            type: "tool_call",
            id: "call-1",
            name: "echoTool",
            arguments: JSON.stringify({ msg: "Hello Tool" }),
          };
          yield { type: "done", reason: "tool_calls" };
        } else {
          yield { type: "text", text: "Tool finished!" };
          yield { type: "done", reason: "stop" };
        }
      },
      maxContextTokens: () => 4000,
    } as any;

    const mockMemory = createMockMemory();
    const contextBuilder = new ContextBuilder(mockMemory, 4000);
    const executor = new ToolExecutor(registry, mockMemory);

    const loop = new ConversationLoop(
      { model: "mock", maxTurns: 10, temperature: 0.7 },
      mockLlm,
      contextBuilder,
      registry,
      executor,
      mockMemory,
      "test-session",
    );

    const events: any[] = [];
    for await (const event of loop.turn("Run echo")) {
      events.push(event);
    }

    const toolCallEvent = events.find((e) => e.type === "tool_call");
    const toolResultEvent = events.find((e) => e.type === "tool_result");

    expect(toolCallEvent).toBeDefined();
    expect(toolCallEvent.toolName).toBe("echoTool");
    expect(toolResultEvent).toBeDefined();
    expect(toolResultEvent.result.output).toBe("Echo: Hello Tool");
  });
});
