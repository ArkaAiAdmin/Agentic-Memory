/**
 * Keyboard Shortcut Reference Panel
 */

import React, { useEffect } from "react";
import { commandRegistry } from "../../services/commands";

interface Props {
  onClose: () => void;
}

export function ShortcutReference({ onClose }: Props) {
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const commands = commandRegistry.getAll();
  const byCategory = new Map<string, typeof commands>();
  for (const cmd of commands) {
    const existing = byCategory.get(cmd.category) || [];
    existing.push(cmd);
    byCategory.set(cmd.category, existing);
  }

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 1001,
      background: "rgba(0,0,0,0.6)", display: "flex", alignItems: "center", justifyContent: "center",
      backdropFilter: "blur(4px)",
    }} onClick={onClose} role="dialog" aria-modal="true" aria-label="Keyboard Shortcuts">
      <div onClick={(e) => e.stopPropagation()} style={{
        background: "var(--bg-elevated)", borderRadius: "var(--radius-xl)", padding: 28,
        width: 640, maxWidth: "92vw", maxHeight: "80vh", overflow: "auto",
        boxShadow: "var(--shadow-lg)", border: "1px solid var(--border-default)",
      }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
          <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600, color: "var(--text-primary)" }}>
            Keyboard Shortcuts
          </h2>
          <button onClick={onClose} style={{
            background: "none", border: "none", color: "var(--text-tertiary)",
            fontSize: 20, cursor: "pointer", lineHeight: 1,
          }}>x</button>
        </div>

        {Array.from(byCategory.entries()).map(([category, cmds]) => (
          <div key={category} style={{ marginBottom: 20 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>
              {category}
            </div>
            {cmds.map((cmd) => (
              <div key={cmd.id} style={{
                display: "flex", alignItems: "center", justifyContent: "space-between",
                padding: "6px 0", borderBottom: "1px solid var(--border-subtle)", fontSize: 13,
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontSize: 14 }}>{cmd.icon || ""}</span>
                  <span style={{ color: "var(--text-primary)" }}>{cmd.title}</span>
                </div>
                {cmd.keybinding ? (
                  <span style={{
                    padding: "2px 8px", borderRadius: "var(--radius-sm)",
                    background: "var(--bg-tertiary)", border: "1px solid var(--border-default)",
                    fontSize: 11, color: "var(--text-secondary)", fontFamily: "var(--font-mono)",
                  }}>
                    {cmd.keybinding}
                  </span>
                ) : (
                  <span style={{ fontSize: 11, color: "var(--text-tertiary)" }}>—</span>
                )}
              </div>
            ))}
          </div>
        ))}

        <div style={{ marginTop: 16, fontSize: 11, color: "var(--text-tertiary)", textAlign: "center" }}>
          Press Esc to close
        </div>
      </div>
    </div>
  );
}
