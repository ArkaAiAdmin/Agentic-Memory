/**
 * Session Manager (TypeScript wrapper)
 *
 * Wraps the Python SessionManager via the memory bridge.
 * Manages session lifecycle: start, compact, end.
 */

import type {
  Session,
  DecisionThread,
} from "@ami/shared";
import type { MemoryBridgeClient } from "@ami/memory-bridge";

export class SessionManager {
  private activeSession: Session | null = null;

  constructor(private readonly memory: MemoryBridgeClient) {}

  /**
   * Start a new session or resume an existing one.
   */
  async start(projectRoot: string): Promise<Session> {
    const briefing = await this.memory.sessionStart(projectRoot);

    this.activeSession = {
      id: briefing.session_id,
      project_root: projectRoot,
      started_at: Date.now(),
      status: "active",
      decision_threads: briefing.active_threads ?? [],
    };

    return this.activeSession;
  }

  /**
   * End the current session with a summary.
   */
  async end(summary: string): Promise<void> {
    if (!this.activeSession) return;

    await this.memory.sessionEnd(this.activeSession.id, summary);
    this.activeSession.status = "ended";
    this.activeSession.ended_at = Date.now();
    this.activeSession.summary = summary;
  }

  /**
   * Compact the session when context window fills up.
   */
  async compactIfNeeded(): Promise<boolean> {
    if (!this.activeSession) return false;

    // Check if we need compaction (simplified — real impl checks token count)
    const result = await this.memory.compactSession(this.activeSession.id);

    if (result.context) {
      this.activeSession.context_window = {
        token_count: 0,
        message_count: 0,
        last_compacted_at: Date.now(),
      };
      return true;
    }

    return false;
  }

  /**
   * Get the active session.
   */
  getSession(): Session | null {
    return this.activeSession;
  }

  /**
   * Get active decision threads.
   */
  getActiveThreads(): DecisionThread[] {
    return this.activeSession?.decision_threads.filter(
      (t) => t.status === "open",
    ) ?? [];
  }
}
