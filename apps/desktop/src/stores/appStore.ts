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
  projects: [],
  activeProject: null,
  openFiles: [],
  activeFile: null,

  sessions: [],
  activeSession: null,
  isStreaming: false,
  chatMessages: [],
  agents: [],

  recentMemories: [],
  activeDecisionThreads: [],
  kgNodes: [],
  memoryHealth: "unknown",

  panelLayout: "default",
  theme: "dark",
  sidebarOpen: true,
  memoryPanelOpen: false,
  terminalOpen: true,

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
