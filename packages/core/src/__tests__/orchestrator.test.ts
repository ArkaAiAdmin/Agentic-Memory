import { describe, it, expect, vi } from "vitest";
import { AgentOrchestrator } from "../agent/orchestrator.js";
import { ToolRegistry } from "../tools/registry.js";
import { SessionManager } from "../session/manager.js";
import type { MemoryBridgeClient } from "@ami/memory-bridge";
import type { LLMProvider } from "@ami/llm";

function createMockMemory(): MemoryBridgeClient {
  return {
    search: vi.fn().mockResolvedValue([]),
    save: vi.fn().mockResolvedValue("note-100"),
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

describe("AgentOrchestrator", () => {
  it("should dispatch and track active agents", async () => {
    const mockMemory = createMockMemory();
    const mockLlm: LLMProvider = {
      name: "mock-llm",
      chat: async function* () {
        yield { type: "text", text: "Subagent complete" };
        yield { type: "done", reason: "stop" };
      },
      maxContextTokens: () => 4000,
    } as any;

    const toolRegistry = new ToolRegistry();
    const sessionManager = new SessionManager(mockMemory);

    const orchestrator = new AgentOrchestrator(
      mockMemory,
      mockLlm,
      toolRegistry,
      sessionManager,
    );

    const agent = await orchestrator.dispatch({
      id: "agent-1",
      model: "gpt-4o",
      systemPrompt: "You are test agent",
      tools: [],
      projectRoot: "/tmp/project",
    });

    expect(agent.id).toBe("agent-1");
    expect(orchestrator.getAgents().length).toBe(1);

    await orchestrator.stop("agent-1");
    expect(orchestrator.getAgents().length).toBe(0);
  });

  it("should dispatch task and return tool result", async () => {
    const mockMemory = createMockMemory();
    const mockLlm: LLMProvider = {
      name: "mock-llm",
      chat: async function* () {
        yield { type: "text", text: "Task output" };
        yield { type: "done", reason: "stop" };
      },
      maxContextTokens: () => 4000,
    } as any;

    const toolRegistry = new ToolRegistry();
    const sessionManager = new SessionManager(mockMemory);

    const orchestrator = new AgentOrchestrator(
      mockMemory,
      mockLlm,
      toolRegistry,
      sessionManager,
    );

    const result = await orchestrator.dispatchTask("Refactor code", "Please refactor X");
    expect(result.success).toBe(true);
    expect(result.output).toBe("Task output");
  });
});
