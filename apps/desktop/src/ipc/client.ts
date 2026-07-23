/**
 * Tauri IPC Client
 *
 * Type-safe bridge between the React frontend and the Tauri Rust backend.
 * Uses Tauri's invoke/event system for communication.
 */

// Tauri IPC invoke wrapper
async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke: tauriInvoke } = await import("@tauri-apps/api/core");
  return tauriInvoke<T>(cmd, args);
}

// Tauri event listener wrapper
async function listen<T>(
  event: string,
  handler: (event: { payload: T }) => void,
): Promise<() => void> {
  const { listen: tauriListen } = await import("@tauri-apps/api/event");
  return tauriListen<T>(event, handler);
}

// ── Filesystem Commands ───────────────────────────────────────────────────

export const fs = {
  readFile(path: string): Promise<string> {
    return invoke<string>("read_file", { path });
  },

  writeFile(path: string, content: string): Promise<void> {
    return invoke<void>("write_file", { path, content });
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

export const git = {
  status(repoPath: string): Promise<string> {
    return invoke<string>("git_status", { repoPath });
  },

  diff(repoPath: string, filePath?: string): Promise<string> {
    return invoke<string>("git_diff", { repoPath, filePath });
  },

  log(repoPath: string, limit: number): Promise<string> {
    return invoke<string>("git_log", { repoPath, limit });
  },

  commit(repoPath: string, message: string): Promise<void> {
    return invoke<void>("git_commit", { repoPath, message });
  },

  branch(repoPath: string): Promise<string> {
    return invoke<string>("git_branch", { repoPath });
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

// ── Memory Bridge Commands ──────────────────────────────────────────────────

export const memoryBridge = {
  start(memoryDir: string, pythonPath?: string): Promise<string> {
    return invoke<string>("start_memory_bridge", { memoryDir, pythonPath });
  },

  stop(processId: string): Promise<void> {
    return invoke<void>("stop_memory_bridge", { processId });
  },

  getStatus(processId: string): Promise<ManagedProcessStatus> {
    return invoke("get_memory_bridge_status", { processId });
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
