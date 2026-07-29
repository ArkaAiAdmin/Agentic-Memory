/**
 * Tauri IPC Client
 *
 * Type-safe bridge between the React frontend and the Tauri Rust backend.
 * Uses Tauri's invoke/event system for communication.
 */

// Tauri IPC invoke wrapper with browser fallback guard and timeout
let _tauriWarningShown = false;
const DEFAULT_TIMEOUT_MS = 60_000;

async function invoke<T>(cmd: string, args?: Record<string, unknown>, timeoutMs: number = DEFAULT_TIMEOUT_MS): Promise<T> {
  try {
    if (typeof window === "undefined") {
      return getMockFallback<T>(cmd, args);
    }
    const hasTauri = (window as any).__TAURI_INTERNALS__ || (window as any).__TAURI__;
    if (!hasTauri) {
      if (!_tauriWarningShown) {
        console.warn("[IPC] Tauri not detected — running in browser-only mode. Git, terminal, and memory bridge features require `cargo tauri dev`.");
        _tauriWarningShown = true;
      }
      return getMockFallback<T>(cmd, args);
    }
    const { invoke: tauriInvoke } = await import("@tauri-apps/api/core");
    
    return Promise.race([
      tauriInvoke<T>(cmd, args),
      new Promise<T>((_, reject) =>
        setTimeout(() => reject(new Error(`IPC call '${cmd}' timed out after ${timeoutMs}ms`)), timeoutMs)
      ),
    ]);
  } catch (err) {
    console.error("[IPC] invoke failed:", cmd, err);
    return getMockFallback<T>(cmd, args);
  }
}

// Tauri event listener wrapper with browser fallback guard
async function listen<T>(
  event: string,
  handler: (event: { payload: T }) => void,
): Promise<() => void> {
  try {
    if (typeof window !== "undefined" && !(window as any).__TAURI_INTERNALS__ && !(window as any).__TAURI__) {
      return () => {};
    }
    const { listen: tauriListen } = await import("@tauri-apps/api/event");
    return await tauriListen<T>(event, handler);
  } catch {
    return () => {};
  }
}

function getMockFallback<T>(cmd: string, args?: Record<string, unknown>): T {
  switch (cmd) {
    case "list_dir":
      return [] as unknown as T;
    case "read_file":
    case "git_diff":
    case "git_log":
    case "get_output":
    case "get_stdout":
    case "get_stderr":
      return "" as unknown as T;
    case "git_status":
      return "## main...origin/main" as unknown as T;
    case "git_branch":
      return "main" as unknown as T;
    case "create_pty":
      return "mock-pty-1" as unknown as T;
    case "run_background":
    case "start_memory_bridge":
      return "mock-proc-1" as unknown as T;
    case "is_process_alive":
      return true as unknown as T;
    case "get_managed_info":
    case "get_memory_bridge_status":
      return {
        processId: (args?.processId as string) || "mock-proc-1",
        pid: 1234,
        alive: true,
        stdout: "",
        stderr: "",
      } as unknown as T;
    case "run_command":
      return { stdout: "", stderr: "", exitCode: 0 } as unknown as T;
    default:
      return undefined as unknown as T;
  }
}

// ── Filesystem Commands ───────────────────────────────────────────────────

export const fs = {
  readFile(path: string): Promise<string> {
    return invoke<string>("read_file", { path });
  },

  writeFile(path: string, content: string): Promise<void> {
    return invoke<void>("write_file", { path, content });
  },

  deleteFile(path: string): Promise<void> {
    return invoke<void>("delete_file", { path });
  },

  listDir(path: string): Promise<Array<{ name: string; isDir: boolean }>> {
    return invoke("list_dir", { path });
  },

  startWatching(path: string): Promise<void> {
    return invoke<void>("start_watching", { path });
  },

  stopWatching(path: string): Promise<void> {
    return invoke<void>("stop_watching", { path });
  },
};

// ── Terminal Commands ─────────────────────────────────────────────────────

