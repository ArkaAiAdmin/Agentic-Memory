/**
 * Agent Task Queue
 *
 * Manages background agent jobs that run without blocking the chat.
 * Each task:
 * - Runs its own ConversationLoop with an isolated session
 * - Produces change-sets for review in the Composer
 * - Saves a memory trail of what it did
 * - Notifies the UI on completion
 */

import { nanoid } from "nanoid";
import { memoryBridge } from "@ami/memory-bridge";
import { agentService } from "./agentService";
import { createChangeSet } from "./changeSet";
import type { FileEdit } from "@ami/shared";

// ── Types ────────────────────────────────────────────────────────────────

export type TaskStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface AgentTask {
  id: string;
  prompt: string;
  status: TaskStatus;
  createdAt: number;
  startedAt?: number;
  completedAt?: number;
  /** Summary of what the task did. */
  summary?: string;
  /** File edits produced by the task. */
  edits: FileEdit[];
  /** Change-set ID if edits were staged. */
  changeSetId?: string;
  /** Error message if failed. */
  error?: string;
  /** Turn count consumed. */
  turnsUsed: number;
}

export type TaskListener = (tasks: AgentTask[]) => void;

// ── State ────────────────────────────────────────────────────────────────

const tasks = new Map<string, AgentTask>();
const activeControllers = new Map<string, AbortController>();
let listener: TaskListener | null = null;
const MAX_CONCURRENT = 2;

// ── Helpers ──────────────────────────────────────────────────────────────

function notifyListener() {
  if (listener) {
    listener(getAllTasks());
  }
}

function getRunningCount(): number {
  return Array.from(tasks.values()).filter((t) => t.status === "running").length;
}

// ── Public API ───────────────────────────────────────────────────────────

/** Get all tasks. */
export function getAllTasks(): AgentTask[] {
  return Array.from(tasks.values()).sort((a, b) => b.createdAt - a.createdAt);
}

/** Get a task by ID. */
export function getTask(id: string): AgentTask | undefined {
  return tasks.get(id);
}

/** Subscribe to task changes. */
export function onTaskChange(fn: TaskListener): () => void {
  listener = fn;
  return () => { listener = null; };
}

/**
 * Enqueue a background task.
 * Returns the task ID. The task starts when capacity is available.
 */
export function enqueueTask(prompt: string): string {
  const id = `task-${nanoid(8)}`;
  const task: AgentTask = {
    id,
    prompt,
    status: "queued",
    createdAt: Date.now(),
    edits: [],
    turnsUsed: 0,
  };
  tasks.set(id, task);
  notifyListener();

  // Try to start immediately if capacity available
  scheduleNext();

  return id;
}

/**
 * Cancel a running or queued task.
 */
export function cancelTask(id: string): boolean {
  const task = tasks.get(id);
  if (!task) return false;

  if (task.status === "running") {
    const controller = activeControllers.get(id);
    if (controller) {
      controller.abort();
      activeControllers.delete(id);
    }
  }

  task.status = "cancelled";
  task.completedAt = Date.now();
  notifyListener();
  return true;
}

/**
 * Retry a failed or cancelled task.
 */
export function retryTask(id: string): string | null {
  const task = tasks.get(id);
  if (!task || (task.status !== "failed" && task.status !== "cancelled")) return null;

  return enqueueTask(task.prompt);
}

/** Subscribe to task changes. */
export { onTaskChange as onChange };

// ── Internal Execution ───────────────────────────────────────────────────

function scheduleNext() {
  if (getRunningCount() >= MAX_CONCURRENT) return;

  const queued = Array.from(tasks.values())
    .filter((t) => t.status === "queued")
    .sort((a, b) => a.createdAt - b.createdAt);

  if (queued.length === 0) return;

  const next = queued[0];
  executeTask(next.id);
}

async function executeTask(id: string) {
  const task = tasks.get(id);
  if (!task || task.status !== "queued") return;

  const controller = new AbortController();
  activeControllers.set(id, controller);

  task.status = "running";
  task.startedAt = Date.now();
  notifyListener();

  try {
    // Ensure agent is initialized
    if (!agentService.isInitialized) {
      throw new Error("Agent service not initialized");
    }

    // Create an isolated session for this task
    const sessionId = `bg-${task.id}`;
    let accumulated = "";
    let turnsUsed = 0;

    for await (const event of agentService.sendMessage(task.prompt, sessionId)) {
      if (controller.signal.aborted) {
        task.status = "cancelled";
        task.completedAt = Date.now();
        notifyListener();
        return;
      }

      switch (event.type) {
        case "text":
          accumulated += event.text;
          break;
        case "tool_call":
          turnsUsed++;
          // Track file edits from writeFile calls
          if (event.toolName === "writeFile" && event.args.path) {
            task.edits.push({
              path: event.args.path as string,
              kind: "modify",
              newText: event.args.content as string,
            });
          }
          break;
        case "tool_result":
          break;
        case "error":
          task.error = event.error;
          task.status = "failed";
          task.completedAt = Date.now();
          notifyListener();
          return;
        case "done":
          break;
      }
    }

    task.turnsUsed = turnsUsed;

    // Stage edits as a change-set if any were produced
    if (task.edits.length > 0) {
      const cs = await createChangeSet(
        `Background task: ${task.prompt.slice(0, 80)}`,
        task.edits,
      );
      task.changeSetId = cs.id;
    }

    // Save task summary to memory
    const summary = accumulated.slice(0, 500) || "No output";
    task.summary = summary;

    await memoryBridge.save({
      content: `Background task completed: ${task.prompt}\nSummary: ${summary}\nEdits: ${task.edits.length} files`,
      category: "projects",
      tags: ["background-task", "completed"],
    });

    task.status = task.error ? "failed" : "completed";
    task.completedAt = Date.now();
  } catch (err) {
    task.status = "failed";
    task.error = err instanceof Error ? err.message : "Unknown error";
    task.completedAt = Date.now();
  } finally {
    activeControllers.delete(id);
    notifyListener();
    // Start next queued task
    scheduleNext();
  }
}
