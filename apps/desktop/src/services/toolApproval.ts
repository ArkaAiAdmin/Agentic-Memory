/**
 * Tool Approval Service
 *
 * Manages tool-call approval flow:
 * - Defines which tools require approval (mutating tools)
 * - Provides an approval callback that pauses the agent loop
 * - Persists "always allow" decisions per tool
 */

// ── Tool Policy ──────────────────────────────────────────────────────────

export type ToolApprovalDecision = "approve" | "deny" | "always_allow";

/** Tools that require approval by default (mutating/dangerous). */
const MUTATING_TOOLS = new Set([
  "writeFile",
  "runCommand",
  "gitCommit",
]);

/** Tools that are always safe (read-only). */
const READONLY_TOOLS = new Set([
  "readFile",
  "listDirectory",
  "globFiles",
  "grepSearch",
  "gitStatus",
  "gitDiff",
  "gitLog",
  "memorySearch",
  "memorySave",
]);

export interface ToolApprovalRequest {
  id: string;
  toolName: string;
  args: Record<string, unknown>;
  timestamp: number;
}

export type ToolApprovalCallback = (
  request: ToolApprovalRequest,
) => Promise<ToolApprovalDecision>;

// ── State ────────────────────────────────────────────────────────────────

let approvalCallback: ToolApprovalCallback | null = null;
const alwaysAllowed = new Set<string>();
let requestCounter = 0;

// ── Public API ───────────────────────────────────────────────────────────

/**
 * Check if a tool requires approval.
 */
export function needsApproval(toolName: string): boolean {
  if (READONLY_TOOLS.has(toolName)) return false;
  if (alwaysAllowed.has(toolName)) return false;
  return MUTATING_TOOLS.has(toolName) || !READONLY_TOOLS.has(toolName);
}

/**
 * Register the approval callback (called by the UI layer).
 */
export function setApprovalCallback(cb: ToolApprovalCallback | null): void {
  approvalCallback = cb;
}

/**
 * Request approval for a tool call.
 * Returns the decision, or "approve" if no callback is registered.
 */
export async function requestApproval(
  toolName: string,
  args: Record<string, unknown>,
): Promise<ToolApprovalDecision> {
  if (!needsApproval(toolName)) return "approve";
  if (!approvalCallback) return "approve";

  const request: ToolApprovalRequest = {
    id: `req-${++requestCounter}`,
    toolName,
    args,
    timestamp: Date.now(),
  };

  return approvalCallback(request);
}

/**
 * Add a tool to the always-allow list.
 */
export function allowAlways(toolName: string): void {
  alwaysAllowed.add(toolName);
}

/**
 * Remove a tool from the always-allow list.
 */
export function disallowAlways(toolName: string): void {
  alwaysAllowed.delete(toolName);
}

/**
 * Get the current always-allow list.
 */
export function getAlwaysAllowed(): string[] {
  return Array.from(alwaysAllowed);
}

/**
 * Clear the always-allow list.
 */
export function clearAlwaysAllowed(): void {
  alwaysAllowed.clear();
}
