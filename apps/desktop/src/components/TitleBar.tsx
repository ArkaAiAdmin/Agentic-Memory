import React from "react";

/**
 * TitleBar — macOS-style window decoration with dynamic theming.
 * Uses frosted glass effect and theme-responsive accent line.
 */
export function TitleBar() {
  return (
    <div className="titlebar" data-tauri-drag-region>
      {/* Traffic light spacer (macOS window controls occupy ~70px left) */}
      <div style={{ width: 70, flexShrink: 0 }} />

      {/* App title — subtle, monochrome */}
      <span style={{
        fontSize: 12,
        fontWeight: 500,
        color: "var(--text-tertiary)",
        letterSpacing: "0.02em",
        userSelect: "none",
        opacity: 0.7,
      }}>
        Agentic Memory
      </span>

      {/* Balance spacer */}
      <div style={{ width: 70, flexShrink: 0 }} />
    </div>
  );
}