export const terminal = {
  create(cwd: string, cols: number, rows: number): Promise<string> {
    return invoke<string>("create_pty", { cwd, cols, rows });
  },

  write(ptyId: string, data: string): Promise<void> {
    return invoke<void>("write_pty", { ptyId, data });
  },

  resize(ptyId: string, cols: number, rows: number): Promise<void> {
    return invoke<void>("resize_pty", { ptyId, cols, rows });
  },

  destroy(ptyId: string): Promise<void> {
    return invoke<void>("destroy_pty", { ptyId });
  },

  onOutput(handler: (data: { ptyId: string; data: string }) => void) {
    return listen<{ ptyId: string; data: string }>("pty-output", (event) => handler(event.payload));
  },

  onExit(handler: (data: { ptyId: string; exitCode: number }) => void) {
    return listen<{ ptyId: string; exitCode: number }>("pty-exit", (event) => handler(event.payload));
  },
};

// ── Git Commands ──────────────────────────────────────────────────────────

export interface GitCommitInfo {
  hash: string;
  shortHash: string;
  author: string;
  email: string;
  date: number; // unix timestamp ms
  message: string;
  parents: string[];
  refs: string[];
}

export interface GitBranchInfo {
  name: string;
  isCurrent: boolean;
  isRemote: boolean;
  upstream?: string;
  lastCommitHash?: string;
  lastCommitMessage?: string;
}

export interface GitStashEntry {
  index: number;
  message: string;
  date: number;
  branch: string;
}

export interface GitBlameLine {
  lineNum: number;
  hash: string;
  author: string;
  date: number;
  content: string;
}

export const git = {
  // ── Status & Diff ─────────────────────────────────────────────────────
  status(repoPath: string): Promise<string> {
    return invoke<string>("git_status", { repoPath });
  },

  diff(repoPath: string, filePath?: string): Promise<string> {
    return invoke<string>("git_diff", { repoPath, filePath });
  },

  diffStaged(repoPath: string, filePath?: string): Promise<string> {
    return invoke<string>("git_diff_staged", { repoPath, filePath });
  },

  diffUnstaged(repoPath: string, filePath?: string): Promise<string> {
    return invoke<string>("git_diff_unstaged", { repoPath, filePath });
  },

  // ── Staging ───────────────────────────────────────────────────────────
  stage(repoPath: string, paths: string[]): Promise<void> {
    return invoke<void>("git_stage", { repoPath, paths });
  },

  unstage(repoPath: string, paths: string[]): Promise<void> {
    return invoke<void>("git_unstage", { repoPath, paths });
  },

  stageAll(repoPath: string): Promise<void> {
    return invoke<void>("git_stage_all", { repoPath });
  },

  unstageAll(repoPath: string): Promise<void> {
    return invoke<void>("git_unstage_all", { repoPath });
  },

  discardFile(repoPath: string, filePath: string): Promise<void> {
    return invoke<void>("git_discard_file", { repoPath, filePath });
  },

  // ── Commit ────────────────────────────────────────────────────────────
  commit(repoPath: string, message: string, amend?: boolean): Promise<void> {
    return invoke<void>("git_commit", { repoPath, message, amend: amend ?? false });
  },

  // ── Log ────────────────────────────────────────────────────────────────
  log(repoPath: string, limit: number): Promise<string> {
    return invoke<string>("git_log", { repoPath, limit });
  },

  logParsed(repoPath: string, limit: number): Promise<GitCommitInfo[]> {
    return invoke<GitCommitInfo[]>("git_log_parsed", { repoPath, limit });
  },

  // ── Branches ──────────────────────────────────────────────────────────
  branch(repoPath: string): Promise<string> {
    return invoke<string>("git_branch", { repoPath });
  },

  branches(repoPath: string): Promise<GitBranchInfo[]> {
    return invoke<GitBranchInfo[]>("git_branches", { repoPath });
  },

  createBranch(repoPath: string, name: string, startPoint?: string): Promise<void> {
    return invoke<void>("git_create_branch", { repoPath, name, startPoint });
  },

  switchBranch(repoPath: string, name: string): Promise<void> {
    return invoke<void>("git_switch_branch", { repoPath, name });
  },

  deleteBranch(repoPath: string, name: string, force?: boolean): Promise<void> {
    return invoke<void>("git_delete_branch", { repoPath, name, force: force ?? false });
  },

  mergeBranch(repoPath: string, name: string): Promise<{ success: boolean; message: string }> {
    return invoke("git_merge_branch", { repoPath, name });
  },

  // ── Stash ─────────────────────────────────────────────────────────────
  stashList(repoPath: string): Promise<GitStashEntry[]> {
    return invoke<GitStashEntry[]>("git_stash_list", { repoPath });
  },

  stashPush(repoPath: string, message?: string, includeUntracked?: boolean): Promise<void> {
    return invoke<void>("git_stash_push", { repoPath, message, includeUntracked: includeUntracked ?? false });
  },

  stashApply(repoPath: string, index: number): Promise<void> {
    return invoke<void>("git_stash_apply", { repoPath, index });
  },

  stashPop(repoPath: string, index: number): Promise<void> {
    return invoke<void>("git_stash_pop", { repoPath, index });
  },

  stashDrop(repoPath: string, index: number): Promise<void> {
    return invoke<void>("git_stash_drop", { repoPath, index });
  },

  // ── Blame ─────────────────────────────────────────────────────────────
  blame(repoPath: string, filePath: string): Promise<GitBlameLine[]> {
    return invoke<GitBlameLine[]>("git_blame", { repoPath, filePath });
  },

  // ── Remote ────────────────────────────────────────────────────────────
  push(repoPath: string, remote?: string, branch?: string): Promise<{ success: boolean; message: string }> {
    return invoke("git_push", { repoPath, remote, branch });
  },

  pull(repoPath: string, remote?: string, branch?: string): Promise<{ success: boolean; message: string }> {
    return invoke("git_pull", { repoPath, remote, branch });
  },

  fetch(repoPath: string, remote?: string): Promise<void> {
    return invoke<void>("git_fetch", { repoPath, remote });
  },
};

