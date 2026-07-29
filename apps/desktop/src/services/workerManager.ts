/**
 * Background Worker Manager
 *
 * Manages the agentic-memory background workers via the Rust backend:
 * - Index rebuild, embedding, contradiction detection, KG extraction
 * - Health checks, integrity checks, consolidation, backups
 *
 * Process spawning and stdout/stderr buffering happen in Rust.
 * This module tracks worker state and emits status events to the UI.
 */

import { process as ipcProcess } from "../ipc/client";

// ── Types ──────────────────────────────────────────────────────────────────

export type WorkerStatus = "idle" | "running" | "error" | "stopped";

export type WorkerType =
  | "index_rebuild"
  | "embedding"
  | "contradiction"
  | "kg_extraction"
  | "health_check"
  | "integrity_check"
  | "consolidation"
  | "backup";

export interface WorkerInfo {
  type: WorkerType;
  status: WorkerStatus;
  lastRunAt: number | null;
  lastError: string | null;
  runCount: number;
}

export interface WorkerEvent {
  type: "started" | "completed" | "error" | "progress" | "stopped";
  workerType: WorkerType;
  message: string;
  progress?: number;
}

type WorkerEventHandler = (event: WorkerEvent) => void;

// ── Worker Definitions ─────────────────────────────────────────────────────

interface WorkerDefinition {
  type: WorkerType;
  command: string;
  args: string[];
  intervalMs: number | null;
  description: string;
}

function discoverPython(memoryDir: string): { pythonPath: string; scriptDir: string } {
  const scriptDir = memoryDir;
  const candidates: string[] = [
    `${memoryDir}/venv/bin/python`,
    `${memoryDir}/.venv/bin/python`,
    "/opt/homebrew/bin/python3",
    "/opt/homebrew/opt/python@3.14/bin/python3.14",
    "/opt/homebrew/opt/python@3.13/bin/python3.13",
    "/opt/homebrew/opt/python@3.12/bin/python3.12",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
  ];
  for (const candidate of candidates) {
    if (typeof window === "undefined" && require("fs").existsSync(candidate)) {
      return { pythonPath: candidate, scriptDir };
    }
  }
  return { pythonPath: "python3", scriptDir };
}

const WORKER_DEFINITIONS: WorkerDefinition[] = [
  {
    type: "index_rebuild",
    command: "PYTHON_DISCOVERED",
    args: ["cron/cron_embedding_recompute.py", "--rebuild"],
    intervalMs: null,
    description: "Rebuild search indices when project files change",
  },
  {
    type: "embedding",
    command: "PYTHON_DISCOVERED",
    args: ["cron/cron_embedding_recompute.py"],
    intervalMs: 30_000,
    description: "Compute embeddings for new memories",
  },
  {
    type: "contradiction",
    command: "PYTHON_DISCOVERED",
    args: ["cron/cron_resolve_contradictions.py"],
    intervalMs: 60_000,
    description: "Detect contradictions between memories",
  },
  {
    type: "kg_extraction",
    command: "PYTHON_DISCOVERED",
    args: ["cron/cron_kg_backfill.py"],
    intervalMs: 45_000,
    description: "Extract entities and facts from new memories",
  },
  {
    type: "health_check",
    command: "PYTHON_DISCOVERED",
    args: ["cron/cron_health_check.py"],
    intervalMs: 120_000,
    description: "Check memory system health",
  },
  {
    type: "integrity_check",
    command: "PYTHON_DISCOVERED",
    args: ["cron/cron_integrity_check.py"],
    intervalMs: 300_000,
    description: "Verify database integrity",
  },
  {
    type: "consolidation",
    command: "PYTHON_DISCOVERED",
    args: ["cron/cron_consolidate.py"],
    intervalMs: 600_000,
    description: "Consolidate and merge duplicate memories",
  },
  {
    type: "backup",
    command: "PYTHON_DISCOVERED",
    args: ["cron/cron_backup.py"],
    intervalMs: 3600_000,
    description: "Backup memory database",
  },
];

