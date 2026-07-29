/**
 * TaskQueueView — Background agent task queue UI.
 *
 * Shows running, queued, completed, and failed tasks.
 * User can enqueue new tasks, cancel running ones, and
 * view results or open change-sets in the Composer.
 */

import React, { useState, useEffect, useCallback } from "react";
import {
  getAllTasks,
  enqueueTask,
  cancelTask,
  retryTask,
  onTaskChange,
  type AgentTask,
  type TaskStatus,
} from "../../services/agentTasks";

interface Props {
  /** Switch to the Composer tab to review a change-set. */
  onOpenComposer?: () => void;
}

export function TaskQueueView({ onOpenComposer }: Props) {
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [newPrompt, setNewPrompt] = useState("");

  useEffect(() => {
    // Initialize from external task store — standard subscription pattern
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTasks(getAllTasks());
    return onTaskChange(setTasks);
  }, []);

  const handleEnqueue = useCallback(() => {
    if (!newPrompt.trim()) return;
    enqueueTask(newPrompt.trim());
    setNewPrompt("");
  }, [newPrompt]);

  const running = tasks.filter((t) => t.status === "running");
  const queued = tasks.filter((t) => t.status === "queued");
  const completed = tasks.filter((t) => t.status === "completed");
  const failed = tasks.filter((t) => t.status === "failed" || t.status === "cancelled");

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* New task input */}
      <div style={{ padding: "8px 12px", borderBottom: "1px solid #2a2a4a" }}>
        <div style={{ fontSize: 11, color: "#666", marginBottom: 4 }}>
          Run a task in the background
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <input
            value={newPrompt}
            onChange={(e) => setNewPrompt(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleEnqueue()}
            placeholder="Describe the task..."
            aria-label="Task prompt"
            style={{
              flex: 1,
              padding: "6px 8px",
              borderRadius: 4,
              border: "1px solid #2a2a4a",
              background: "#0d1117",
              color: "#c9d1d9",
              fontSize: 12,
              outline: "none",
            }}
          />
          <button
            onClick={handleEnqueue}
            disabled={!newPrompt.trim()}
            style={{
              padding: "6px 12px",
              borderRadius: 4,
              border: "none",
              background: newPrompt.trim() ? "#238636" : "#21262d",
              color: newPrompt.trim() ? "#fff" : "#666",
              fontSize: 12,
              cursor: newPrompt.trim() ? "pointer" : "default",
            }}
          >
            Run
          </button>
        </div>
      </div>

      {/* Task list */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {tasks.length === 0 && (
          <div style={{ padding: 20, color: "#666", fontSize: 13, textAlign: "center" }}>
            No background tasks. Enter a prompt above to run one.
          </div>
        )}

        {running.length > 0 && (
          <TaskGroup label="Running" tasks={running} onOpenComposer={onOpenComposer} />
        )}

        {queued.length > 0 && (
          <TaskGroup label="Queued" tasks={queued} onOpenComposer={onOpenComposer} />
        )}

        {completed.length > 0 && (
          <TaskGroup label="Completed" tasks={completed} onOpenComposer={onOpenComposer} />
        )}

        {failed.length > 0 && (
          <TaskGroup label="Failed / Cancelled" tasks={failed} onOpenComposer={onOpenComposer} />
        )}
      </div>
    </div>
  );
}

// ── Task Group ───────────────────────────────────────────────────────────

function TaskGroup({
  label,
  tasks,
  onOpenComposer,
}: {
  label: string;
  tasks: AgentTask[];
  onOpenComposer?: () => void;
}) {
  return (
    <div>
      <div
        style={{
          padding: "6px 12px",
          fontSize: 10,
          fontWeight: 600,
          color: "#666",
          textTransform: "uppercase",
          letterSpacing: 0.5,
        }}
      >
        {label}
      </div>
      {tasks.map((task) => (
        <TaskCard key={task.id} task={task} onOpenComposer={onOpenComposer} />
      ))}
    </div>
  );
}

// ── Task Card ────────────────────────────────────────────────────────────

function TaskCard({
  task,
  onOpenComposer,
}: {
  task: AgentTask;
  onOpenComposer?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const statusColor: Record<TaskStatus, string> = {
    queued: "#888",
    running: "#4caf50",
    completed: "#22c55e",
    failed: "#ef4444",
    cancelled: "#eab308",
  };

  const statusIcon: Record<TaskStatus, string> = {
    queued: "○",
    running: "●",
    completed: "✓",
    failed: "✗",
    cancelled: "⊘",
  };

  return (
    <div style={{ borderBottom: "1px solid #1a1a2e" }}>
      {/* Summary row */}
      <div
        onClick={() => setExpanded(!expanded)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded(!expanded);
          }
        }}
        style={{
          padding: "6px 12px",
          display: "flex",
          alignItems: "center",
          gap: 8,
          cursor: "pointer",
          fontSize: 12,
        }}
      >
        <span style={{ color: statusColor[task.status], fontSize: 10 }}>
          {statusIcon[task.status]}
        </span>
        <span style={{ color: "#c9d1d9", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {task.prompt}
        </span>
        <span style={{ fontSize: 10, color: "#666" }}>
          {task.edits.length > 0 && `${task.edits.length} edits`}
        </span>
        {task.status === "running" && (
          <button
            onClick={(e) => { e.stopPropagation(); cancelTask(task.id); }}
            style={{
              padding: "2px 6px",
              borderRadius: 3,
              border: "1px solid #2a2a4a",
              background: "transparent",
              color: "#ef4444",
              fontSize: 10,
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
        )}
        {(task.status === "failed" || task.status === "cancelled") && (
          <button
            onClick={(e) => { e.stopPropagation(); retryTask(task.id); }}
            style={{
              padding: "2px 6px",
              borderRadius: 3,
              border: "1px solid #2a2a4a",
              background: "transparent",
              color: "#888",
              fontSize: 10,
              cursor: "pointer",
            }}
          >
            Retry
          </button>
        )}
      </div>

      {/* Expanded details */}
      {expanded && (
        <div style={{ padding: "4px 12px 8px", fontSize: 11 }}>
          {task.summary && (
            <pre
              style={{
                margin: "0 0 6px",
                padding: 6,
                background: "#0d1117",
                borderRadius: 4,
                color: "#888",
                fontSize: 10,
                overflow: "auto",
                maxHeight: 120,
                whiteSpace: "pre-wrap",
              }}
            >
              {task.summary}
            </pre>
          )}

          {task.error && (
            <div style={{ color: "#ef4444", marginBottom: 6 }}>
              {task.error}
            </div>
          )}

          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <span style={{ color: "#666" }}>
              {task.turnsUsed} turns · {formatDuration(task.createdAt, task.completedAt)}
            </span>

            {task.changeSetId && (
              <button
                onClick={() => onOpenComposer?.()}
                style={{
                  padding: "3px 8px",
                  borderRadius: 3,
                  border: "none",
                  background: "#58a6ff",
                  color: "#fff",
                  fontSize: 10,
                  cursor: "pointer",
                }}
              >
                Review Changes
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────

function formatDuration(start: number, end?: number): string {
  const ms = (end ?? Date.now()) - start;
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.floor((ms % 60_000) / 1000)}s`;
}
