import React, { useState, useRef, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { useAppStore, type ChatMessage } from "../../stores/appStore";
import { Avatar } from "../ui";
import { CodeBlock } from "./CodeBlock";
import { ToolCallInline } from "./ToolCallInline";
import chatStyles from "../../styles/chat.module.css";

const MessageBubble = React.memo(function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const [copied, setCopied] = useState(false);

  const [displayContent, setDisplayContent] = useState(message.content);
  const contentRef = useRef(message.content);
  const rafRef = useRef<number | null>(null);
  const isStreaming = useAppStore((s) => s.isStreaming);

  useEffect(() => {
    contentRef.current = message.content;
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      setDisplayContent(contentRef.current);
      rafRef.current = null;
    });
    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [message.content, isStreaming]);

  const handleCopy = () => {
    navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const timeStr = new Date(message.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  if (isUser) {
    return (
      <div className={chatStyles.messageUser}>
        <div className={chatStyles.messageHeader}>
          <Avatar type="user" size="sm" name="U" />
          <span className={chatStyles.messageRole}>You</span>
          <span className={chatStyles.messageTime}>{timeStr}</span>
          <div className={chatStyles.messageActions}>
            <button className={chatStyles.codeBlockCopy} onClick={handleCopy}>
              {copied ? "✓" : "Copy"}
            </button>
          </div>
        </div>
        <div className={chatStyles.userBubble}>{message.content}</div>
      </div>
    );
  }

  return (
    <div className={chatStyles.messageAssistant}>
      <div className={chatStyles.messageHeader}>
        <Avatar type="agent" size="sm" />
        <span className={chatStyles.messageRole}>Agent</span>
        <span className={chatStyles.messageTime}>{timeStr}</span>
        <div className={chatStyles.messageActions}>
          <button className={chatStyles.codeBlockCopy} onClick={handleCopy}>
            {copied ? "✓" : "Copy"}
          </button>
        </div>
      </div>
      <div className={chatStyles.markdown}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[rehypeHighlight]}
          components={{
            code({ className, children, ...props }) {
              const isInline = !className && typeof children === "string" && !children.includes("\n");
              if (isInline) {
                return <code className={className} {...props}>{children}</code>;
              }
              return <CodeBlock className={className} {...props}>{children}</CodeBlock>;
            },
            pre({ children }) {
              return <>{children}</>;
            },
          }}
        >
          {displayContent || "..."}
        </ReactMarkdown>
      </div>
      {message.toolCalls?.map((tc, i) => (
        <ToolCallInline key={tc.name + "-" + i} toolCall={tc} />
      ))}
    </div>
  );
});

export { MessageBubble };
