/**
 * useMemory Hook
 *
 * Provides access to the memory system: search, save, graph exploration.
 * Subscribes to memory events for real-time updates.
 */

import { useCallback, useEffect } from "react";
import { useAppStore } from "../stores/appStore";
import { memoryBridge, memoryEventBus } from "@ami/memory-bridge";
import type { SearchQuery, SavePayload, SearchResult } from "@ami/shared";

export function useMemory() {
  const { setRecentMemories, setKgNodes } = useAppStore();

  /**
   * Search memories.
   */
  const search = useCallback(
    async (query: SearchQuery): Promise<SearchResult[]> => {
      if (!memoryBridge.isRunning) return [];

      try {
        const results = await memoryBridge.search(query);
        setRecentMemories(results);
        return results;
      } catch (err) {
        console.error("Memory search failed:", err);
        return [];
      }
    },
    [setRecentMemories],
  );

  /**
   * Save a memory.
   */
  const save = useCallback(
    async (payload: SavePayload): Promise<string | null> => {
      if (!memoryBridge.isRunning) return null;

      try {
        return await memoryBridge.save(payload);
      } catch (err) {
        console.error("Memory save failed:", err);
        return null;
      }
    },
    [],
  );

  /**
   * Explore the knowledge graph.
   */
  const exploreGraph = useCallback(
    async (query: string) => {
      if (!memoryBridge.isRunning) return [];

      try {
        const nodes = await memoryBridge.graphExplore(query);
        setKgNodes(nodes);
        return nodes;
      } catch (err) {
        console.error("KG explore failed:", err);
        return [];
      }
    },
    [setKgNodes],
  );

  /**
   * Subscribe to memory events for real-time updates.
   */
  useEffect(() => {
    const unsubSaved = memoryEventBus.on("memory.saved", (event) => {
      if (event.type === "memory.saved" && event.category === "auto_save") return;
      if (!memoryBridge.isRunning) return;
      memoryBridge.search({ query: "", limit: 10 }).then((results) => {
        useAppStore.getState().setRecentMemories(results);
      }).catch(() => {});
    });

    return () => {
      unsubSaved();
    };
  }, []);

  return {
    search,
    save,
    exploreGraph,
    isRunning: memoryBridge.isRunning,
  };
}
