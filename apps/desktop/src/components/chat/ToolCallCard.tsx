/**
 * ToolCallCard — Tool-call approval UI.
 *
 * Shows tool name, arguments, and Approve/Deny/Always-allow buttons.
 * Used in the chat panel when a tool call requires approval.
 */

import React, { useState } from "react";
import type { ToolApprovalRequest, ToolApprovalDecision } from "../../services/toolApproval";

interface Props {
  request: ToolApprovalRequest;
  onDecide: (requestId: string, decision: ToolApprovalDecision) => void;
}

export function ToolCallCard({ request, onDecide }: Props) {
  const [expanded, setExpanded] = useState(false);

  const isMutating = ["writeFile", "runCommand", "gitCommit"].includes(request.toolName);

  return (
    <div
      style={{
        marginTop: 6,
        background: "#1a1a2e",
        border: `1px solid ${isMutating ? "#eab308" : "#2a2a4a"}`,
        borderRadius: 8,
        padding: "8px 12px",
        fontSize: 12,
        maxWidth: "85%",
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          cursor: "pointer",
        }}
        onClick={() => setExpanded(!expanded)}
      >
        <span style={{ color: "#eab308", fontWeight: 600 }}>
          🔧 {request.toolName}
        </span>
        {isMutating && (
          <span
            style={{
              fontSize: 9,
              padding: "1px 5px",
              borderRadius: 3,
              background: "rgba(234, 179, 8, 0.15)",
              color: "#eab308",
            }}
          >
            MUTATING
          </span>
        )}
        <span style={{ color: "#666", fontSize: 10, marginLeft: "auto" }}>
          {expanded ? "▼" : "▶"}
        </span>
      </div>

      {/* Expanded args */}
      {expanded && (
        <pre
          style={{
            margin: "6px 0",
            padding: 6,
            background: "#0d1117",
            borderRadius: 4,
            color: "#888",
            fontSize: 10,
            overflow: "auto",
            maxHeight: 120,
          }}
        >
          {JSON.stringify(request.args, null, 2)}
        </pre>
      )}

      {/* Approval buttons */}
      <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
        <button
          onClick={() => onDecide(request.id, "approve")}
          style={{
            padding: "4px 10px",
            borderRadius: 4,
            border: "none",
            background: "#238636",
            color: "#fff",
            fontSize: 11,
            cursor: "pointer",
          }}
        >
          Approve
        </button>
        <button
          onClick={() => onDecide(request.id, "deny")}
          style={{
            padding: "4px 10px",
            borderRadius: 4,
            border: "1px solid #2a2a4a",
            background: "transparent",
            color: "#ef4444",
            fontSize: 11,
            cursor: "pointer",
          }}
        >
          Deny
        </button>
        {isMutating && (
          <button
            onClick={() => onDecide(request.id, "always_allow")}
            style={{
              padding: "4px 10px",
              borderRadius: 4,
              border: "1px solid #2a2a4a",
              background: "transparent",
              color: "#888",
              fontSize: 11,
              cursor: "pointer",
            }}
          >
            Always Allow
          </button>
        )}
      </div>
    </div>
  );
}
