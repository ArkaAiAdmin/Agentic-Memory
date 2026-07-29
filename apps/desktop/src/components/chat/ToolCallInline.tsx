import React, { useState } from "react";
import type { ChatMessage } from "../../stores/appStore";
import chatStyles from "../../styles/chat.module.css";

const ToolCallInline = React.memo(function ToolCallInline({ toolCall }: { toolCall: NonNullable<ChatMessage["toolCalls"]>[number] }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className={chatStyles.toolCall}>
      <div className={chatStyles.toolCallHeader} onClick={() => setExpanded(!expanded)}>
        <span style={{ fontSize: 12 }}>🔧</span>
        <span className={chatStyles.toolCallName}>{toolCall.name}</span>
        <span className={chatStyles.toolCallStatus} style={{
          color: toolCall.status === "completed" ? "var(--success)" : toolCall.status === "error" ? "var(--error)" : "var(--warning)",
        }}>
          {toolCall.status === "running" ? "⟳ running" : toolCall.status === "completed" ? "✓ done" : "✗ error"}
        </span>
        <span style={{ fontSize: 10, color: "var(--text-tertiary)" }}>{expanded ? "▼" : "▶"}</span>
      </div>
      {expanded && toolCall.result && (
        <div className={chatStyles.toolCallBody}>
          <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
            {toolCall.result.slice(0, 1000)}
          </pre>
        </div>
      )}
    </div>
  );
});

export { ToolCallInline };
