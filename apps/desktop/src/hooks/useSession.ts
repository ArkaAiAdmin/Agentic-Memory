/**
 * useSession Hook
 *
 * Manages the agent session lifecycle: start, end, compaction.
 * Integrates with the memory bridge and app store.
 */

import { useCallback, useEffect, useRef } from "react";
import { useAppStore } from "../stores/appStore";
import { memoryBridge } from "@ami/memory-bridge";

export function useSession() {
  const {
    memoryHealth,
    setMemoryHealth,
    setRecentMemories,
    setDecisionThreads,
  } = useAppStore();

  const sessionRef = useRef<string | null>(null);

  /**
   * Start a new session for a project.
   */
  const startSession = useCallback(
    async (projectRoot: string) => {
      if (!memoryBridge.isRunning) return;

      try {
        const briefing = await memoryBridge.sessionStart(projectRoot);
        sessionRef.current = briefing.session_id;

        // Update store with briefing data
        if (briefing.active_threads) {
          setDecisionThreads(briefing.active_threads);
        }
        if (briefing.recent_memories) {
          setRecentMemories(briefing.recent_memories);
        }

        return briefing;
      } catch (err) {
        console.error("Failed to start session:", err);
        setMemoryHealth("degraded");
      }
    },
    [setDecisionThreads, setRecentMemories, setMemoryHealth],
  );

  /**
   * End the current session.
   */
  const endSession = useCallback(
    async (summary: string) => {
      if (!sessionRef.current || !memoryBridge.isRunning) return;

      try {
        await memoryBridge.sessionEnd(sessionRef.current, summary);
        sessionRef.current = null;
      } catch (err) {
        console.error("Failed to end session:", err);
      }
    },
    [],
  );

  /**
   * Check memory health periodically.
   */
  const checkHealth = useCallback(async () => {
    if (!memoryBridge.isRunning) {
      setMemoryHealth("unknown");
      return;
    }

    try {
      const health = await memoryBridge.healthCheck();
      setMemoryHealth(health.status);
    } catch {
      setMemoryHealth("unhealthy");
    }
  }, [setMemoryHealth]);

  return {
    sessionId: sessionRef.current,
    startSession,
    endSession,
    checkHealth,
    isMemoryRunning: memoryBridge.isRunning,
  };
}
