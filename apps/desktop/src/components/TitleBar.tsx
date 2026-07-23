import React from "react";
import { useAppStore } from "../stores/appStore";

export function TitleBar({ onOpenProject }: { onOpenProject?: () => void }) {
  const { activeProject, toggleSidebar, toggleMemoryPanel, toggleTerminal, memoryHealth } =
    useAppStore();

  const projectName = activeProject?.split("/").pop() ?? "No Project";
  const healthColor =
    memoryHealth === "healthy"
      ? "#4caf50"
      : memoryHealth === "degraded"
        ? "#ff9800"
        : memoryHealth === "unhealthy"
          ? "#f44336"
          : "#666";

  return (
    <div
      style={{
        height: 40,
        background: "#16213e",
        borderBottom: "1px solid #2a2a4a",
        display: "flex",
        alignItems: "center",
        padding: "0 12px",
        gap: 10,
        userSelect: "none",
      }}
    >
      {/* App icon */}
      <img
        src="/cursor.svg"
        alt=""
        style={{ width: 22, height: 22, borderRadius: 6, flexShrink: 0 }}
        draggable={false}
      />

      {/* Project info */}
      <span style={{ fontWeight: 600, fontSize: 13 }}>{projectName}</span>

      {/* Memory health indicator */}
      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
        <div
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: healthColor,
          }}
        />
        <span style={{ fontSize: 11, color: "#888" }}>Memory</span>
      </div>

      {/* Spacer */}
      <div style={{ flex: 1 }} />

      {/* Toolbar buttons */}
      <button
        onClick={onOpenProject}
        style={btnStyle}
        title="Open project"
      >
        Open
      </button>
      <button
        onClick={toggleSidebar}
        style={btnStyle}
        title="Toggle file explorer"
      >
        Files
      </button>
      <button
        onClick={toggleTerminal}
        style={btnStyle}
        title="Toggle terminal"
      >
        Terminal
      </button>
      <button
        onClick={toggleMemoryPanel}
        style={btnStyle}
        title="Toggle memory inspector"
      >
        Memory
      </button>
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  background: "transparent",
  border: "1px solid #2a2a4a",
  color: "#aaa",
  padding: "2px 8px",
  borderRadius: 4,
  fontSize: 11,
  cursor: "pointer",
};
