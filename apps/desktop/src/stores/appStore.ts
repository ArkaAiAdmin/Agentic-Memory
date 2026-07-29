/**
 * Application Store (Zustand)
 *
 * Central state management for the IDE.
 * Covers workspace, agent sessions, memory, and UI state.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";
import type {
  Project,
  OpenFile,
  Session,
  DecisionThread,
  KGNode,
  PanelLayout,
  Theme,
  AgentInstance,
  SearchResult,
} from "@ami/shared";
import type { ProviderConfig } from "@ami/llm";

// ── Chat Message ──────────────────────────────────────────────────────────

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  toolCalls?: Array<{
    name: string;
    args: Record<string, unknown>;
    result?: string;
    status: "running" | "completed" | "error";
  }>;
  timestamp: number;
}

export interface ChatSession {
  id: string;
  title: string;
  agentId: string;
  agentName?: string;
  messages: ChatMessage[];
  createdAt: number;
  updatedAt: number;
}

// ── Right Panel Tabs ────────────────────────────────────────────────────────

export type RightPanelTab = "chat" | "memory" | "beliefs" | "skills" | "workers" | "tasks" | "composer" | "git" | "goals" | "worktrees";

const DEFAULT_PROVIDER_CONFIG: ProviderConfig = { type: "lmstudio", model: "local-model", baseUrl: "http://127.0.0.1:1234/v1" };

// ── App Store ─────────────────────────────────────────────────────────────

interface AppState {
  // ── Workspace ───────────────────────────────────────────────────────
  projects: Project[];
  activeProject: string | null;
  openFiles: OpenFile[];
  activeFile: string | null;

  // ── Agent ───────────────────────────────────────────────────────────
  sessions: Session[];
  activeSession: string | null;
  isStreaming: boolean;
  chatMessages: ChatMessage[];
  chatSessions: ChatSession[];
  activeChatSessionId: string | null;
  agents: AgentInstance[];

  // ── Memory ──────────────────────────────────────────────────────────
  recentMemories: SearchResult[];
  activeDecisionThreads: DecisionThread[];
  kgNodes: KGNode[];
  memoryHealth: "healthy" | "degraded" | "unhealthy" | "unknown";

  // ── UI ──────────────────────────────────────────────────────────────
  panelLayout: PanelLayout;
  theme: Theme;
  sidebarOpen: boolean;
  memoryPanelOpen: boolean;
  terminalOpen: boolean;
  sidebarWidth: number;
  rightPanelWidth: number;
  terminalHeight: number;
  rightPanelTab: RightPanelTab;
  hasCompletedOnboarding: boolean;

  // ── Settings ────────────────────────────────────────────────────────
  providerConfig: ProviderConfig;
  autocompleteEnabled: boolean;
  autocompleteModel: string;
  toolApprovalEnabled: boolean;
  mcpServers: Array<{ id: string; name: string; transport: "stdio" | "sse"; command?: string; args?: string[]; url?: string; enabled: boolean }>;

  // ── Actions ─────────────────────────────────────────────────────────
  // Workspace
  addProject: (project: Project) => void;
  setActiveProject: (root: string) => void;
  openFile: (file: OpenFile) => void;
  closeFile: (path: string) => void;
  setActiveFile: (path: string) => void;
  updateFileContent: (path: string, content: string) => void;

  // Agent
  addChatMessage: (message: ChatMessage) => void;
  updateChatMessage: (id: string, update: Partial<ChatMessage>) => void;
  clearChat: () => void;
  setStreaming: (streaming: boolean) => void;
  setAgents: (agents: AgentInstance[]) => void;
  createChatSession: (title?: string, agentId?: string, agentName?: string) => string;
  switchChatSession: (sessionId: string) => void;
  deleteChatSession: (sessionId: string) => void;
  addChatMessageToSession: (sessionId: string, message: ChatMessage) => void;
  updateChatSessionTitle: (sessionId: string, title: string) => void;
  setActiveChatSession: (sessionId: string | null) => void;

  // Memory
  setRecentMemories: (memories: SearchResult[]) => void;
  addRecentMemory: (memory: SearchResult) => void;
  setDecisionThreads: (threads: DecisionThread[]) => void;
  setKgNodes: (nodes: KGNode[]) => void;
  setMemoryHealth: (health: AppState["memoryHealth"]) => void;

  // UI
  setPanelLayout: (layout: PanelLayout) => void;
  setTheme: (theme: string) => void;
  setHasCompletedOnboarding: (completed: boolean) => void;
  toggleSidebar: () => void;
  toggleMemoryPanel: () => void;
  toggleTerminal: () => void;
  setSidebarWidth: (width: number) => void;
  setRightPanelWidth: (width: number) => void;
  setTerminalHeight: (height: number) => void;
  setRightPanelTab: (tab: RightPanelTab) => void;

  // Settings
  setProviderConfig: (config: ProviderConfig) => void;
  setAutocompleteEnabled: (enabled: boolean) => void;
  setAutocompleteModel: (model: string) => void;
  setToolApprovalEnabled: (enabled: boolean) => void;
  addMcpServer: (server: { id: string; name: string; transport: "stdio" | "sse"; command?: string; args?: string[]; url?: string; enabled: boolean }) => void;
  removeMcpServer: (id: string) => void;
  toggleMcpServer: (id: string) => void;
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
  // ── Initial State ───────────────────────────────────────────────────
  projects: [],
  activeProject: null,
  openFiles: [],
  activeFile: null,

  sessions: [],
  activeSession: null,
  isStreaming: false,
  chatMessages: [],
  chatSessions: [],
  activeChatSessionId: null,
  agents: [],

  recentMemories: [],
  activeDecisionThreads: [],
  kgNodes: [],
  memoryHealth: "unknown",

  panelLayout: "default",
  theme: "obsidian" as string,
  hasCompletedOnboarding: false,
  sidebarOpen: true,
  memoryPanelOpen: false,
  terminalOpen: false,
  sidebarWidth: 250,
  rightPanelWidth: 400,
  terminalHeight: 200,
  rightPanelTab: "chat",

  providerConfig: DEFAULT_PROVIDER_CONFIG,
  autocompleteEnabled: true,
  autocompleteModel: "gpt-4o-mini",
  toolApprovalEnabled: true,
  mcpServers: [],

  // ── Workspace Actions ───────────────────────────────────────────────
  addProject: (project) =>
    set((state) => ({
      projects: [...state.projects, project],
      activeProject: state.activeProject ?? project.root,
    })),

  setActiveProject: (root) => set({ activeProject: root }),

  openFile: (file) =>
    set((state) => {
      const exists = state.openFiles.find((f) => f.path === file.path);
      if (exists) {
        return { activeFile: file.path };
      }
      return {
        openFiles: [...state.openFiles, file],
        activeFile: file.path,
      };
    }),

  closeFile: (path) =>
    set((state) => {
      const idx = state.openFiles.findIndex((f) => f.path === path);
      const newFiles = state.openFiles.filter((f) => f.path !== path);
      return {
        openFiles: newFiles,
        activeFile:
          state.activeFile === path
            ? newFiles[Math.min(idx, newFiles.length - 1)]?.path ?? null
            : state.activeFile,
      };
    }),

  setActiveFile: (path) => set({ activeFile: path }),

  updateFileContent: (path, content) =>
    set((state) => ({
      openFiles: state.openFiles.map((f) =>
        f.path === path ? { ...f, content, isDirty: true } : f,
      ),
    })),

  // ── Agent Actions ───────────────────────────────────────────────────
  addChatMessage: (message) =>
    set((state) => {
      console.log("[store] addChatMessage:", message.role, message.content.slice(0, 50));
      return {
        chatMessages: [...state.chatMessages, message],
      };
    }),

  updateChatMessage: (id, update) =>
    set((state) => {
      console.log("[store] updateChatMessage:", id, update);
      return {
        chatMessages: state.chatMessages.map((m) =>
          m.id === id ? { ...m, ...update } : m,
        ),
        chatSessions: state.chatSessions.map((s) => ({
          ...s,
          messages: s.messages.map((m) => (m.id === id ? { ...m, ...update } : m)),
        })),
      };
    }),

  clearChat: () => set({ chatMessages: [] }),

  createChatSession: (title, agentId = "default", agentName) => {
    const id = `session-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const now = Date.now();
    set((state) => {
      const count = state.chatSessions.length;
      const session: ChatSession = {
        id,
        title: title || `Chat ${count + 1}`,
        agentId,
        agentName: agentName ?? (agentId === "default" ? "Primary Agent" : agentId),
        messages: [],
        createdAt: now,
        updatedAt: now,
      };
      const sessions = [...state.chatSessions, session];
      return { chatSessions: sessions.slice(-20), activeChatSessionId: id, chatMessages: [] };
    });
    return id;
  },

  switchChatSession: (sessionId) =>
    set((state) => {
      const session = state.chatSessions.find((s) => s.id === sessionId);
      return {
        activeChatSessionId: sessionId,
        chatMessages: session ? session.messages : [],
      };
    }),

  deleteChatSession: (sessionId) =>
    set((state) => {
      const sessions = state.chatSessions.filter((s) => s.id !== sessionId);
      let activeChatSessionId = state.activeChatSessionId;
      let chatMessages: ChatMessage[] = [];
      if (activeChatSessionId === sessionId) {
        activeChatSessionId = sessions[0]?.id ?? null;
        chatMessages = sessions[0]?.messages ?? [];
      }
      return { chatSessions: sessions, activeChatSessionId, chatMessages };
    }),

  addChatMessageToSession: (sessionId, message) =>
    set((state) => {
      const MAX_MESSAGES_PER_SESSION = 200;
      return {
        chatSessions: state.chatSessions.map((s) =>
          s.id === sessionId
            ? { ...s, messages: [...s.messages.slice(-(MAX_MESSAGES_PER_SESSION - 1)), message], updatedAt: Date.now() }
            : s,
        ),
        chatMessages:
          state.activeChatSessionId === sessionId
            ? [...state.chatMessages.slice(-(MAX_MESSAGES_PER_SESSION - 1)), message]
            : state.chatMessages,
      };
    }),

  updateChatSessionTitle: (sessionId, title) =>
    set((state) => ({
      chatSessions: state.chatSessions.map((s) =>
        s.id === sessionId ? { ...s, title, updatedAt: Date.now() } : s,
      ),
    })),

  setActiveChatSession: (sessionId) =>
    set((state) => {
      const session = state.chatSessions.find((s) => s.id === sessionId);
      return {
        activeChatSessionId: sessionId,
        chatMessages: session ? session.messages : [],
      };
    }),

  setStreaming: (streaming) => set({ isStreaming: streaming }),

  setAgents: (agents) => set({ agents }),

  // ── Memory Actions ──────────────────────────────────────────────────
  setRecentMemories: (memories) => set({ recentMemories: memories }),

  addRecentMemory: (memory) =>
    set((state) => ({
      recentMemories: [memory, ...state.recentMemories].slice(0, 50),
    })),

  setDecisionThreads: (threads) => set({ activeDecisionThreads: threads }),

  setKgNodes: (nodes) => set({ kgNodes: nodes }),

  setMemoryHealth: (health) => set({ memoryHealth: health }),

  // ── UI Actions ──────────────────────────────────────────────────────
  setPanelLayout: (layout) => set({ panelLayout: layout }),

  setTheme: (theme) => set({ theme }),

  setHasCompletedOnboarding: (completed) => set({ hasCompletedOnboarding: completed }),

  toggleSidebar: () =>
    set((state) => ({ sidebarOpen: !state.sidebarOpen })),

  toggleMemoryPanel: () =>
    set((state) => ({ memoryPanelOpen: !state.memoryPanelOpen })),

  toggleTerminal: () =>
    set((state) => ({ terminalOpen: !state.terminalOpen })),

  setSidebarWidth: (width) => set({ sidebarWidth: width }),

  setRightPanelWidth: (width) => set({ rightPanelWidth: width }),

  setTerminalHeight: (height) => set({ terminalHeight: height }),

  setRightPanelTab: (tab) => set({ rightPanelTab: tab }),

  // ── Settings Actions ────────────────────────────────────────────────
  setProviderConfig: (config) => set({ providerConfig: config }),

  setAutocompleteEnabled: (enabled) => set({ autocompleteEnabled: enabled }),

  setAutocompleteModel: (model) => set({ autocompleteModel: model }),

  setToolApprovalEnabled: (enabled) => set({ toolApprovalEnabled: enabled }),

  addMcpServer: (server) =>
    set((state) => ({
      mcpServers: [...state.mcpServers, server],
    })),

  removeMcpServer: (id) =>
    set((state) => ({
      mcpServers: state.mcpServers.filter((s) => s.id !== id),
    })),

  toggleMcpServer: (id) =>
    set((state) => ({
      mcpServers: state.mcpServers.map((s) =>
        s.id === id ? { ...s, enabled: !s.enabled } : s,
      ),
    })),
    }),
    {
      name: "ami-ide-store",
      version: 1,
      partialize: (state) => ({
        theme: state.theme,
        hasCompletedOnboarding: state.hasCompletedOnboarding,
        sidebarOpen: state.sidebarOpen,
        terminalOpen: state.terminalOpen,
        sidebarWidth: state.sidebarWidth,
        rightPanelWidth: state.rightPanelWidth,
        terminalHeight: state.terminalHeight,
        rightPanelTab: state.rightPanelTab,
        providerConfig: state.providerConfig,
        autocompleteEnabled: state.autocompleteEnabled,
        autocompleteModel: state.autocompleteModel,
        toolApprovalEnabled: state.toolApprovalEnabled,
        mcpServers: state.mcpServers,
        activeProject: state.activeProject,
        chatSessions: state.chatSessions,
        activeChatSessionId: state.activeChatSessionId,
      }),
    },
  ),
);
