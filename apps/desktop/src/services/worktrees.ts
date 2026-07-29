/**
 * Worktree Service
 *
 * Manage Git worktrees from the IDE.
 * Create, switch, list, and remove worktrees.
 */

export interface Worktree {
  path: string;
  branch: string;
  head: string;
  isCurrent: boolean;
}

type WorktreeListener = (worktrees: Worktree[]) => void;
const listeners = new Set<WorktreeListener>();
let cached: Worktree[] = [];

function notify() { for (const fn of listeners) fn(cached); }

export function getWorktrees(): Worktree[] {
  return cached;
}

export function onWorktreeChange(fn: WorktreeListener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/**
 * List all worktrees for the current project.
 */
export async function listWorktrees(repoPath: string): Promise<Worktree[]> {
  try {
    const result = await (await import("../ipc/client")).process.run(
      "git worktree list --porcelain",
      repoPath,
    );
    const lines = result.stdout.split("\n").filter((l) => l.trim());
    const worktrees: Worktree[] = [];
    let current: Partial<Worktree> = {};

    for (const line of lines) {
      if (line.startsWith("worktree ")) {
        if (current.path) worktrees.push(current as Worktree);
        current = { path: line.slice(9), isCurrent: false };
      } else if (line.startsWith("HEAD ")) {
        current.head = line.slice(5);
      } else if (line.startsWith("branch ")) {
        current.branch = line.slice(7).replace("refs/heads/", "");
      } else if (line === "bare") {
        // skip
      }
    }
    if (current.path) worktrees.push(current as Worktree);

    // Mark current — resolve both paths for reliable comparison
    const mainPath = repoPath.replace(/\/$/, "");
    for (const wt of worktrees) {
      const wtPath = wt.path.replace(/\/$/, "").trim();
      if (wtPath === mainPath || wtPath === mainPath.trim()) {
        wt.isCurrent = true;
        break;
      }
    }

    cached = worktrees;
    notify();
    return worktrees;
  } catch (err) {
    console.error("[worktrees] listWorktrees failed:", err);
    cached = [];
    notify();
    return [];
  }
}

/**
 * Create a new worktree.
 */
export async function createWorktree(
  repoPath: string,
  branch: string,
  path: string,
): Promise<boolean> {
  try {
    const result = await (await import("../ipc/client")).process.run(
      `git worktree add "${path}" -b "${branch}"`,
      repoPath,
    );
    if (result.exitCode === 0) {
      await listWorktrees(repoPath);
      return true;
    }
    return false;
  } catch (err) {
    console.error("[worktrees] createWorktree failed:", err);
    return false;
  }
}

/**
 * Remove a worktree.
 */
export async function removeWorktree(
  repoPath: string,
  path: string,
): Promise<boolean> {
  try {
    const result = await (await import("../ipc/client")).process.run(
      `git worktree remove "${path}" --force`,
      repoPath,
    );
    if (result.exitCode === 0) {
      await listWorktrees(repoPath);
      return true;
    }
    return false;
  } catch (err) {
    console.error("[worktrees] removeWorktree failed:", err);
    return false;
  }
}

/**
 * Prune stale worktrees.
 */
export async function pruneWorktrees(repoPath: string): Promise<boolean> {
  try {
    const result = await (await import("../ipc/client")).process.run(
      "git worktree prune",
      repoPath,
    );
    if (result.exitCode === 0) {
      await listWorktrees(repoPath);
      return true;
    }
    return false;
  } catch (err) {
    console.error("[worktrees] pruneWorktrees failed:", err);
    return false;
  }
}
