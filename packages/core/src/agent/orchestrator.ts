/**
 * Agent Orchestrator
 *
 * Manages multiple concurrent agent instances.
 * Each agent has its own conversation loop, coordinated via
 * the memory system's coordination layer (messaging, locking, project state).
 */

import type { AgentConfig, AgentInstance, ToolResult } from "@ami/shared";
import type { MemoryBridgeClient } from "@ami/memory-bridge";
import type { LLMProvider } from "@ami/llm";
import { ConversationLoop } from "../conversation/loop.js";
import { ContextBuilder } from "../context/builder.js";
import { ToolRegistry } from "../tools/registry.js";
import { ToolExecutor } from "../tools/executor.js";
import { SessionManager } from "../session/manager.js";

export class AgentOrchestrator {
  private agents: Map<string, AgentInstance & { loop?: ConversationLoop }> =
    new Map();

  constructor(
    private readonly memory: MemoryBridgeClient,
    private readonly llm: LLMProvider,
    private readonly toolRegistry: ToolRegistry,
    private readonly sessionManager: SessionManager,
  ) {}

  /**
   * Dispatch a new agent instance.
   */
  async dispatch(config: AgentConfig): Promise<AgentInstance> {
    const contextBuilder = new ContextBuilder(this.memory);
    const toolExecutor = new ToolExecutor(this.toolRegistry, this.memory);

    const loop = new ConversationLoop(
      {
        model: config.model,
        maxTurns: 50,
        temperature: 0.7,
        systemPrompt: config.systemPrompt,
      },
      this.llm,
      contextBuilder,
      this.toolRegistry,
      toolExecutor,
      this.memory,
      config.id,
    );

    const instance: AgentInstance & { loop?: ConversationLoop } = {
      id: config.id,
      config,
      status: "idle",
      loop,
    };

    this.agents.set(config.id, instance);
    return { id: instance.id, config: instance.config, status: instance.status };
  }

  /**
   * Dispatch a sub-agent for a specific task (like the Task tool).
   */
  async dispatchTask(
    description: string,
    prompt: string,
  ): Promise<ToolResult> {
    const agent = await this.dispatch({
      id: `task-${Date.now()}`,
      model: "gpt-4o-mini", // Use smaller model for tasks
      systemPrompt: `You are a focused sub-agent. Complete the task: ${description}`,
      tools: this.toolRegistry.getNames(),
      projectRoot: "",
    });

    const instance = this.agents.get(agent.id);
    if (!instance?.loop) {
      return {
        success: false,
        output: "",
        preview: "",
        error: "Failed to create agent loop",
      };
    }

    instance.status = "running";

    try {
      let output = "";
      for await (const event of instance.loop.turn(prompt)) {
        if (event.type === "text") {
          output += event.text;
        }
      }

      instance.status = "idle";
      return {
        success: true,
        output,
        preview: output.slice(0, 500),
      };
    } catch (err) {
      instance.status = "error";
      return {
        success: false,
        output: "",
        preview: "",
        error: err instanceof Error ? err.message : "Task failed",
      };
    }
  }

  /**
   * Send a message to an agent (inter-agent messaging).
   */
  async sendMessage(
    from: string,
    to: string,
    type: string,
    payload: Record<string, unknown>,
  ): Promise<void> {
    // TODO: Switch to memory_coordinate or memory_send_message MCP tool once exposed in bridge
    await this.memory.save({
      content: `Message from ${from} to ${to}: [${type}] ${JSON.stringify(payload).slice(0, 500)}`,
      category: "auto_save",
      tags: ["agent-message", from, to, type],
    });
  }

  /**
   * Stop an agent.
   */
  async stop(agentId: string): Promise<void> {
    const agent = this.agents.get(agentId);
    if (agent) {
      agent.status = "stopped";
      this.agents.delete(agentId);
    }
  }

  /**
   * Get all active agents.
   */
  getAgents(): AgentInstance[] {
    return [...this.agents.values()].map((a) => ({
      id: a.id,
      config: a.config,
      status: a.status,
    }));
  }
}
