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
  type: "started" | "completed" | "error" | "progress";
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

const WORKER_DEFINITIONS: WorkerDefinition[] = [
  {
    type: "index_rebuild",
    command: "python",
    args: ["-m", "agentic_memory.background.indexer", "--rebuild"],
    intervalMs: null,
    description: "Rebuild search indices when project files change",
  },
  {
    type: "embedding",
    command: "python",
    args: ["-m", "agentic_memory.background.embedding_worker"],
    intervalMs: 30_000,
    description: "Compute embeddings for new memories",
  },
  {
    type: "contradiction",
    command: "python",
    args: ["-m", "agentic_memory.background.contradiction_detector"],
    intervalMs: 60_000,
    description: "Detect contradictions between memories",
  },
  {
    type: "kg_extraction",
    command: "python",
    args: ["-m", "agentic_memory.background.kg_extractor"],
    intervalMs: 45_000,
    description: "Extract entities and facts from new memories",
  },
  {
    type: "health_check",
    command: "python",
    args: ["-m", "agentic_memory.background.health_check"],
    intervalMs: 120_000,
    description: "Check memory system health",
  },
  {
    type: "integrity_check",
    command: "python",
    args: ["-m", "agentic_memory.background.integrity_check"],
    intervalMs: 300_000,
    description: "Verify database integrity",
  },
  {
    type: "consolidation",
    command: "python",
    args: ["-m", "agentic_memory.background.consolidation"],
    intervalMs: 600_000,
    description: "Consolidate and merge duplicate memories",
  },
  {
    type: "backup",
    command: "python",
    args: ["-m", "agentic_memory.background.backup"],
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
  private eventHandlers: Set<WorkerEventHandler> = new Set();
  private memoryDir: string;
  private _running = false;
  private pollHandle: ReturnType<typeof setInterval> | null = null;

  constructor(memoryDir: string) {
    this.memoryDir = memoryDir;

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

  get isRunning(): boolean {
    return this._running;
  }

  start(): void {
    if (this._running) return;
    this._running = true;

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

    for (const [type, interval] of this.intervals) {
      clearInterval(interval);
    }
    this.intervals.clear();

    if (this.pollHandle) {
      clearInterval(this.pollHandle);
      this.pollHandle = null;
    }

    this.emit({
      type: "started",
      workerType: "health_check",
      message: "Background worker manager stopped",
    });
  }

  async runWorker(type: WorkerType): Promise<void> {
    const def = WORKER_DEFINITIONS.find((d) => d.type === type);
    if (!def) throw new Error(`Unknown worker type: ${type}`);

    const info = this.workers.get(type);
    if (!info) throw new Error(`Worker not initialized: ${type}`);

    const command = [def.command, ...def.args].join(" ");

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
    this.eventHandlers.add(handler);
    return () => this.eventHandlers.delete(handler);
  }

  // ── Internal ────────────────────────────────────────────────────────────

  private startPeriodicWorker(type: WorkerType): void {
    const def = WORKER_DEFINITIONS.find((d) => d.type === type);
    if (!def?.intervalMs) return;

    this.runWorker(type).catch(() => {});

    const interval = setInterval(() => {
      if (this._running) {
        this.runWorker(type).catch(() => {});
      }
    }, def.intervalMs);

    this.intervals.set(type, interval);
  }

  private startPolling(): void {
    this.pollHandle = setInterval(async () => {
      if (!this._running) return;

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
              console.error(`[Worker:${type}] stderr:`, newStderr);
              info.lastStderrLen = stderr.length;
            }
          }
        } catch {
          // Polling errors are non-fatal
        }
      }
    }, 2000);
  }

  private emit(event: WorkerEvent): void {
    for (const handler of this.eventHandlers) {
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

export function getWorkerManager(memoryDir?: string): BackgroundWorkerManager {
  if (!instance) {
    if (!memoryDir) {
      memoryDir = "/Users/arka/.config/agentic-memory";
    }
    instance = new BackgroundWorkerManager(memoryDir);
  }
  return instance;
}
