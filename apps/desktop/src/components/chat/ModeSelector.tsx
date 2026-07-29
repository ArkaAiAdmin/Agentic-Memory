/**
 * ModeSelector — Hamburger menu with mode toggles.
 *
 * Compact icon button that opens a dropdown with toggle switches
 * for Plan, Build, Spec modes. Chat is always on (default).
 */

import React, { useState, useEffect, useRef } from "react";
import {
  getCurrentMode,
  setMode,
  onModeChange,
  type AgentMode,
} from "../../services/agentModes";

const MODES: Array<{ id: AgentMode; label: string; description: string }> = [
  { id: "plan", label: "Plan Mode", description: "Analyze and plan before acting" },
  { id: "build", label: "Build Mode", description: "Focus on build commands and errors" },
  { id: "spec", label: "Spec Mode", description: "Create specifications and PRDs" },
];

export function ModeSelector() {
  const [mode, setModeState] = useState<AgentMode>(() => getCurrentMode());
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => onModeChange(setModeState), []);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const hasActive = mode !== "chat";

  const toggleMode = (id: AgentMode) => {
    if (mode === id) {
      setMode("chat"); // Revert to chat
    } else {
      setMode(id);
    }
  };

  return (
    <div ref={ref} style={{ position: "relative" }}>
      {/* Hamburger button */}
      <button
        onClick={() => setOpen(!open)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          padding: "4px 8px",
          borderRadius: "var(--radius-sm)",
          border: `1px solid ${hasActive ? "var(--accent)" : "var(--border-default)"}`,
          background: hasActive ? "var(--accent-muted)" : "var(--bg-tertiary)",
          color: hasActive ? "var(--accent)" : "var(--text-tertiary)",
          fontSize: 11,
          fontWeight: 500,
          cursor: "pointer",
          transition: "all 0.12s",
        }}
      >
        {/* Hamburger icon */}
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
          <line x1="2" y1="3.5" x2="12" y2="3.5" />
          <line x1="2" y1="7" x2="12" y2="7" />
          <line x1="2" y1="10.5" x2="12" y2="10.5" />
        </svg>
        {hasActive && (
          <span style={{ fontSize: 9, padding: "1px 4px", borderRadius: "var(--radius-xs)", background: "var(--accent)", color: "var(--accent-text)" }}>
            1
          </span>
        )}
      </button>

      {/* Dropdown menu — opens upward */}
      {open && (
        <div style={{
          position: "absolute", bottom: "100%", left: 0, marginBottom: 4, zIndex: 200,
          minWidth: 220, background: "var(--bg-elevated)", border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-md)", boxShadow: "var(--shadow-lg)", overflow: "hidden",
          animation: "scaleIn 0.12s ease",
        }}>
          <div style={{ padding: "6px 12px", fontSize: 10, fontWeight: 600, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: 0.5 }}>
            Agent Mode
          </div>
          {MODES.map((m) => {
            const isActive = mode === m.id;
            return (
              <div
                key={m.id}
                onClick={() => toggleMode(m.id)}
                style={{
                  padding: "8px 12px", display: "flex", alignItems: "center", justifyContent: "space-between",
                  cursor: "pointer", borderBottom: "1px solid var(--border-subtle)",
                  background: isActive ? "var(--accent-muted)" : "transparent",
                }}
                onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.background = "var(--bg-hover)"; }}
                onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.background = isActive ? "var(--accent-muted)" : "transparent"; }}
              >
                <div>
                  <div style={{ fontSize: 12, fontWeight: 500, color: "var(--text-primary)" }}>{m.label}</div>
                  <div style={{ fontSize: 10, color: "var(--text-tertiary)", marginTop: 1 }}>{m.description}</div>
                </div>
                {/* Toggle switch */}
                <div style={{
                  width: 32, height: 18, borderRadius: 9,
                  background: isActive ? "var(--accent)" : "var(--bg-tertiary)",
                  border: `1px solid ${isActive ? "var(--accent)" : "var(--border-default)"}`,
                  position: "relative", transition: "all 0.15s", flexShrink: 0,
                }}>
                  <div style={{
                    width: 14, height: 14, borderRadius: "50%",
                    background: isActive ? "var(--accent-text)" : "var(--text-tertiary)",
                    position: "absolute", top: 1, left: isActive ? 15 : 1,
                    transition: "all 0.15s",
                  }} />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
