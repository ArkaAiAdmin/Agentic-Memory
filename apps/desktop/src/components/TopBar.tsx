/**
 * TopBar — Slim breadcrumb bar below window decoration.
 *
 * Shows project name, breadcrumbs, model selector, and memory health.
 * macOS: acts as drag region for window.
 */

import React from "react";
import { useAppStore } from "../stores/appStore";

export function TopBar() {
  const { activeProject, activeFile, providerConfig } = useAppStore();
  const projectName = activeProject?.split("/").pop() ?? "";
  const filePath = activeFile && activeProject ? activeFile.replace(activeProject + "/", "") : "";
  const pathParts = filePath ? filePath.split("/") : [];

  return (
    <div
      data-tauri-drag-region="true"
      style={{
        height: 36,
        background: "var(--bg-tertiary)",
        borderBottom: "1px solid var(--border-default)",
        display: "flex",
        alignItems: "center",
        padding: "0 80px 0 14px",
        gap: 6,
        fontSize: 11,
        color: "var(--text-tertiary)",
        flexShrink: 0,
      } as React.CSSProperties}
    >
      {/* Project name */}
      <span style={{ fontWeight: 600, color: "var(--text-secondary)", letterSpacing: "-0.01em" }}>
        {projectName || "No project"}
      </span>

      {/* Breadcrumb path */}
      {pathParts.length > 0 && (
        <span style={{ display: "flex", alignItems: "center", gap: 2 }} aria-label="File path">
        {pathParts.map((part, i) => (
        <React.Fragment key={i}>
          <span style={{ opacity: 0.3, fontSize: 10 }}>›</span>
          <span style={{
            color: i === pathParts.length - 1 ? "var(--text-primary)" : "var(--text-tertiary)",
            fontWeight: i === pathParts.length - 1 ? 500 : 400,
          }}>{part}</span>
        </React.Fragment>
        ))}
        </span>
      )}

      {/* Spacer */}
      <div style={{ flex: 1 }} />

      {/* Model pill */}
      <div style={{
        display: "flex", alignItems: "center", gap: 5,
        padding: "3px 10px", borderRadius: "var(--radius-full)",
        background: "var(--bg-elevated)", border: "1px solid var(--border-subtle)",
        fontSize: 10, fontWeight: 500, color: "var(--text-secondary)",
      }}>
        <span style={{ width: 5, height: 5, borderRadius: "50%", background: "var(--success)" }} />
        {providerConfig.model || providerConfig.type}
      </div>

      {/* Memory health dot */}
      <MemoryDot />
    </div>
  );
}

function MemoryDot() {
  const { memoryHealth } = useAppStore();
  const color =
    memoryHealth === "healthy" ? "var(--success)"
    : memoryHealth === "degraded" ? "var(--warning)"
    : memoryHealth === "unhealthy" ? "var(--error)"
    : "var(--text-tertiary)";

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
      <div style={{
        width: 6, height: 6, borderRadius: "50%", background: color,
        boxShadow: memoryHealth === "healthy" ? `0 0 3px ${color}` : undefined,
      }} />
    </div>
  );
}
