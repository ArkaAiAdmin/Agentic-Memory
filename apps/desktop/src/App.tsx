import React, { useState, useEffect } from "react";
import { useAppStore } from "./stores/appStore";
import { ActivityBar } from "./components/ActivityBar";
import { TopBar } from "./components/TopBar";
import { StatusBar } from "./components/StatusBar";
import { PanelBarDrag } from "./components/PanelBarDrag";
import { FileExplorer } from "./components/FileExplorer";
import { EditorPanel } from "./components/editor/EditorPanel";
import { ChatPanel } from "./components/chat/ChatPanel";
import { MemoryInspector } from "./components/memory/MemoryInspector";
import { BeliefReviewPanel } from "./components/memory/BeliefReviewPanel";
import { SkillBrowser } from "./components/memory/SkillBrowser";
import { WorkerStatusPanel } from "./components/memory/WorkerStatusPanel";
import { TaskQueueView } from "./components/worker/TaskQueueView";
import { TerminalPanel } from "./components/terminal/TerminalPanel";
import { ProjectOpenDialog } from "./components/ProjectOpenDialog";
import { SettingsPanel } from "./components/settings/SettingsPanel";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ResizablePane } from "./components/layout/ResizablePane";
import { CommandPalette } from "./components/command/CommandPalette";
import { ShortcutReference } from "./components/command/ShortcutReference";
import { InlineEdit } from "./components/editor/InlineEdit";
import { ComposerPanel } from "./components/composer/ComposerPanel";
import { GitPanel } from "./components/git/GitPanel";
import { WorktreePanel } from "./components/git/WorktreePanel";
import { GoalsPanel } from "./components/goals/GoalsPanel";
import { OnboardingWizard } from "./components/onboarding/OnboardingWizard";
import { useHotkeys } from "./hooks/useHotkeys";
import { useSession } from "./hooks/useSession";
import { useOnlineStatus } from "./hooks/useOnlineStatus";
import { commandRegistry } from "./services/commands";
import { agentService } from "./services/agentService";
import { agentRegistry } from "./services/agentRegistry";
import { getThemeById, applyTheme } from "./services/themes";
import { checkForUpdates, onUpdateAvailable, type UpdateInfo } from "./services/updater";
import "./theme.css";

