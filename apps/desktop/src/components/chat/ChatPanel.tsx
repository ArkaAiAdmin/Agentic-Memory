import React, { useState, useRef, useEffect } from "react";
import { useAppStore, type ChatMessage } from "../../stores/appStore";
import { agentService } from "../../services/agentService";
import { nanoid } from "nanoid";

export function ChatPanel() {
  const { chatMessages, isStreaming, addChatMessage, updateChatMessage, setStreaming } =
    useAppStore();
  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chatMessages]);

  const handleSend = async () => {
    if (!input.trim() || isStreaming) return;

    const userMessage: ChatMessage = {
      id: nanoid(),
      role: "user",
      content: input.trim(),
      timestamp: Date.now(),
    };

    addChatMessage(userMessage);
    const prompt = input.trim();
    setInput("");
    setStreaming(true);

    // Create assistant message placeholder
    const assistantId = nanoid();
    addChatMessage({
      id: assistantId,
      role: "assistant",
      content: "",
      timestamp: Date.now(),
    });

    if (!agentService.isInitialized) {
      try {
        await agentService.initialize();
      } catch (err) {
        updateChatMessage(assistantId, {
          content: `Failed to initialize agent: ${err instanceof Error ? err.message : String(err)}`,
        });
        setStreaming(false);
        return;
      }
    }

    let assistantContent = "";
    let toolCalls: Array<{
      name: string;
      args: Record<string, unknown>;
      result?: string;
      status: "running" | "completed" | "error";
    }> = [];

    try {
      for await (const event of agentService.sendMessage(prompt)) {
        switch (event.type) {
          case "text":
            assistantContent += event.text;
            updateChatMessage(assistantId, { content: assistantContent });
            break;
          case "tool_call":
            toolCalls = [
              ...toolCalls,
              {
                name: event.toolName,
                args: event.args ?? {},
                status: "running",
              },
            ];
            updateChatMessage(assistantId, { toolCalls: [...toolCalls] });
            break;
          case "tool_result":
            toolCalls = toolCalls.map((tc) =>
              tc.name === event.toolName && tc.status === "running"
                ? {
                    ...tc,
                    result:
                      typeof event.result?.preview === "string"
                        ? event.result.preview
                        : JSON.stringify(event.result),
                    status: event.result?.success === false ? "error" : "completed",
                  }
                : tc,
            );
            updateChatMessage(assistantId, { toolCalls: [...toolCalls] });
            break;
          case "error":
            updateChatMessage(assistantId, {
              content:
                (assistantContent ? assistantContent + "\n\n" : "") +
                `Error: ${event.error}`,
            });
            setStreaming(false);
            return;
          case "done":
            setStreaming(false);
            return;
        }
      }
    } catch (err) {
      updateChatMessage(assistantId, {
        content:
          (assistantContent ? assistantContent + "\n\n" : "") +
          `Error: ${err instanceof Error ? err.message : "Unknown error"}`,
      });
    } finally {
      setStreaming(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Header */}
      <div
        style={{
          padding: "8px 12px",
          borderBottom: "1px solid #2a2a4a",
          fontSize: 12,
          fontWeight: 600,
          color: "#888",
        }}
      >
        Agent Chat
        {isStreaming && (
          <span style={{ color: "#4caf50", marginLeft: 8 }}>● streaming</span>
        )}
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflow: "auto", padding: 12 }}>
        {chatMessages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div
        style={{
          padding: 12,
          borderTop: "1px solid #2a2a4a",
          display: "flex",
          gap: 8,
        }}
      >
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Message the agent..."
          disabled={isStreaming}
          style={{
            flex: 1,
            background: "#16213e",
            border: "1px solid #2a2a4a",
            borderRadius: 6,
            padding: 8,
            color: "#e0e0e0",
            fontSize: 13,
            resize: "none",
            minHeight: 36,
            maxHeight: 120,
            fontFamily: "inherit",
          }}
        />
        <button
          onClick={handleSend}
          disabled={isStreaming || !input.trim()}
          style={{
            background: isStreaming ? "#333" : "#4a9eff",
            border: "none",
            borderRadius: 6,
            color: "#fff",
            padding: "8px 16px",
            cursor: isStreaming ? "not-allowed" : "pointer",
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          Send
        </button>
      </div>
    </div>
  );
}

// ── Message Bubble ────────────────────────────────────────────────────────

function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";

  return (
    <div
      style={{
        marginBottom: 12,
        display: "flex",
        flexDirection: "column",
        alignItems: isUser ? "flex-end" : "flex-start",
      }}
    >
      {/* Role label */}
      <span
        style={{
          fontSize: 10,
          color: "#666",
          marginBottom: 2,
          padding: "0 4px",
        }}
      >
        {isUser ? "You" : "Agent"}
      </span>

      {/* Message content */}
      <div
        style={{
          background: isUser ? "#4a9eff" : "#2a2a4a",
          color: isUser ? "#fff" : "#e0e0e0",
          padding: "8px 12px",
          borderRadius: 8,
          maxWidth: "85%",
          fontSize: 13,
          lineHeight: 1.5,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {message.content || (
          <span style={{ color: "#666" }}>...</span>
        )}
      </div>

      {/* Tool calls */}
      {message.toolCalls?.map((tc, i) => (
        <div
          key={i}
          style={{
            marginTop: 4,
            background: "#1a1a2e",
            border: "1px solid #2a2a4a",
            borderRadius: 6,
            padding: "6px 10px",
            fontSize: 11,
            maxWidth: "85%",
          }}
        >
          <div style={{ color: "#82aaff", fontWeight: 600 }}>
            🔧 {tc.name}
            <span
              style={{
                marginLeft: 8,
                color:
                  tc.status === "completed"
                    ? "#4caf50"
                    : tc.status === "error"
                      ? "#f44336"
                      : "#ff9800",
              }}
            >
              {tc.status}
            </span>
          </div>
          {tc.result && (
            <pre
              style={{
                marginTop: 4,
                color: "#888",
                fontSize: 10,
                overflow: "auto",
                maxHeight: 100,
              }}
            >
              {tc.result.slice(0, 500)}
            </pre>
          )}
        </div>
      ))}
    </div>
  );
}
