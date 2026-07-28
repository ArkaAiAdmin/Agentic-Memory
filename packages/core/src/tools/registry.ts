/**
 * Tool Registry
 *
 * Defines and manages all tools available to the agent.
 * Tools are grouped by category: filesystem, terminal, search, memory, git, agent.
 */

import type { Tool, ToolDefinition, ToolCategory } from "@ami/shared";

export class ToolRegistry {
  private tools: Map<string, Tool> = new Map();

  /**
   * Register a tool.
   */
  register(tool: Tool): void {
    this.tools.set(tool.name, tool);
  }

  /**
   * Register multiple tools.
   */
  registerAll(tools: Tool[]): void {
    for (const tool of tools) {
      this.register(tool);
    }
  }

  /**
   * Get a tool by name.
   */
  get(name: string): Tool | undefined {
    return this.tools.get(name);
  }

  /**
   * Get all tool definitions (for LLM).
   */
  getDefinitions(categories?: ToolCategory[]): ToolDefinition[] {
    const tools = categories
      ? [...this.tools.values()].filter((t) =>
          categories.includes(t.category),
        )
      : [...this.tools.values()];

    return tools.map((t) => ({
      name: t.name,
      description: t.description,
      inputSchema: t.inputSchema,
    }));
  }

  /**
   * Get all registered tool names.
   */
  getNames(): string[] {
    return [...this.tools.keys()];
  }

  /**
   * Check if a tool exists.
   */
  has(name: string): boolean {
    return this.tools.has(name);
  }

  /**
   * Remove a tool.
   */
  unregister(name: string): boolean {
    return this.tools.delete(name);
  }
}
