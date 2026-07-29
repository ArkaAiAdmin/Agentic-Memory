/**
 * CommandPalette — Spotlight-style command palette.
 *
 * Cmd+K (or Cmd+Shift+P) opens the palette.
 * Type to filter commands. Enter to execute. Escape to close.
 */

import React, { useState, useEffect, useRef, useCallback } from "react";
import { commandRegistry, type Command } from "../../services/commands";

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

export function CommandPalette({ isOpen, onClose }: Props) {
  const [query, setQuery] = useState("");
  const [filtered, setFiltered] = useState<Command[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Filter commands when query changes
  useEffect(() => {
    if (!isOpen) {
      // Reset query state when palette closes — syncing with prop change
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setQuery("");
      setFiltered([]);
      setSelectedIndex(0);
      return;
    }
    const results = query
      ? commandRegistry.search(query)
      : commandRegistry.getAll();
    setFiltered(results);
    setSelectedIndex(0);
  }, [query, isOpen]);

  // Focus input on open
  useEffect(() => {
    if (isOpen) {
      // Small delay to ensure DOM is ready
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [isOpen]);

  const executeCommand = useCallback(
    async (cmd: Command) => {
      onClose();
      await cmd.run();
    },
    [onClose],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      switch (e.key) {
        case "Escape":
          onClose();
          break;
        case "ArrowDown":
          e.preventDefault();
          setSelectedIndex((i) => Math.min(i + 1, filtered.length - 1));
          break;
        case "ArrowUp":
          e.preventDefault();
          setSelectedIndex((i) => Math.max(i - 1, 0));
          break;
        case "Enter":
          e.preventDefault();
          if (filtered[selectedIndex]) {
            executeCommand(filtered[selectedIndex]);
          }
          break;
      }
    },
    [filtered, selectedIndex, onClose, executeCommand],
  );

  if (!isOpen) return null;

  // Group by category
  const grouped = new Map<string, Command[]>();
  for (const cmd of filtered) {
    const list = grouped.get(cmd.category) ?? [];
    list.push(cmd);
    grouped.set(cmd.category, list);
  }

  let flatIndex = 0;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Command Palette"
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: 9999,
        display: "flex",
        justifyContent: "center",
        paddingTop: "15vh",
        background: "rgba(0, 0, 0, 0.5)",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        style={{
          width: 520,
          maxHeight: 400,
          background: "#1e1e2e",
          borderRadius: 12,
          border: "1px solid #2a2a4a",
          boxShadow: "0 16px 48px rgba(0,0,0,0.4)",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
        onKeyDown={handleKeyDown}
      >
        {/* Input */}
        <div
          style={{
            padding: "12px 16px",
            borderBottom: "1px solid #2a2a4a",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <span style={{ color: "#666", fontSize: 14 }}>&gt;</span>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Type a command..."
            style={{
              flex: 1,
              background: "transparent",
              border: "none",
              outline: "none",
              color: "#e0e0e0",
              fontSize: 14,
              fontFamily: "inherit",
            }}
          />
        </div>

        {/* Results */}
        <div role="listbox" style={{ flex: 1, overflow: "auto", padding: "4px 0" }}>
          {filtered.length === 0 && (
            <div
              style={{
                padding: "20px 16px",
                color: "#666",
                fontSize: 13,
                textAlign: "center",
              }}
            >
              No commands found
            </div>
          )}

          {Array.from(grouped.entries()).map(([category, commands]) => (
            <div key={category}>
              <div
                style={{
                  padding: "6px 16px",
                  fontSize: 10,
                  fontWeight: 600,
                  color: "#666",
                  textTransform: "uppercase",
                  letterSpacing: 0.5,
                }}
              >
                {category}
              </div>
              {commands.map((cmd) => {
                const idx = flatIndex++;
                const isSelected = idx === selectedIndex;
                return (
                  <div
                    key={cmd.id}
                    role="option"
                    aria-selected={isSelected}
                    onClick={() => executeCommand(cmd)}
                    onMouseEnter={() => setSelectedIndex(idx)}
                    style={{
                      padding: "6px 16px",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      cursor: "pointer",
                      background: isSelected
                        ? "rgba(88, 166, 255, 0.1)"
                        : "transparent",
                      fontSize: 13,
                    }}
                  >
                    <span style={{ color: isSelected ? "#fff" : "#ccc" }}>
                      {cmd.icon && (
                        <span style={{ marginRight: 8 }}>{cmd.icon}</span>
                      )}
                      {cmd.title}
                    </span>
                    {cmd.keybinding && (
                      <span
                        style={{
                          fontSize: 11,
                          color: "#666",
                          background: "#2a2a4a",
                          padding: "2px 6px",
                          borderRadius: 4,
                        }}
                      >
                        {cmd.keybinding}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