// ── Process Commands ──────────────────────────────────────────────────────

export interface ManagedProcessStatus {
  processId: string;
  pid: number;
  alive: boolean;
  stdout: string;
  stderr: string;
}

export const process = {
  run(
    command: string,
    cwd: string,
    env?: Record<string, string>,
  ): Promise<{ stdout: string; stderr: string; exitCode: number }> {
    return invoke("run_command", { command, cwd, env });
  },

  runBackground(
    command: string,
    cwd: string,
  ): Promise<string> {
    return invoke<string>("run_background", { command, cwd });
  },

  getOutput(processId: string): Promise<string> {
    return invoke<string>("get_output", { processId });
  },

  getStdout(processId: string): Promise<string> {
    return invoke<string>("get_stdout", { processId });
  },

  getStderr(processId: string): Promise<string> {
    return invoke<string>("get_stderr", { processId });
  },

  getManagedInfo(processId: string): Promise<ManagedProcessStatus> {
    return invoke("get_managed_info", { processId });
  },

  isAlive(processId: string): Promise<boolean> {
    return invoke<boolean>("is_process_alive", { processId });
  },

  writeStdin(processId: string, data: string): Promise<void> {
    return invoke<void>("write_process_stdin", { processId, data });
  },

  kill(processId: string): Promise<void> {
    return invoke<void>("kill_process", { processId });
  },
};



// ── File Watcher Events ──────────────────────────────────────────────────

export interface FsChangeEvent {
  path: string;
  changeType: "created" | "modified" | "deleted" | "renamed";
  newPath?: string; // For renamed events
}

export function onFsChange(
  handler: (event: FsChangeEvent) => void,
): Promise<() => void> {
  return listen<FsChangeEvent>("fs-change", (e) => handler(e.payload));
}
