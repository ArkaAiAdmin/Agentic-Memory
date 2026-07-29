/**
 * MentionAutocomplete — @-mention popup in chat input.
 *
 * When the user types "@" in the chat, shows a dropdown of
 * files, symbols, memories, and KG entities from the project.
 * Selection injects scoped context into the next turn.
 */

import React, { useState, useEffect, useRef, useCallback } from "react";
import type { MentionItem, MentionKind } from "@ami/shared";
import { memoryBridge } from "@ami/memory-bridge";
import { useAppStore } from "../../stores/appStore";

interface Props {
  /** Whether the mention popup is visible. */
  isOpen: boolean;
  /** The query after "@" */
  query: string;
  /** Called when a mention is selected. */
  onSelect: (item: MentionItem) => void;
  /** Called when the popup should close. */
  onClose: () => void;
}

/** Hook to manage @-mention detection and querying. */
export function useMention(inputValue: string, onSelect: (item: MentionItem) => void) {
  const lastAtIndex = inputValue.lastIndexOf("@");
  const afterAt = lastAtIndex >= 0 ? inputValue.slice(lastAtIndex + 1) : "";
  const isOpen = lastAtIndex >= 0 && !afterAt.includes(" ") && !afterAt.includes("\n");
  const query = isOpen ? afterAt : "";

  const handleSelect = useCallback(
    (item: MentionItem) => {
      onSelect(item);
    },
    [onSelect],
  );

  return { isOpen, query, onSelect: handleSelect, onClose: () => {} };
}

export function MentionAutocomplete({ isOpen, query, onSelect, onClose }: Props) {
  const [items, setItems] = useState<MentionItem[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!isOpen) return;

    let cancelled = false;

    const fetchMentions = async () => {
      setLoading(true);
      const results: MentionItem[] = [];

      const { openFiles } = useAppStore.getState();
      for (const f of openFiles) {
        if (!query || f.name.toLowerCase().includes(query.toLowerCase())) {
          results.push({
            kind: "file",
            label: f.name,
            value: f.path,
            description: f.path,
            icon: "📄",
          });
        }
      }

      if (query.length > 0) {
        try {
          const memories = await memoryBridge.search({
            query,
            mode: "hybrid",
            limit: 5,
          });
          for (const m of memories) {
            results.push({
              kind: "memory",
              label: m.content.slice(0, 60),
              value: m.note_id,
              description: `[${m.category}] score: ${m.score.toFixed(2)}`,
              icon: "🧠",
            });
          }
        } catch {
          // memory search may fail if bridge is unavailable — show file/entity results only
        }
      }

      if (query.length > 1) {
        try {
          const entities = await memoryBridge.graphExplore(query);
          for (const e of entities.slice(0, 5)) {
            results.push({
              kind: "entity",
              label: e.name,
              value: e.name,
              description: `${e.type} — ${e.facts.length} facts`,
              icon: "◇",
            });
          }
        } catch {
          // graph explore may fail — degrade gracefully to file/memory suggestions
        }
      }

      if (!cancelled) {
        setItems(results);
        setSelectedIndex(0);
        setLoading(false);
      }
    };

    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(fetchMentions, 300);
    return () => {
      cancelled = true;
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [isOpen, query]);

  // Keyboard navigation
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          setSelectedIndex((i) => Math.min(i + 1, items.length - 1));
          break;
        case "ArrowUp":
          e.preventDefault();
          setSelectedIndex((i) => Math.max(i - 1, 0));
          break;
        case "Enter":
          e.preventDefault();
          if (items[selectedIndex]) {
            onSelect(items[selectedIndex]);
          }
          break;
        case "Escape":
          onClose();
          break;
      }
    },
    [items, selectedIndex, onSelect, onClose],
  );

  if (!isOpen || items.length === 0) return null;

  return (
    <div
      ref={containerRef}
      tabIndex={0}
      onKeyDown={handleKeyDown}
      style={{
        position: "absolute",
        bottom: "100%",
        left: 0,
        right: 0,
        maxHeight: 250,
        overflow: "auto",
        background: "#1e1e2e",
        border: "1px solid #2a2a4a",
        borderRadius: 8,
        boxShadow: "0 -8px 24px rgba(0,0,0,0.3)",
        marginBottom: 4,
        zIndex: 100,
      }}
    >
      {loading && (
        <div style={{ padding: "8px 12px", color: "#666", fontSize: 12 }}>
          Searching...
        </div>
      )}

      {/* Group by kind */}
      {(["file", "memory", "entity"] as MentionKind[]).map((kind) => {
        const group = items.filter((i) => i.kind === kind);
        if (group.length === 0) return null;
        return (
          <div key={kind}>
            <div
              style={{
                padding: "4px 12px",
                fontSize: 10,
                fontWeight: 600,
                color: "#666",
                textTransform: "uppercase",
              }}
            >
              {kind === "file" ? "Files" : kind === "memory" ? "Memories" : "Entities"}
            </div>
            {group.map((item) => {
              const idx = items.indexOf(item);
              return (
                <div
                  key={`${item.kind}-${item.value}`}
                  onClick={() => onSelect(item)}
                  onMouseEnter={() => setSelectedIndex(idx)}
                  style={{
                    padding: "5px 12px",
                    fontSize: 12,
                    cursor: "pointer",
                    background: idx === selectedIndex ? "rgba(88, 166, 255, 0.1)" : "transparent",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  <span>{item.icon}</span>
                  <span style={{ color: idx === selectedIndex ? "#fff" : "#c9d1d9" }}>
                    {item.label}
                  </span>
                  {item.description && (
                    <span style={{ fontSize: 10, color: "#666", marginLeft: "auto" }}>
                      {item.description}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}
