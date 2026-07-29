/**
 * Goals Service
 *
 * Track goals and progress within a session.
 * Goals can be created manually or by the agent.
 */

import { nanoid } from "nanoid";

export interface Goal {
  id: string;
  title: string;
  description: string;
  status: "active" | "completed" | "abandoned";
  tasks: GoalTask[];
  createdAt: number;
  completedAt?: number;
}

export interface GoalTask {
  id: string;
  text: string;
  done: boolean;
}

type GoalListener = (goals: Goal[]) => void;
const listeners = new Set<GoalListener>();
const goals = new Map<string, Goal>();
let initialized = false;
let debounceTimer: ReturnType<typeof setTimeout>;

function notify() {
  const all = getAllGoals();
  for (const fn of listeners) fn(all);
  // Persist to localStorage as simple backup (debounced)
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    try {
      localStorage.setItem("ami-goals", JSON.stringify(all));
    } catch { /* non-critical */ }
  }, 500);
}

/** Load persisted goals from localStorage on first access. */
function ensureInit() {
  if (initialized) return;
  initialized = true;
  try {
    const raw = localStorage.getItem("ami-goals");
    if (raw) {
      const parsed: Goal[] = JSON.parse(raw);
      for (const g of parsed) goals.set(g.id, g);
    }
  } catch { /* non-critical */ }
}

export function getAllGoals(): Goal[] {
  ensureInit();
  return Array.from(goals.values()).sort((a, b) => b.createdAt - a.createdAt);
}

export function getActiveGoals(): Goal[] {
  return getAllGoals().filter((g) => g.status === "active");
}

export function onGoalChange(fn: GoalListener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

export function createGoal(title: string, description = ""): Goal {
  ensureInit();
  const goal: Goal = {
    id: `goal-${nanoid(8)}`,
    title,
    description,
    status: "active",
    tasks: [],
    createdAt: Date.now(),
  };
  goals.set(goal.id, goal);
  notify();
  return goal;
}

export function addTask(goalId: string, text: string): GoalTask | null {
  ensureInit();
  const goal = goals.get(goalId);
  if (!goal) return null;
  const task: GoalTask = { id: `task-${nanoid(6)}`, text, done: false };
  goal.tasks.push(task);
  notify();
  return task;
}

export function toggleTask(goalId: string, taskId: string): void {
  ensureInit();
  const goal = goals.get(goalId);
  if (!goal) return;
  const task = goal.tasks.find((t) => t.id === taskId);
  if (task) {
    task.done = !task.done;
    notify();
  }
}

export function completeGoal(goalId: string): void {
  ensureInit();
  const goal = goals.get(goalId);
  if (goal) {
    goal.status = "completed";
    goal.completedAt = Date.now();
    notify();
  }
}

export function abandonGoal(goalId: string): void {
  ensureInit();
  const goal = goals.get(goalId);
  if (goal) {
    goal.status = "abandoned";
    notify();
  }
}

export function deleteGoal(goalId: string): void {
  ensureInit();
  goals.delete(goalId);
  notify();
}