export function App() {
  const {
    sidebarOpen,
    terminalOpen,
    theme,
    chatSessions,
    activeChatSessionId,
    createChatSession,
    switchChatSession,
    deleteChatSession,
    sidebarWidth,
    rightPanelWidth,
    terminalHeight,
    rightPanelTab,
    setSidebarWidth,
    setRightPanelWidth,
    setTerminalHeight,
    setRightPanelTab: setTab,
    toggleSidebar,
    toggleTerminal,
  } = useAppStore();
  const [showOpenDialog, setShowOpenDialog] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showPalette, setShowPalette] = useState(false);
  const [showShortcuts, setShowShortcuts] = useState(false);
  const [showInlineEdit, setShowInlineEdit] = useState(false);
  const [pendingUpdate, setPendingUpdate] = useState<UpdateInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const isOnline = useOnlineStatus();

  const { hasCompletedOnboarding } = useAppStore();

  // Apply theme on mount and when it changes
  useEffect(() => {
    const themeId = useAppStore.getState().theme;
    const themePalette = getThemeById(themeId);
    applyTheme(themePalette);
  }, [theme]);

  // Check for updates on mount
  useEffect(() => {
    try {
      checkForUpdates();
    } catch (err) {
      console.warn("[App] Update check failed:", err);
    }
    return onUpdateAvailable((info) => setPendingUpdate(info));
  }, []);

  // Auto-initialize agent service (starts memory bridge) on mount
  useEffect(() => {
    agentService.initialize().catch((err) =>
      console.warn("[App] Agent service init failed (non-fatal):", err)
    );
  }, []);

  // Ensure a default chat session exists so the ChatPanel renders
  useEffect(() => {
    const state = useAppStore.getState();
    if (state.chatSessions.length === 0) {
      state.createChatSession("Chat");
    }
  }, []);

  // Register commands
  useEffect(() => {
    commandRegistry.registerAll([
      { id: "file.open", title: "Open File", category: "File", keybinding: "Cmd+O", icon: "📂", run: () => setShowOpenDialog(true) },
      { id: "file.settings", title: "Open Settings", category: "File", icon: "⚙", run: () => setShowSettings(true) },
      { id: "view.toggleSidebar", title: "Toggle Sidebar", category: "View", keybinding: "Cmd+B", icon: "📁", run: toggleSidebar },
      { id: "view.toggleTerminal", title: "Toggle Terminal", category: "View", keybinding: "Cmd+`", icon: "💻", run: toggleTerminal },
      { id: "view.chat", title: "Focus Chat", category: "View", keybinding: "Cmd+L", icon: "💬", run: () => setTab("chat") },
      { id: "view.composer", title: "Open Composer", category: "View", icon: "📝", run: () => setTab("composer") },
      { id: "view.git", title: "Open Git Panel", category: "View", icon: "⑂", run: () => setTab("git") },
      { id: "view.tasks", title: "Open Task Queue", category: "View", icon: "⚡", run: () => setTab("tasks") },
      { id: "view.memory", title: "Open Memory Panel", category: "View", icon: "🧠", run: () => setTab("memory") },
      { id: "view.beliefs", title: "Open Belief Review", category: "View", icon: "◇", run: () => setTab("beliefs") },
      { id: "view.skills", title: "Open Skill Browser", category: "View", icon: "★", run: () => setTab("skills") },
      { id: "view.workers", title: "Open Worker Status", category: "View", icon: "⚙", run: () => setTab("workers") },
      { id: "agent.newChat", title: "New Chat Session", category: "Agent", icon: "💬", run: () => createChatSession() },
      { id: "editor.inlineEdit", title: "Inline Edit (Cmd+I)", category: "Editor", keybinding: "Cmd+I", icon: "✏", run: () => setShowInlineEdit(true) },
      { id: "command.palette", title: "Command Palette", category: "System", keybinding: "Cmd+Shift+P", icon: "⌘", run: () => setShowPalette(true) },
      { id: "help.shortcuts", title: "Keyboard Shortcuts", category: "Help", keybinding: "Cmd+Shift+/", icon: "?", run: () => setShowShortcuts(true) },
    ]);
  }, [toggleSidebar, toggleTerminal, setTab, createChatSession]);

  useHotkeys();

  // Auto-start memory session and health monitoring
  useSession();

  useEffect(() => {
    function handleQuickOpen(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        const tag = (e.target as HTMLElement)?.tagName;
        if (tag !== "INPUT" && tag !== "TEXTAREA") {
          e.preventDefault();
          setShowPalette(true);
        }
      }
    }
    window.addEventListener("keydown", handleQuickOpen);
    return () => window.removeEventListener("keydown", handleQuickOpen);
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => setLoading(false), 600);
    return () => clearTimeout(timer);
  }, []);

  if (loading) {
    return (
      <div style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        height: "100vh",
        background: "var(--bg-primary, #0a0b10)",
      }}>
        {/* Logo mark — matches OnboardingWizard diamond */}
        <div
          style={{
            width: 72,
            height: 72,
            borderRadius: 20,
            background: "var(--accent-gradient, linear-gradient(135deg, #8b5cf6, #6366f1))",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            marginBottom: 32,
            boxShadow: "var(--accent-glow, 0 0 24px rgba(139, 92, 246, 0.25)), 0 8px 32px rgba(139, 92, 246, 0.3)",
          }}
        >
          <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
            <path d="M18 4L4 12v12l14 8 14-8V12L18 4z" stroke="white" strokeWidth="2" fill="none"/>
            <path d="M4 12l14 8m0 0l14-8m-14 8v12" stroke="white" strokeWidth="2" opacity="0.5"/>
            <circle cx="18" cy="18" r="4" fill="white"/>
          </svg>
        </div>

        <h1
          style={{
            fontSize: 28,
            fontWeight: 700,
            color: "var(--text-primary, #f1f3f8)",
            margin: "0 0 8px",
            letterSpacing: -0.5,
          }}
        >
          Agentic Memory IDE
        </h1>

        <div style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          color: "var(--text-secondary, #8b93a8)",
          fontSize: 13,
        }}>
          <div style={{
            width: 12,
            height: 12,
            borderRadius: "50%",
            border: "2px solid var(--text-secondary, #8b93a8)",
            borderTopColor: "transparent",
            animation: "spin 0.8s linear infinite",
          }} />
          Initializing...
        </div>
      </div>
    );
  }

  const activeChatSession = chatSessions.find((s) => s.id === activeChatSessionId);

  if (!hasCompletedOnboarding) {
    return (
      <OnboardingWizard onComplete={() => useAppStore.getState().setHasCompletedOnboarding(true)} />
    );
  }

  return (
    <div style={{
      display: "flex",
      flexDirection: "column",
      height: "100vh",
      background: "var(--bg-primary)",
      color: "var(--text-primary)",
      fontFamily: "var(--font-sans)",
    }}>
      {!isOnline && (
        <div className="memory-banner">
          Offline — LLM and memory features unavailable until reconnected
        </div>
      )}
      <TopBar />
      <div style={{ display: "flex", flex: 1, overflow: "visible" }}>
        {/* Activity Bar — vertical icon strip */}
        <ActivityBar onOpenProject={() => setShowOpenDialog(true)} onOpenSettings={() => setShowSettings(true)} onOpenPalette={() => setShowPalette(true)} />

        {/* Sidebar */}
        {sidebarOpen && (
          <ResizablePane side="right" size={sidebarWidth} onResize={setSidebarWidth} defaultSize={250} minSize={150} maxSize={500}>
            <div style={{
              overflow: "auto",
              height: "100%",
              background: "var(--bg-secondary)",
              borderRight: "1px solid var(--border-default)",
            }}>
              <ErrorBoundary fallbackLabel="File Explorer">
                <FileExplorer />
              </ErrorBoundary>
            </div>
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
            <ResizablePane side="top" size={terminalHeight} onResize={setTerminalHeight} defaultSize={200} minSize={100} maxSize={600}>
              <div style={{
                height: "100%",
                overflow: "hidden",
                borderTop: "2px solid var(--border-default)",
              }}>
                <ErrorBoundary fallbackLabel="Terminal">
                  <TerminalPanel />
                </ErrorBoundary>
              </div>
            </ResizablePane>
          )}
        </div>

        {/* Panel Bar + Right panel */}
        <PanelBarDrag onResize={setRightPanelWidth} currentSize={rightPanelWidth} />
        <ResizablePane side="left" size={rightPanelWidth} onResize={setRightPanelWidth} defaultSize={400} minSize={300} maxSize={900}>
          <div style={{
            display: "flex",
            flexDirection: "column",
            height: "100%",
            borderLeft: "1px solid var(--border-default)",
            background: "var(--bg-secondary)",
          }}>
            {/* Chat session sub-tabs */}
            {rightPanelTab === "chat" && (
              <div style={{
                display: "flex",
                overflowX: "auto",
                background: "var(--bg-primary)",
                borderBottom: "1px solid var(--border-subtle)",
                minHeight: 32,
                flexShrink: 0,
              }}>
                {chatSessions.map((session) => {
                  const agent = agentRegistry.get(session.agentId);
                  const agentColor = agent?.color || "var(--accent)";
                  const agentName = agent?.name || session.agentName || session.agentId;

                  return (
                    <div
                      key={session.id}
                      onClick={() => switchChatSession(session.id)}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                        padding: "5px 12px",
                        fontSize: 11,
                        cursor: "pointer",
                        color: activeChatSessionId === session.id ? "var(--text-primary)" : "var(--text-tertiary)",
                        borderBottom: activeChatSessionId === session.id ? `2px solid ${agentColor}` : "2px solid transparent",
                        whiteSpace: "nowrap",
                        background: activeChatSessionId === session.id ? "var(--bg-elevated)" : "transparent",
                        maxWidth: 170,
                        flexShrink: 0,
                      }}
                      title={`${session.title} (${agentName})`}
                    >
                      <span
                        style={{
                          width: 7,
                          height: 7,
                          borderRadius: "50%",
                          background: agentColor,
                          display: "inline-block",
                          flexShrink: 0,
                          opacity: activeChatSessionId === session.id ? 1 : 0.65,
                          boxShadow: activeChatSessionId === session.id ? `0 0 6px ${agentColor === "var(--accent)" ? "var(--accent-glow, transparent)" : agentColor + "40"}` : "none",
                        }}
                      />
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{session.title}</span>
                      {chatSessions.length > 1 && (
                        <span
                          onClick={(e) => { e.stopPropagation(); deleteChatSession(session.id); }}
                          style={{ fontSize: 12, lineHeight: 1, opacity: 0.5, padding: "0 2px" }}
                        >×</span>
                      )}
                    </div>
                  );
                })}
                <div
                  onClick={() => createChatSession(undefined, agentRegistry.getActiveAgentId())}
                  style={{ padding: "5px 12px", fontSize: 14, cursor: "pointer", color: "var(--text-tertiary)", lineHeight: 1, flexShrink: 0 }}
                  title="New chat with active agent"
                >+</div>
              </div>
            )}

            {/* Panel content */}
            <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", minHeight: 0 }}>
              {rightPanelTab === "chat" && activeChatSession && <ErrorBoundary fallbackLabel="Chat Panel"><ChatPanel key={activeChatSession.id} sessionId={activeChatSession.id} /></ErrorBoundary>}
              {rightPanelTab === "composer" && <ErrorBoundary fallbackLabel="Composer"><ComposerPanel onOpenFile={() => setTab("chat")} /></ErrorBoundary>}
              {rightPanelTab === "git" && <ErrorBoundary fallbackLabel="Git"><GitPanel /></ErrorBoundary>}
              {rightPanelTab === "tasks" && <ErrorBoundary fallbackLabel="Tasks"><TaskQueueView onOpenComposer={() => setTab("composer")} /></ErrorBoundary>}
              {rightPanelTab === "goals" && <ErrorBoundary fallbackLabel="Goals"><GoalsPanel /></ErrorBoundary>}
              {rightPanelTab === "worktrees" && <ErrorBoundary fallbackLabel="Worktrees"><WorktreePanel /></ErrorBoundary>}
              {rightPanelTab === "memory" && <ErrorBoundary fallbackLabel="Memory"><MemoryInspector /></ErrorBoundary>}
              {rightPanelTab === "beliefs" && <ErrorBoundary fallbackLabel="Beliefs"><BeliefReviewPanel /></ErrorBoundary>}
              {rightPanelTab === "skills" && <ErrorBoundary fallbackLabel="Skills"><SkillBrowser /></ErrorBoundary>}
              {rightPanelTab === "workers" && <ErrorBoundary fallbackLabel="Workers"><WorkerStatusPanel /></ErrorBoundary>}
            </div>
          </div>
        </ResizablePane>
      </div>

      {showOpenDialog && <ProjectOpenDialog onClose={() => setShowOpenDialog(false)} />}

      {/* Status Bar */}
      <StatusBar />

      {showSettings && <SettingsPanel onClose={() => setShowSettings(false)} />}
      <CommandPalette isOpen={showPalette} onClose={() => setShowPalette(false)} />
      {showShortcuts && <ShortcutReference onClose={() => setShowShortcuts(false)} />}
      <InlineEdit
        isOpen={showInlineEdit}
        onClose={() => setShowInlineEdit(false)}
        onApply={(newText) => {
          const { activeFile } = useAppStore.getState();
          if (activeFile) useAppStore.getState().updateFileContent(activeFile, newText);
        }}
      />

      {/* Update notification */}
      {pendingUpdate && (
        <div style={{
          position: "fixed", bottom: 16, right: 16, zIndex: 9999,
          padding: "12px 16px", borderRadius: "var(--radius-lg)",
          background: "var(--bg-elevated)", border: "1px solid var(--accent)",
          boxShadow: "var(--shadow-lg)", display: "flex", alignItems: "center", gap: 12, maxWidth: 360,
        }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>Update: v{pendingUpdate.version}</div>
            <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 2 }}>{pendingUpdate.releaseNotes?.slice(0, 80) ?? "Bug fixes"}</div>
          </div>
          <button onClick={async () => { const { installUpdate } = await import("./services/updater"); installUpdate(); }}
            style={{ padding: "6px 14px", borderRadius: "var(--radius-sm)", border: "none", background: "var(--accent)", color: "var(--accent-text)", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>Install</button>
          <button onClick={() => setPendingUpdate(null)}
            style={{ padding: "6px 8px", borderRadius: "var(--radius-sm)", border: "none", background: "transparent", color: "var(--text-tertiary)", fontSize: 12, cursor: "pointer" }}>×</button>
        </div>
      )}
    </div>
  );
}
