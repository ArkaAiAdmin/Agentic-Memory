/**
 * Tool Middleware
 *
 * Intercepts tool execution via a chain of middleware functions.
 * The change-set middleware catches writeFile/deleteFile calls,
 * captures pre-images, and stages them as FileEdit proposals
 * instead of writing directly.
 */

import type { Tool, ToolResult, ToolContext } from "@ami/shared";

export type MiddlewareNext = (
  args: Record<string, unknown>,
  ctx: ToolContext,
) => Promise<ToolResult>;

export type MiddlewareFn = (
  tool: Tool,
  args: Record<string, unknown>,
  ctx: ToolContext,
  next: MiddlewareNext,
) => Promise<ToolResult>;

export class ToolMiddlewareChain {
  private middlewares: MiddlewareFn[] = [];

  use(middleware: MiddlewareFn): void {
    this.middlewares.push(middleware);
  }

  /**
   * Execute a tool through the middleware chain.
   * The final step in the chain calls the actual tool.execute.
   */
  async execute(
    tool: Tool,
    args: Record<string, unknown>,
    ctx: ToolContext,
    actualExecute: MiddlewareNext,
  ): Promise<ToolResult> {
    if (this.middlewares.length === 0) {
      return actualExecute(args, ctx);
    }

    // Build the chain: each middleware calls the next one
    let chain = actualExecute;
    for (let i = this.middlewares.length - 1; i >= 0; i--) {
      const mw = this.middlewares[i];
      const next = chain;
      chain = (a, c) => mw(tool, a, c, next);
    }

    return chain(args, ctx);
  }
}