// ── Internal Worker State ──────────────────────────────────────────────────

interface InternalWorkerInfo {
  type: WorkerType;
  status: WorkerStatus;
  lastRunAt: number | null;
  lastError: string | null;
  runCount: number;
  processId: string | null;
  lastStdoutLen: number;
  lastStderrLen: number;
}

// ── Worker Manager ─────────────────────────────────────────────────────────

export class BackgroundWorkerManager {
  private workers: Map<WorkerType, InternalWorkerInfo> = new Map();
  private intervals: Map<WorkerType, ReturnType<typeof setInterval>> =
    new Map();
  private eventHandlers: Map<number, WorkerEventHandler> = new Map();
  private memoryDir: string;
  private pythonPath: string;
  private scriptDir: string;
  private _running = false;
  private pollHandle: ReturnType<typeof setInterval> | null = null;
  private handlerIdCounter = 0;

  constructor(memoryDir: string) {
    this.memoryDir = memoryDir;
    const discovered = discoverPython(memoryDir);
    this.pythonPath = discovered.pythonPath;
    this.scriptDir = discovered.scriptDir;

    for (const def of WORKER_DEFINITIONS) {
      this.workers.set(def.type, {
        type: def.type,
        status: "idle",
        lastRunAt: null,
        lastError: null,
        runCount: 0,
        processId: null,
        lastStdoutLen: 0,
        lastStderrLen: 0,
      });
    }
  }

  configure(pythonPath: string, scriptDir: string): void {
    this.pythonPath = pythonPath;
    this.scriptDir = scriptDir;
  }

  get isRunning(): boolean {
    return this._running;
  }

  start(): void {
    if (this._running) return;
    this._running = true;

    const hasTauri = typeof window !== "undefined" &&
      Boolean((window as any).__TAURI_INTERNALS__ || (window as any).__TAURI__);
    if (!hasTauri) {
      console.warn("[WorkerManager] Skipping — Tauri not detected (browser-only mode)");
      return;
    }

    for (const def of WORKER_DEFINITIONS) {
      if (def.intervalMs) {
        this.startPeriodicWorker(def.type);
      }
    }

    this.startPolling();

    this.emit({
      type: "started",
      workerType: "health_check",
      message: "Background worker manager started",
    });
  }

  stop(): void {
    this._running = false;

    for (const [, interval] of this.intervals) {
      clearInterval(interval);
    }
    this.intervals.clear();

    if (this.pollHandle) {
      clearInterval(this.pollHandle);
      this.pollHandle = null;
    }

    this.emit({
      type: "stopped",
      workerType: "health_check",
      message: "Background worker manager stopped",
    });
  }

  async runWorker(type: WorkerType): Promise<void> {
    const def = WORKER_DEFINITIONS.find((d) => d.type === type);
    if (!def) throw new Error(`Unknown worker type: ${type}`);

    const info = this.workers.get(type);
    if (!info) throw new Error(`Worker not initialized: ${type}`);

    if (info.status === "running") return;

    const resolvedCommand = def.command === "PYTHON_DISCOVERED"
      ? this.pythonPath
      : def.command;
    const command = [resolvedCommand, ...def.args].join(" ");

    info.status = "running";
    info.lastError = null;
    info.lastStdoutLen = 0;
    info.lastStderrLen = 0;

    try {
      const processId = await ipcProcess.runBackground(command, this.memoryDir);
      info.processId = processId;
    } catch (err) {
      info.status = "error";
      info.lastError = err instanceof Error ? err.message : String(err);
      this.emit({
        type: "error",
        workerType: type,
        message: info.lastError,
      });
    }
  }

