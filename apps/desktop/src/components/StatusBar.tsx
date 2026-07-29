/**
 * StatusBar — Bottom bar with status info.
 *
 * Shows memory health, current mode, git branch, and connection status.
 */

import React, { useState } from "react";
import { useAppStore } from "../stores/appStore";
import { getCurrentMode, onModeChange } from "../services/agentModes";

export function StatusBar() {
  const { memoryHealth, theme, providerConfig, agents } = useAppStore();
  const [mode, setMode] = React.useState(getCurrentMode());
  const [showHealthPopover, setShowHealthPopover] = useState(false);

  React.useEffect(() => onModeChange(setMode), []);

  const healthColor =
    memoryHealth === "healthy" ? "var(--success)"
    : memoryHealth === "degraded" ? "var(--warning)"
    : memoryHealth === "unhealthy" ? "var(--error)"
    : "var(--text-tertiary)";

  const healthLabel =
    memoryHealth === "healthy" ? "Memory: Ready"
    : memoryHealth === "degraded" ? "Memory: Degraded"
    : memoryHealth === "unhealthy" ? "Memory: Error"
    : "Memory: Offline";

  return (
    <div style={{
      height: 26,
      background: "var(--bg-tertiary)",
      borderTop: "1px solid var(--border-default)",
      display: "flex",
      alignItems: "center",
      padding: "0 14px",
      gap: 16,
      fontSize: 11,
      color: "var(--text-tertiary)",
      flexShrink: 0,
    }}>
      {/* Memory status (clickable) */}
      <div
        onClick={() => setShowHealthPopover(!showHealthPopover)}
        style={{ display: "flex", alignItems: "center", gap: 5, cursor: "pointer", position: "relative" }}
      >
        <div style={{
          width: 7, height: 7, borderRadius: "50%", background: healthColor,
          boxShadow: memoryHealth === "healthy" ? `0 0 4px ${healthColor}` : undefined,
        }} />
        <span>{healthLabel}</span>

        {/* Health popover */}
        {showHealthPopover && (
          <div style={{
            position: "absolute", bottom: "100%", left: 0, marginBottom: 8,
            background: "var(--bg-elevated)", border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-md)", padding: "10px 14px", minWidth: 200,
            boxShadow: "var(--shadow-lg)", zIndex: 9999, fontSize: 11,
          }}>
            <div style={{ fontWeight: 600, marginBottom: 6, color: "var(--text-primary)" }}>Memory System</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <StatusRow label="Status" value={memoryHealth} color={healthColor} />
              <StatusRow label="Provider" value={providerConfig.type} />
              <StatusRow label="Model" value={providerConfig.model || "default"} />
              <StatusRow label="Agents" value={String(agents.length)} />
            </div>
          </div>
        )}
      </div>

      {/* Current mode */}
      {mode !== "chat" && (
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <span style={{ color: "var(--accent)", fontSize: 8 }}>●</span>
          <span>{mode.charAt(0).toUpperCase() + mode.slice(1)} Mode</span>
        </div>
      )}

      {/* Active agents */}
      {agents.length > 0 && (
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <span style={{ color: "var(--info)" }}>⚡</span>
          <span>{agents.length} agent{agents.length > 1 ? "s" : ""}</span>
        </div>
      )}

      {/* Spacer */}
      <div style={{ flex: 1 }} />

      {/* Provider indicator */}
      <span style={{ color: "var(--text-tertiary)" }}>
        {providerConfig.type === "lmstudio" ? "LM Studio" : providerConfig.type}
      </span>

      {/* Theme indicator */}
      <span style={{ textTransform: "capitalize" }}>{theme}</span>
    </div>
  );
}

function StatusRow({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between" }}>
      <span style={{ color: "var(--text-tertiary)" }}>{label}</span>
      <span style={{ color: color || "var(--text-secondary)", fontWeight: 500 }}>{value}</span>
    </div>
  );
}
