/**
 * Tool Executor
 *
 * Executes tools with pre/post memory events and optional approval.
 * Every tool execution is a memory event — this is the key architectural
 * difference from other IDEs.
 */

import type { ToolResult, ToolContext } from "@ami/shared";
import type { MemoryBridgeClient } from "@ami/memory-bridge";
import type { ToolRegistry } from "./registry.js";

export type ApprovalDecision = "approve" | "deny" | "always_allow";

export interface ApprovalRequest {
  toolName: string;
  args: Record<string, unknown>;
}

export type ApprovalCallback = (request: ApprovalRequest) => Promise<ApprovalDecision>;

export class ToolExecutor {
  private recentResults: Array<{ tool: string; result: string }> = [];
  private maxRecentResults = 20;
  private _approvalCallback: ApprovalCallback | null = null;

  constructor(
    private readonly registry: ToolRegistry,
    private readonly memory: MemoryBridgeClient,
  ) {}

  /**
   * Set the approval callback. When set, mutating tools will prompt
   * for approval before execution.
   */
  setApprovalCallback(cb: ApprovalCallback | null): void {
    this._approvalCallback = cb;
  }

  /**
   * Execute a tool with full memory integration and optional approval.
   */
  async execute(
    toolName: string,
    args: Record<string, unknown>,
    ctx?: ToolContext,
  ): Promise<ToolResult> {
    const tool = this.registry.get(toolName);
    if (!tool) {
      return {
        success: false,
        output: "",
        preview: "",
        error: `Unknown tool: ${toolName}`,
      };
    }

    // Check approval for mutating tools
    if (this._approvalCallback) {
      const decision = await this._approvalCallback({ toolName, args });
      if (decision === "deny") {
        return {
          success: false,
          output: "",
          preview: "",
          error: `Tool call denied by user: ${toolName}`,
        };
      }
      // "always_allow" — the UI layer handles persisting this
    }

    // 1. Pre-execution: save intent to memory (crash recovery)
    try {
      await this.memory.save({
        content: `About to execute: ${toolName}(${JSON.stringify(args).slice(0, 500)})`,
        category: "auto_save",
        tags: ["tool-intent", toolName],
      });
    } catch {
      // Don't block tool execution on memory save failure
    }

    // 2. Execute
    let result: ToolResult;
    try {
      const toolCtx: ToolContext = ctx ?? {
        projectRoot: "",
        sessionId: "",
        agentId: "default",
        allowedPaths: [],
      };
      result = await tool.execute(args, toolCtx);
    } catch (err) {
      result = {
        success: false,
        output: "",
        preview: "",
        error:
          err instanceof Error ? err.message : "Unknown execution error",
      };
    }

    // 3. Post-execution: save result to memory
    try {
      await this.memory.save({
        content: `Tool result: ${toolName}\n${result.preview.slice(0, 1000)}`,
        category: "auto_save",
        tags: ["tool-result", toolName],
      });
    } catch {
      // Don't block on memory save failure
    }

    // 4. Track recent results
    this.recentResults.push({
      tool: toolName,
      result: result.preview.slice(0, 200),
    });
    if (this.recentResults.length > this.maxRecentResults) {
      this.recentResults.shift();
    }

    return result;
  }

  /**
   * Get recent tool results (for context builder).
   */
  getRecentResults(): Array<{ tool: string; result: string }> {
    return [...this.recentResults];
  }

  /**
   * Clear recent results (for new session).
   */
  clearRecentResults(): void {
    this.recentResults = [];
  }
}