  getStatus(): Array<{
    type: WorkerType;
    status: WorkerStatus;
    lastRunAt: number | null;
    lastError: string | null;
    runCount: number;
    description: string;
    hasInterval: boolean;
    intervalMs: number | null;
  }> {
    return WORKER_DEFINITIONS.map((def) => {
      const info = this.workers.get(def.type)!;
      return {
        type: def.type,
        status: info.status,
        lastRunAt: info.lastRunAt,
        lastError: info.lastError,
        runCount: info.runCount,
        description: def.description,
        hasInterval: def.intervalMs !== null,
        intervalMs: def.intervalMs,
      };
    });
  }

  onEvent(handler: WorkerEventHandler): () => void {
    const id = ++this.handlerIdCounter;
    this.eventHandlers.set(id, handler);
    if (this.eventHandlers.size > 100) {
      const oldest = this.eventHandlers.keys().next().value;
      if (oldest !== undefined) {
        this.eventHandlers.delete(oldest);
      }
    }
    return () => this.eventHandlers.delete(id);
  }

  dispose(): void {
    this.stop();
    this.workers.clear();
    this.eventHandlers.clear();
    this.handlerIdCounter = 0;
  }

  // ── Internal ────────────────────────────────────────────────────────────

  private startPeriodicWorker(type: WorkerType): void {
    const def = WORKER_DEFINITIONS.find((d) => d.type === type);
    if (!def?.intervalMs) return;

    this.runWorker(type).catch((err) => { console.error("[WorkerManager] Worker failed:", err); });

    const interval = setInterval(() => {
      if (this._running) {
        this.runWorker(type).catch((err) => { console.error("[WorkerManager] Worker failed:", err); });
      }
    }, def.intervalMs);

    this.intervals.set(type, interval);
  }

  private startPolling(): void {
    this.pollHandle = setInterval(() => {
      if (!this._running) return;

      (async () => {
        for (const [type, info] of this.workers) {
          if (!info.processId) continue;

          try {
            const alive = await ipcProcess.isAlive(info.processId);

            if (!alive && info.status === "running") {
              info.status = "idle";
              info.lastRunAt = Date.now();
              info.runCount++;
              info.processId = null;

              this.emit({
                type: "completed",
                workerType: type,
                message: `Worker ${type} completed`,
              });
              continue;
            }

            if (alive) {
              const stdout = await ipcProcess.getStdout(info.processId);
              const stderr = await ipcProcess.getStderr(info.processId);

              const newStdout = stdout.slice(info.lastStdoutLen);
              const newStderr = stderr.slice(info.lastStderrLen);

              if (newStdout) {
                this.emit({
                  type: "progress",
                  workerType: type,
                  message: newStdout,
                });
                info.lastStdoutLen = stdout.length;
              }

              if (newStderr) {
                console.warn(`[Worker:${type}] stderr:`, newStderr);
                info.lastStderrLen = stderr.length;
              }
            }
          } catch {
            // Polling errors are non-fatal
          }
        }
      })().catch((err) => {
        console.error("[WorkerManager] Polling error:", err);
      });
    }, 5000);
  }

  private emit(event: WorkerEvent): void {
    for (const [, handler] of this.eventHandlers) {
      try {
        handler(event);
      } catch (err) {
        console.error("[WorkerManager] Event handler error:", err);
      }
    }
  }
}

// Singleton
let instance: BackgroundWorkerManager | null = null;

function getDefaultMemoryDir(): string {
  if (typeof process !== "undefined" && process.env?.HOME) {
    return `${process.env.HOME}/.config/agentic-memory`;
  }
  if (typeof process !== "undefined" && process.env?.USERPROFILE) {
    return `${process.env.USERPROFILE}/.config/agentic-memory`;
  }
  return "/tmp/agentic-memory";
}

export function getWorkerManager(memoryDir?: string): BackgroundWorkerManager {
  if (!instance) {
    if (!memoryDir) {
      memoryDir = getDefaultMemoryDir();
    }
    instance = new BackgroundWorkerManager(memoryDir);
  }
  return instance;
}
