import React, { useState, useEffect, useCallback } from "react";
import { useAppStore } from "../../stores/appStore";
import { getWorkerManager, type WorkerEvent } from "../../services/workerManager";

/**
 * Worker Status Panel
 *
 * Shows the status of background memory workers:
 * - Index rebuild, embedding, contradiction detection, KG extraction
 * - Health checks, integrity checks, consolidation, backups
 */

export function WorkerStatusPanel() {
  const { theme } = useAppStore();
  const workerManager = getWorkerManager();
  const [workers, setWorkers] = useState<ReturnType<typeof getWorkerManager>["getStatus"] extends () => infer R ? R : never>(() => workerManager.getStatus());
  const [logs, setLogs] = useState<string[]>([]);

  const refreshStatus = useCallback(() => {
    setWorkers(workerManager.getStatus());
  }, [workerManager]);

  useEffect(() => {
    const unsub = workerManager.onEvent((event: WorkerEvent) => {
      setWorkers(workerManager.getStatus());
      setLogs((prev) =>
        [`[${new Date().toLocaleTimeString()}] ${event.workerType}: ${event.message}`, ...prev].slice(0, 100),
      );
    });

    return unsub;
  }, [workerManager]);

  const isDark = theme === "dark";
  const bgColor = isDark ? "#161b22" : "#fff";
  const textColor = isDark ? "#c9d1d9" : "#24292f";
  const borderColor = isDark ? "#30363d" : "#d0d7de";

  const statusColor = (status: string) => {
    switch (status) {
      case "running":
        return "#3fb950";
      case "error":
        return "#f85149";
      case "idle":
        return "#8b949e";
      default:
        return "#666";
    }
  };

  const statusIcon = (status: string) => {
    switch (status) {
      case "running":
        return "⟳";
      case "error":
        return "✕";
      case "idle":
        return "✓";
      default:
        return "○";
    }
  };

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: bgColor,
        color: textColor,
        fontSize: 12,
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "8px 12px",
          borderBottom: `1px solid ${borderColor}`,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <span style={{ fontWeight: 600 }}>Background Workers</span>
        <div style={{ display: "flex", gap: 4 }}>
          <button
            onClick={() => {
              if (workerManager.isRunning) {
                workerManager.stop();
              } else {
                workerManager.start();
              }
              refreshStatus();
            }}
            style={{
              background: workerManager.isRunning ? "#da3633" : "#238636",
              border: "none",
              color: "#fff",
              cursor: "pointer",
              padding: "2px 8px",
              borderRadius: 4,
              fontSize: 11,
            }}
          >
            {workerManager.isRunning ? "Stop All" : "Start All"}
          </button>
        </div>
      </div>

      {/* Worker list */}
      <div style={{ flex: 1, overflow: "auto", padding: "8px 12px" }}>
        {workers.map((w) => (
          <div
            key={w.type}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "6px 0",
              borderBottom: `1px solid ${isDark ? "#21262d" : "#e1e4e8"}`,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span
                style={{
                  color: statusColor(w.status),
                  fontSize: 14,
                  animation: w.status === "running" ? "spin 1s linear infinite" : "none",
                }}
              >
                {statusIcon(w.status)}
              </span>
              <div>
                <div style={{ fontWeight: 500, fontSize: 11 }}>
                  {w.type.replace(/_/g, " ")}
                </div>
                <div style={{ fontSize: 10, color: "#888" }}>
                  {w.description}
                </div>
              </div>
            </div>
            <div style={{ textAlign: "right", fontSize: 10, color: "#888" }}>
              <div>Runs: {w.runCount}</div>
              {w.lastRunAt && (
                <div>{new Date(w.lastRunAt).toLocaleTimeString()}</div>
              )}
              {w.lastError && (
                <div style={{ color: "#f85149" }}>{w.lastError}</div>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Recent logs */}
      {logs.length > 0 && (
        <div
          style={{
            borderTop: `1px solid ${borderColor}`,
            maxHeight: 120,
            overflow: "auto",
            padding: "4px 12px",
          }}
        >
          <div
            style={{
              fontSize: 10,
              fontWeight: 600,
              color: "#888",
              marginBottom: 4,
            }}
          >
            Recent Activity
          </div>
          {logs.slice(0, 20).map((log, i) => (
            <div
              key={i}
              style={{
                fontSize: 10,
                color: "#666",
                fontFamily: "monospace",
                lineHeight: 1.4,
              }}
            >
              {log}
            </div>
          ))}
        </div>
      )}

      <style>{`
        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
