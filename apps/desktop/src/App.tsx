import React, { useState, useEffect } from "react";
import { useAppStore } from "./stores/appStore";
import { TitleBar } from "./components/TitleBar";
import { FileExplorer } from "./components/FileExplorer";
import { EditorPanel } from "./components/editor/EditorPanel";
import { ChatPanel } from "./components/chat/ChatPanel";
import { MemoryInspector } from "./components/memory/MemoryInspector";
import { BeliefReviewPanel } from "./components/memory/BeliefReviewPanel";
import { SkillBrowser } from "./components/memory/SkillBrowser";
import { WorkerStatusPanel } from "./components/memory/WorkerStatusPanel";
import { TerminalPanel } from "./components/terminal/TerminalPanel";
import { ProjectOpenDialog } from "./components/ProjectOpenDialog";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ResizablePane } from "./components/layout/ResizablePane";

type RightPanelTab = "chat" | "memory" | "beliefs" | "skills" | "workers";

export function App() {
  const {
    sidebarOpen,
    memoryPanelOpen,
    terminalOpen,
    theme,
    chatSessions,
    activeChatSessionId,
    createChatSession,
    switchChatSession,
    deleteChatSession,
  } = useAppStore();
  const [showOpenDialog, setShowOpenDialog] = useState(false);
  const [rightPanelTab, setRightPanelTab] = useState<RightPanelTab>("chat");

  useEffect(() => {
    if (chatSessions.length === 0 && activeChatSessionId === null) {
      createChatSession("Main Chat");
    }
  }, [chatSessions.length, activeChatSessionId, createChatSession]);

  const activeChatSession = chatSessions.find((s) => s.id === activeChatSessionId);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        background: theme === "dark" ? "#1a1a2e" : "#f5f5f5",
        color: theme === "dark" ? "#e0e0e0" : "#333",
      }}
    >
      {/* Title Bar */}
      <TitleBar onOpenProject={() => setShowOpenDialog(true)} />

      {/* Main Content */}
      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        {/* Left: File Explorer */}
        {sidebarOpen && (
          <ResizablePane
            direction="horizontal"
            defaultSize={250}
            minSize={150}
            maxSize={500}
            initialCollapsed={false}
            collapsedSize={0}
          >
            <div style={{ overflow: "auto", height: "100%" }}>
              <ErrorBoundary fallbackLabel="File Explorer">
                <FileExplorer />
              </ErrorBoundary>
            </div>
            <div />
          </ResizablePane>
        )}

        {/* Center: Editor + Terminal */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          <div style={{ flex: 1, overflow: "hidden" }}>
            <ErrorBoundary fallbackLabel="Editor">
              <EditorPanel />
            </ErrorBoundary>
          </div>
          {terminalOpen && (
            <ResizablePane
              direction="vertical"
              defaultSize={200}
              minSize={100}
              maxSize={600}
              initialCollapsed={false}
              collapsedSize={32}
            >
              <div style={{ overflow: "hidden" }}>
                <ErrorBoundary fallbackLabel="Terminal">
                  <TerminalPanel />
                </ErrorBoundary>
              </div>
              <div />
            </ResizablePane>
          )}
        </div>

        {/* Right: Tabbed panel (Chat / Memory / Beliefs / Skills / Workers) */}
        <ResizablePane
          direction="horizontal"
          defaultSize={400}
          minSize={300}
          maxSize={900}
          initialCollapsed={false}
          collapsedSize={0}
        >
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              height: "100%",
              borderLeft: "1px solid #2a2a4a",
            }}
          >
            {/* Tab bar */}
            <div
              style={{
                display: "flex",
                background: "#16213e",
                borderBottom: "1px solid #2a2a4a",
                overflowX: "auto",
                minHeight: 36,
              }}
            >
              {chatSessions.map((session) => (
                <div
                  key={session.id}
                  onClick={() => switchChatSession(session.id)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    padding: "6px 10px",
                    fontSize: 11,
                    cursor: "pointer",
                    color: activeChatSessionId === session.id ? "#fff" : "#888",
                    borderBottom:
                      activeChatSessionId === session.id
                        ? "2px solid #58a6ff"
                        : "2px solid transparent",
                    whiteSpace: "nowrap",
                    background: activeChatSessionId === session.id ? "#1e2d4d" : "transparent",
                    maxWidth: 160,
                  }}
                  title={session.title}
                >
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                    {session.title}
                  </span>
                  {chatSessions.length > 1 && (
                    <span
                      onClick={(e) => {
                        e.stopPropagation();
                        deleteChatSession(session.id);
                      }}
                      style={{
                        fontSize: 12,
                        lineHeight: 1,
                        opacity: 0.6,
                        padding: "0 2px",
                      }}
                    >
                      ×
                    </span>
                  )}
                </div>
              ))}
              <div
                onClick={() => createChatSession()}
                style={{
                  padding: "6px 10px",
                  fontSize: 14,
                  cursor: "pointer",
                  color: "#888",
                  lineHeight: 1,
                }}
                title="New chat"
              >
                +
              </div>
            </div>

            {/* Panel content */}
            <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
              {rightPanelTab === "chat" && activeChatSession && (
                <ChatPanel key={activeChatSession.id} sessionId={activeChatSession.id} />
              )}
              {rightPanelTab === "memory" && <MemoryInspector />}
              {rightPanelTab === "beliefs" && <BeliefReviewPanel />}
              {rightPanelTab === "skills" && <SkillBrowser />}
              {rightPanelTab === "workers" && <WorkerStatusPanel />}
            </div>
          </div>
          <div />
        </ResizablePane>
      </div>

      {/* Project Open Dialog */}
      {showOpenDialog && (
        <ProjectOpenDialog onClose={() => setShowOpenDialog(false)} />
      )}
    </div>
  );
}
