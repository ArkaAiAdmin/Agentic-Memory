/**
 * Extensible Tool Registry
 *
 * Provides a way to create tool registries from multiple sources:
 * - Built-in tools (agentService)
 * - MCP server tools (settings)
 * - User-defined tools (future)
 *
 * Tools are merged and deduplicated by name. Later registrations
 * overwrite earlier ones for the same name.
 */

import type { Tool, ToolCategory } from "@ami/shared";

export type ToolSource = "builtin" | "mcp" | "user" | "plugin";

export interface RegisteredTool extends Tool {
  source: ToolSource;
  enabled: boolean;
}

export interface ToolContributor {
  id: string;
  source: ToolSource;
  getTools(): Tool[];
}

class ToolContributorRegistry {
  private contributors = new Map<string, ToolContributor>();

  register(contributor: ToolContributor): void {
    this.contributors.set(contributor.id, contributor);
  }

  unregister(id: string): void {
    this.contributors.delete(id);
  }

  getAllTools(): RegisteredTool[] {
    const toolsByName = new Map<string, RegisteredTool>();

    for (const contributor of this.contributors.values()) {
      for (const tool of contributor.getTools()) {
        toolsByName.set(tool.name, {
          ...tool,
          source: contributor.source,
          enabled: true,
        });
      }
    }

    return Array.from(toolsByName.values());
  }

  getToolsByCategory(category: ToolCategory): RegisteredTool[] {
    return this.getAllTools().filter((t) => t.category === category);
  }

  getContributors(): ToolContributor[] {
    return Array.from(this.contributors.values());
  }
}

export const toolContributorRegistry = new ToolContributorRegistry();
