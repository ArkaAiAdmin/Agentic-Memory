import React, { useState } from "react";
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

type RightPanelTab = "chat" | "memory" | "beliefs" | "skills" | "workers";

export function App() {
  const { sidebarOpen, memoryPanelOpen, terminalOpen, theme } = useAppStore();
  const [showOpenDialog, setShowOpenDialog] = useState(false);
  const [rightPanelTab, setRightPanelTab] = useState<RightPanelTab>("chat");

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
          <div
            style={{
              width: 250,
              borderRight: "1px solid #2a2a4a",
              overflow: "auto",
            }}
          >
            <ErrorBoundary fallbackLabel="File Explorer">
              <FileExplorer />
            </ErrorBoundary>
          </div>
        )}

        {/* Center: Editor + Terminal */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
          <div style={{ flex: 1, overflow: "hidden" }}>
            <ErrorBoundary fallbackLabel="Editor">
              <EditorPanel />
            </ErrorBoundary>
          </div>
          {terminalOpen && (
            <div
              style={{
                height: 200,
                borderTop: "1px solid #2a2a4a",
              }}
            >
              <ErrorBoundary fallbackLabel="Terminal">
                <TerminalPanel />
              </ErrorBoundary>
            </div>
          )}
        </div>

        {/* Right: Tabbed panel (Chat / Memory / Beliefs / Skills / Workers) */}
        <div
          style={{
            width: 400,
            display: "flex",
            flexDirection: "column",
            borderLeft: "1px solid #2a2a4a",
          }}
        >
          {/* Tab bar */}
          <div
            style={{
              display: "flex",
              background: "#16213e",
              borderBottom: "1px solid #2a2a4a",
            }}
          >
            {(
              [
                { id: "chat", label: "Chat" },
                { id: "memory", label: "Memory" },
                { id: "beliefs", label: "Beliefs" },
                { id: "skills", label: "Skills" },
                { id: "workers", label: "Workers" },
              ] as const
            ).map((tab) => (
              <div
                key={tab.id}
                onClick={() => setRightPanelTab(tab.id)}
                style={{
                  padding: "6px 10px",
                  fontSize: 11,
                  cursor: "pointer",
                  color:
                    rightPanelTab === tab.id ? "#fff" : "#888",
                  borderBottom:
                    rightPanelTab === tab.id
                      ? "2px solid #58a6ff"
                      : "2px solid transparent",
                  whiteSpace: "nowrap",
                }}
              >
                {tab.label}
              </div>
            ))}
          </div>

          {/* Panel content */}
          <div style={{ flex: 1, overflow: "hidden" }}>
            <ErrorBoundary fallbackLabel="Chat">
              {rightPanelTab === "chat" && <ChatPanel />}
            </ErrorBoundary>
            <ErrorBoundary fallbackLabel="Memory">
              {rightPanelTab === "memory" && <MemoryInspector />}
            </ErrorBoundary>
            <ErrorBoundary fallbackLabel="Beliefs">
              {rightPanelTab === "beliefs" && <BeliefReviewPanel />}
            </ErrorBoundary>
            <ErrorBoundary fallbackLabel="Skills">
              {rightPanelTab === "skills" && <SkillBrowser />}
            </ErrorBoundary>
            <ErrorBoundary fallbackLabel="Workers">
              {rightPanelTab === "workers" && <WorkerStatusPanel />}
            </ErrorBoundary>
          </div>
        </div>
      </div>

      {/* Project Open Dialog */}
      {showOpenDialog && (
        <ProjectOpenDialog onClose={() => setShowOpenDialog(false)} />
      )}
    </div>
  );
}
