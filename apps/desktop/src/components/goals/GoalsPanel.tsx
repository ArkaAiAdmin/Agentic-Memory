/**
 * GoalsPanel — Track goals and progress.
 *
 * Create goals, add tasks, mark complete.
 * Goals persist in the memory system.
 */

import React, { useState, useEffect, useCallback } from "react";
import {
  createGoal,
  addTask,
  toggleTask,
  completeGoal,
  abandonGoal,
  deleteGoal,
  onGoalChange,
  type Goal,
} from "../../services/goals";

export function GoalsPanel() {
  const [goals, setGoals] = useState<Goal[]>([]);
  const [newTitle, setNewTitle] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => onGoalChange(setGoals), []);

  const handleCreate = useCallback(() => {
    if (!newTitle.trim()) return;
    const goal = createGoal(newTitle.trim());
    setNewTitle("");
    setExpanded(goal.id);
  }, [newTitle]);

  const active = goals.filter((g) => g.status === "active");
  const completed = goals.filter((g) => g.status === "completed");

  return (
    <div style={{ padding: 16, height: "100%", overflow: "auto" }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 16 }}>
        Goals
      </div>

      {/* New goal input */}
      <div style={{ display: "flex", gap: 6, marginBottom: 16 }}>
        <input
          value={newTitle}
          onChange={(e) => setNewTitle(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleCreate()}
          placeholder="New goal..."
          aria-label="Add goal"
          style={{
            flex: 1, padding: "8px 12px", borderRadius: "var(--radius-md)",
            border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
            color: "var(--text-primary)", fontSize: 12,
          }}
        />
        <button onClick={handleCreate} style={addBtn}>Add</button>
      </div>

      {/* Active goals */}
      {active.length === 0 && completed.length === 0 && (
        <div style={{ textAlign: "center", padding: 20, color: "var(--text-tertiary)", fontSize: 12 }}>
          No goals yet. Create one above.
        </div>
      )}

      {active.map((goal) => (
        <GoalCard
          key={goal.id}
          goal={goal}
          expanded={expanded === goal.id}
          onToggle={() => setExpanded(expanded === goal.id ? null : goal.id)}
          onComplete={() => completeGoal(goal.id)}
          onAbandon={() => abandonGoal(goal.id)}
          onDelete={() => deleteGoal(goal.id)}
        />
      ))}

      {completed.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 8 }}>
            Completed ({completed.length})
          </div>
          {completed.map((goal) => (
            <GoalCard
              key={goal.id}
              goal={goal}
              expanded={expanded === goal.id}
              onToggle={() => setExpanded(expanded === goal.id ? null : goal.id)}
              onComplete={() => {}}
              onAbandon={() => {}}
              onDelete={() => deleteGoal(goal.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function GoalCard({
  goal, expanded, onToggle, onComplete, onAbandon, onDelete,
}: {
  goal: Goal;
  expanded: boolean;
  onToggle: () => void;
  onComplete: () => void;
  onAbandon: () => void;
  onDelete: () => void;
}) {
  const [newTaskText, setNewTaskText] = useState("");
  const doneCount = goal.tasks.filter((t) => t.done).length;
  const total = goal.tasks.length;
  const progress = total > 0 ? (doneCount / total) * 100 : 0;
  const isActive = goal.status === "active";

  const handleAddTask = () => {
    if (!newTaskText.trim()) return;
    addTask(goal.id, newTaskText.trim());
    setNewTaskText("");
  };

  return (
    <div style={{
      marginBottom: 8, borderRadius: "var(--radius-md)",
      border: `1px solid ${isActive ? "var(--border-default)" : "var(--border-subtle)"}`,
      background: "var(--bg-tertiary)", overflow: "hidden",
    }}>
      <div onClick={onToggle} style={{
        padding: "10px 12px", cursor: "pointer",
        display: "flex", alignItems: "center", gap: 8,
      }} role="button" tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}>
        <span style={{ fontSize: 10, color: "var(--text-tertiary)" }}>{expanded ? "▾" : "▸"}</span>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 12, fontWeight: 500, color: "var(--text-primary)", opacity: isActive ? 1 : 0.6 }}>
            {goal.title}
          </div>
          {total > 0 && (
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4 }}>
              <div style={{ flex: 1, height: 3, background: "var(--bg-elevated)", borderRadius: 2 }}>
                <div style={{ width: `${progress}%`, height: "100%", background: progress === 100 ? "var(--success)" : "var(--accent)", borderRadius: 2, transition: "width 0.2s" }} />
              </div>
              <span style={{ fontSize: 10, color: "var(--text-tertiary)" }}>{doneCount}/{total}</span>
            </div>
          )}
        </div>
        {isActive && (
          <div style={{ display: "flex", gap: 4 }}>
            <button onClick={(e) => { e.stopPropagation(); onComplete(); }} style={miniBtn} title="Complete">✓</button>
            <button onClick={(e) => { e.stopPropagation(); onAbandon(); }} style={miniBtn} title="Abandon">⊘</button>
            <button onClick={(e) => { e.stopPropagation(); onDelete(); }} style={{ ...miniBtn, color: "var(--error)" }} title="Delete">×</button>
          </div>
        )}
      </div>

      {expanded && (
        <div style={{ padding: "0 12px 12px" }}>
          {goal.description && (
            <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 8 }}>{goal.description}</div>
          )}

          {goal.tasks.map((task) => (
            <div key={task.id} onClick={() => toggleTask(goal.id, task.id)} style={{
              padding: "4px 8px", fontSize: 11, cursor: "pointer",
              display: "flex", alignItems: "center", gap: 6, borderRadius: "var(--radius-xs)",
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = "var(--bg-hover)"}
            onMouseLeave={(e) => e.currentTarget.style.background = "transparent"}
            >
              <span style={{ color: task.done ? "var(--success)" : "var(--text-tertiary)", fontSize: 12 }}>
                {task.done ? "✓" : "○"}
              </span>
              <span style={{ textDecoration: task.done ? "line-through" : "none", opacity: task.done ? 0.5 : 1, color: "var(--text-primary)" }}>
                {task.text}
              </span>
            </div>
          ))}

          {isActive && (
            <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
              <input
                value={newTaskText}
                onChange={(e) => setNewTaskText(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleAddTask()}
                placeholder="Add task..."
                style={{
                  flex: 1, padding: "4px 8px", borderRadius: "var(--radius-xs)",
                  border: "1px solid var(--border-default)", background: "var(--bg-primary)",
                  color: "var(--text-primary)", fontSize: 11,
                }}
              />
              <button onClick={handleAddTask} style={addBtn}>+</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const addBtn: React.CSSProperties = {
  padding: "6px 12px", borderRadius: "var(--radius-sm)", border: "none",
  background: "var(--accent)", color: "var(--accent-text)", fontSize: 11, fontWeight: 600, cursor: "pointer",
};
const miniBtn: React.CSSProperties = {
  padding: "2px 6px", borderRadius: "var(--radius-xs)", border: "none",
  background: "transparent", color: "var(--text-tertiary)", fontSize: 11, cursor: "pointer",
};
