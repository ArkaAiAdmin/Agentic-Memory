/**
 * Application Store (Zustand)
 *
 * Central state management for the IDE.
 * Covers workspace, agent sessions, memory, and UI state.
 */

import { create } from "zustand";
import type {
  Project,
  OpenFile,
  Session,
  MemoryNote,
  DecisionThread,
  KGNode,
  PanelLayout,
  Theme,
  Message,
  TurnEvent,
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
  messages: ChatMessage[];
  createdAt: number;
  updatedAt: number;
}

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
  createChatSession: (title?: string) => string;
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
  setTheme: (theme: Theme) => void;
  toggleSidebar: () => void;
  toggleMemoryPanel: () => void;
  toggleTerminal: () => void;
}

export const useAppStore = create<AppState>((set) => ({
  // ── Initial State ───────────────────────────────────────────────────
  projects: [
    {
      root: "/Users/arka/.config/agentic-memory",
      name: "agentic-memory",
      files: [],
    },
  ],
  activeProject: "/Users/arka/.config/agentic-memory",
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
  theme: "dark",
  sidebarOpen: true,
  memoryPanelOpen: false,
  terminalOpen: false,

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
    set((state) => ({
      chatMessages: [...state.chatMessages, message],
    })),

  updateChatMessage: (id, update) =>
    set((state) => ({
      chatMessages: state.chatMessages.map((m) =>
        m.id === id ? { ...m, ...update } : m,
      ),
    })),

  clearChat: () => set({ chatMessages: [] }),

  createChatSession: (title) => {
    const id = `session-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const now = Date.now();
    const count = useAppStore.getState().chatSessions.length;
    const session: ChatSession = {
      id,
      title: title || `Chat ${count + 1}`,
      messages: [],
      createdAt: now,
      updatedAt: now,
    };
    set((state) => {
      const sessions = [...state.chatSessions, session];
      return { chatSessions: sessions, activeChatSessionId: id, chatMessages: session.messages };
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
    set((state) => ({
      chatSessions: state.chatSessions.map((s) =>
        s.id === sessionId
          ? { ...s, messages: [...s.messages, message], updatedAt: Date.now() }
          : s,
      ),
      chatMessages:
        state.activeChatSessionId === sessionId
          ? [...state.chatMessages, message]
          : state.chatMessages,
    })),

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

  toggleSidebar: () =>
    set((state) => ({ sidebarOpen: !state.sidebarOpen })),

  toggleMemoryPanel: () =>
    set((state) => ({ memoryPanelOpen: !state.memoryPanelOpen })),

  toggleTerminal: () =>
    set((state) => ({ terminalOpen: !state.terminalOpen })),
}));
