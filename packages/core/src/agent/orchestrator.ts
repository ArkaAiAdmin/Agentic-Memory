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
   * Dispatch a sub-agent for a specific task with parent-child tracking.
   */
  async dispatchTask(
    description: string,
    prompt: string,
    options?: {
      parentAgentId?: string;
      projectId?: string;
      taskType?: string;
    },
  ): Promise<ToolResult> {
    const taskId = `task-${Date.now()}`;
    const parentAgentId = options?.parentAgentId ?? "IDE";
    const projectId = options?.projectId ?? "default";

    // 1. Initialize sub-agent identity in kernel with parent link
    try {
      await this.memory.initAgent(taskId, {
        displayName: `SubAgent ${taskId}`,
        parentAgent: parentAgentId,
      });
    } catch (err) {
      console.warn(`[Orchestrator] Kernel init sub-agent ${taskId} failed:`, err);
    }

    // 2. Register task in kernel coordination layer
    let createdTaskId: string | null = null;
    try {
      const taskRes = await this.memory.coordinateTask("create_task", {
        project_id: projectId,
        task_type: options?.taskType ?? "subagent_task",
        description,
        assigned_to: taskId,
      });
      if (taskRes && typeof taskRes === "object" && "task_id" in taskRes) {
        createdTaskId = String(taskRes.task_id);
      }
    } catch (err) {
      console.warn(`[Orchestrator] Kernel task creation failed:`, err);
    }

    const agent = await this.dispatch({
      id: taskId,
      model: "gpt-4o-mini", // Use smaller model for tasks
      systemPrompt: `You are a focused sub-agent (ID: ${taskId}, Parent: ${parentAgentId}). Complete the task: ${description}`,
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

      // 3. Mark task as completed in kernel coordination layer
      if (createdTaskId) {
        this.memory.coordinateTask("complete_task", { task_id: createdTaskId })
          .catch(err => console.warn(`[Orchestrator] Kernel complete_task failed:`, err));
      }

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
   * Send a message to another agent (inter-agent messaging via memory_coordinate).
   */
  async sendMessage(
    from: string,
    to: string,
    type: string,
    payload: Record<string, unknown>,
  ): Promise<void> {
    // Dispatch message via kernel coordination layer
    await this.memory.coordinateMessage("send_message", {
      to_agent: to,
      message_type: type,
      payload: {
        from_agent: from,
        ...payload,
      },
    });

    // Save searchable message trace in memory journal
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
