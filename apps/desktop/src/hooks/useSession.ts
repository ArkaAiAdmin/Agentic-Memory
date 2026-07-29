/**
 * useSession Hook
 *
 * Manages the agent session lifecycle: start, end, compaction.
 * Integrates with the memory bridge and app store.
 * Auto-starts on mount and monitors health periodically.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores/appStore";
import { memoryBridge } from "@ami/memory-bridge";

export function useSession() {
  const {
    setMemoryHealth,
    setRecentMemories,
    setDecisionThreads,
    activeProject,
  } = useAppStore();

  const [sessionId, setSessionId] = useState<string | null>(null);
  const healthIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const sessionStartingRef = useRef(false);

  const startSession = useCallback(
    async (projectRoot?: string) => {
      if (!memoryBridge.isRunning || sessionStartingRef.current) return;

      sessionStartingRef.current = true;
      try {
        const root = projectRoot || activeProject || "/Users/arka/.config/agentic-memory";
        const briefing = await memoryBridge.sessionStart(root);
        setSessionId(briefing.session_id);

        if (briefing.active_threads) {
          setDecisionThreads(briefing.active_threads);
        }
        if (briefing.recent_memories) {
          setRecentMemories(briefing.recent_memories);
        }

        setMemoryHealth("healthy");
        return briefing;
      } catch (err) {
        console.error("Failed to start session:", err);
        setMemoryHealth("degraded");
      } finally {
        sessionStartingRef.current = false;
      }
    },
    [activeProject, setDecisionThreads, setRecentMemories, setMemoryHealth],
  );

  const startSessionRef = useRef(startSession);

  useEffect(() => {
    startSessionRef.current = startSession;
  });

  /**
   * End the current session.
   */
  const endSession = useCallback(
    async (summary: string) => {
      if (!sessionId || !memoryBridge.isRunning) return;

      try {
        await memoryBridge.sessionEnd(sessionId, summary);
        setSessionId(null);
      } catch (err) {
        console.error("Failed to end session:", err);
      }
    },
    [sessionId],
  );

  /**
   * Check memory health.
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

  // Auto-start session on mount and set up health polling.
  // startSession() manages its own state — standard async init pattern
  useEffect(() => {
    if (memoryBridge.isRunning && !sessionId) {
      startSessionRef.current();
    }

    // Periodic health check every 30s
    healthIntervalRef.current = setInterval(() => {
      checkHealth();
    }, 30_000);

    // Initial health check
    checkHealth();

    return () => {
      if (healthIntervalRef.current) {
        clearInterval(healthIntervalRef.current);
      }
    };
  }, [checkHealth, sessionId]);

  return {
    sessionId,
    startSession,
    endSession,
    checkHealth,
    isMemoryRunning: memoryBridge.isRunning,
  };
}
