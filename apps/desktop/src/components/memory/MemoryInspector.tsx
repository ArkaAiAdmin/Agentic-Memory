import React from "react";
import { useAppStore } from "../../stores/appStore";

export function MemoryInspector() {
  const { recentMemories, activeDecisionThreads, kgNodes, memoryHealth } =
    useAppStore();

  return (
    <div style={{ padding: 12 }}>
      {/* Header */}
      <div
        style={{
          fontSize: 12,
          fontWeight: 600,
          color: "#888",
          marginBottom: 12,
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span>🧠 Memory Inspector</span>
        <span
          style={{
            fontSize: 10,
            padding: "1px 6px",
            borderRadius: 4,
            background:
              memoryHealth === "healthy"
                ? "#1b5e20"
                : memoryHealth === "degraded"
                  ? "#e65100"
                  : "#333",
            color:
              memoryHealth === "healthy"
                ? "#4caf50"
                : memoryHealth === "degraded"
                  ? "#ff9800"
                  : "#666",
          }}
        >
          {memoryHealth}
        </span>
      </div>

      {/* Recent Memories */}
      <section style={{ marginBottom: 16 }}>
        <h4 style={{ fontSize: 11, color: "#666", marginBottom: 6 }}>
          Recent Memories ({recentMemories.length})
        </h4>
        {recentMemories.length === 0 ? (
          <div style={{ fontSize: 11, color: "#444" }}>No memories yet</div>
        ) : (
          recentMemories.slice(0, 5).map((m, i) => (
            <div
              key={m.note_id ?? i}
              style={{
                padding: "4px 8px",
                background: "#16213e",
                borderRadius: 4,
                marginBottom: 4,
                fontSize: 11,
              }}
            >
              <span
                style={{
                  color: "#82aaff",
                  fontSize: 10,
                  marginRight: 6,
                }}
              >
                [{m.category}]
              </span>
              <span style={{ color: "#aaa" }}>
                {m.content.slice(0, 100)}
              </span>
              <span
                style={{ color: "#555", fontSize: 10, marginLeft: 4 }}
              >
                ({m.score.toFixed(2)})
              </span>
            </div>
          ))
        )}
      </section>

      {/* Decision Threads */}
      <section style={{ marginBottom: 16 }}>
        <h4 style={{ fontSize: 11, color: "#666", marginBottom: 6 }}>
          Decision Threads ({activeDecisionThreads.length})
        </h4>
        {activeDecisionThreads.length === 0 ? (
          <div style={{ fontSize: 11, color: "#444" }}>No active threads</div>
        ) : (
          activeDecisionThreads.map((thread) => (
            <div
              key={thread.id}
              style={{
                padding: "4px 8px",
                background: "#16213e",
                borderRadius: 4,
                marginBottom: 4,
              }}
            >
              <div style={{ fontSize: 11, color: "#c0c0c0" }}>
                {thread.title}
              </div>
              <div style={{ fontSize: 10, color: "#555" }}>
                {thread.events.length} events · {thread.status}
              </div>
            </div>
          ))
        )}
      </section>

      {/* KG Nodes */}
      <section>
        <h4 style={{ fontSize: 11, color: "#666", marginBottom: 6 }}>
          Knowledge Graph ({kgNodes.length})
        </h4>
        {kgNodes.length === 0 ? (
          <div style={{ fontSize: 11, color: "#444" }}>No entities yet</div>
        ) : (
          kgNodes.slice(0, 5).map((node) => (
            <div
              key={node.id}
              style={{
                padding: "4px 8px",
                background: "#16213e",
                borderRadius: 4,
                marginBottom: 4,
                fontSize: 11,
              }}
            >
              <span style={{ color: "#c792ea" }}>{node.name}</span>
              <span style={{ color: "#555", marginLeft: 6 }}>
                ({node.type}) — {node.facts.length} facts
              </span>
            </div>
          ))
        )}
      </section>
    </div>
  );
}
