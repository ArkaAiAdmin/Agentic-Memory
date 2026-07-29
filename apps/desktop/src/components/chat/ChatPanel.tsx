import React, { useState, useRef, useEffect, useCallback } from "react";
import { useAgent } from "../../hooks/useAgent";
import { useAppStore } from "../../stores/appStore";
import {
  setApprovalCallback,
  allowAlways,
  type ToolApprovalRequest,
  type ToolApprovalDecision,
} from "../../services/toolApproval";
import { ToolCallCard } from "./ToolCallCard";
import { ModeSelector } from "./ModeSelector";
import { MentionAutocomplete, useMention } from "./MentionAutocomplete";
import { MessageBubble } from "./MessageBubble";
import { TypingIndicator, Button } from "../ui";
import chatStyles from "../../styles/chat.module.css";
import { exportChatAsMarkdown, downloadBlob } from "../../services/exportData";
import "highlight.js/styles/github-dark.css";

interface ChatPanelProps {
  sessionId?: string;
}

export function ChatPanel({ sessionId = "default" }: ChatPanelProps) {
  const {
    messages: chatMessages,
    sendMessage,
    isStreaming,
    isInitialized,
    abort,
  } = useAgent(sessionId);
  const [input, setInput] = useState("");
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const [pendingApprovals, setPendingApprovals] = useState<
    Map<string, { request: ToolApprovalRequest; resolve: (d: ToolApprovalDecision) => void }>
  >(new Map());

  const mention = useMention(input, (item) => {
    const lastAtIndex = input.lastIndexOf("@");
    const before = input.slice(0, lastAtIndex);
    const after = input.slice(lastAtIndex + 1 + mention.query.length);
    setInput(`${before}@${item.value} ${after}`);
  });

  useEffect(() => {
    setApprovalCallback(async (request) => {
      return new Promise<ToolApprovalDecision>((resolve) => {
        setPendingApprovals((prev) => {
          const next = new Map(prev);
          next.set(request.id, { request, resolve });
          return next;
        });
      });
    });
    return () => setApprovalCallback(null);
  }, []);

  const handleApproval = useCallback((requestId: string, decision: ToolApprovalDecision) => {
    setPendingApprovals((prev) => {
      const entry = prev.get(requestId);
      if (entry) {
        if (decision === "always_allow") allowAlways(entry.request.toolName);
        entry.resolve(decision);
        const next = new Map(prev);
        next.delete(requestId);
        return next;
      }
      return prev;
    });
  }, []);

  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Handle scroll position for "scroll to bottom" button
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
    setShowScrollBtn(!isNearBottom && chatMessages.length > 0);
  }, [chatMessages.length]);

  // Auto-scroll when new messages come in
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 200;
    if (isNearBottom) {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    }
  }, [chatMessages, pendingApprovals]);

  const scrollToBottom = useCallback(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, []);

  const handleSend = async () => {
    if (!input.trim() || isStreaming) return;
    const prompt = input.trim();
    setInput("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    await sendMessage(prompt);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
  };

  // Auto-grow textarea
  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  };

  const providerConfig = useAppStore((s) => s.providerConfig);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
      {/* Header */}
      <div className={chatStyles.chatInputFooter} style={{
        padding: "10px 16px", borderBottom: "1px solid var(--border-default)",
        justifyContent: "space-between", margin: 0,
      }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", display: "flex", alignItems: "center", gap: 8 }}>
          Chat
          {isStreaming && (
            <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--success)", animation: "pulse 2s ease-in-out infinite" }} />
              <span style={{ color: "var(--success)", fontSize: 10 }}>streaming</span>
            </span>
          )}
          {!isInitialized && !isStreaming && (
            <span style={{ color: "var(--warning)", fontSize: 10 }}>initializing...</span>
          )}
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          {chatMessages.length > 0 && (
            <button
              onClick={() => {
                const md = exportChatAsMarkdown(chatMessages);
                downloadBlob(md, `chat-${new Date().toISOString().slice(0, 10)}.md`, "text/markdown");
              }}
              style={{
                padding: "2px 8px", fontSize: 10, borderRadius: "var(--radius-sm)",
                border: "1px solid var(--border-default)", background: "transparent",
                color: "var(--text-tertiary)", cursor: "pointer",
              }}
              title="Export as Markdown"
            >
              Export
            </button>
          )}
          {isStreaming && (
            <Button variant="ghost" size="sm" onClick={abort}>
              Stop
            </Button>
          )}
        </div>
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflow: "hidden", position: "relative" }}>
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          role="log"
          aria-live="polite"
          style={{ position: "absolute", inset: 0, overflowY: "auto", overflowX: "hidden" }}
        >
          {chatMessages.length === 0 && (
            <div style={{ textAlign: "center", padding: "60px 20px", color: "var(--text-tertiary)" }}>
              <div style={{ fontSize: 28, marginBottom: 8 }}>💬</div>
              <div style={{ fontSize: 14, color: "var(--text-secondary)", fontWeight: 500 }}>Start a conversation</div>
              <div style={{ fontSize: 12, marginTop: 6, maxWidth: 280, margin: "6px auto 0" }}>
                The agent uses memory for context-aware responses. Type a message or use @ to mention files.
              </div>
            </div>
          )}
          {chatMessages.map((msg) => (
            <MessageBubble key={msg.id} message={msg} />
          ))}
          {isStreaming && chatMessages[chatMessages.length - 1]?.role === "assistant" && !chatMessages[chatMessages.length - 1]?.content && (
            <div className={chatStyles.thinking}>
              <TypingIndicator />
            </div>
          )}
          {Array.from(pendingApprovals.entries()).map(([id, { request }]) => (
            <div key={id} style={{ padding: "8px 18px" }}>
              <ToolCallCard request={request} onDecide={handleApproval} />
            </div>
          ))}
        </div>

        {/* Scroll to bottom */}
        {showScrollBtn && (
          <button className={chatStyles.scrollToBottom} onClick={scrollToBottom}>
            ↓ New messages
          </button>
        )}
      </div>

      {/* Input area */}
      <div className={chatStyles.chatInput}>
        <MentionAutocomplete isOpen={mention.isOpen} query={mention.query} onSelect={mention.onSelect} onClose={mention.onClose} />

        <div className={chatStyles.chatInputBox}>
          <div style={{ marginBottom: 6 }}>
            <ModeSelector />
          </div>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            placeholder="Message the agent... (@ for mentions, Shift+Enter for newline)"
            disabled={isStreaming}
            aria-label="Chat message input"
            className={chatStyles.chatTextarea}
            rows={1}
          />
          <div className={chatStyles.chatInputFooter}>
            <div className={chatStyles.chatModelPill}>
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: isInitialized ? "var(--success)" : "var(--warning)" }} />
              {providerConfig.model || providerConfig.type}
            </div>
            <Button
              variant="primary"
              size="sm"
              onClick={handleSend}
              disabled={isStreaming || !input.trim()}
              aria-label="Send message"
            >
              Send
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

