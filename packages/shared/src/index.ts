// ── Memory Types ──────────────────────────────────────────────────────────

export type NoteId = string;
export type SessionId = string;
export type AgentId = string;

export interface MemoryNote {
  id: NoteId;
  content: string;
  category: NoteCategory;
  tags: string[];
  metadata: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  access_count: number;
  last_accessed: number;
  importance: number;
}

export type NoteCategory =
  | "lessons"
  | "decisions"
  | "projects"
  | "preferences"
  | "sessions"
  | "auto_save"
  | "journal"
  | "belief"
  | "skill"
  | "pinned";

// ── Search Types ──────────────────────────────────────────────────────────

export type SearchMode =
  | "hybrid"
  | "semantic"
  | "fts"
  | "facts"
  | "graph";

export interface SearchQuery {
  query: string;
  mode?: SearchMode;
  limit?: number;
  category?: NoteCategory;
  tags?: string[];
  min_importance?: number;
  session_id?: string;
  project?: string;
}

export interface SearchResult {
  note_id: NoteId;
  content: string;
  category: NoteCategory;
  tags: string[];
  score: number;
  source: string; // which search phase found it
  metadata: Record<string, unknown>;
  created_at: number;
}

// ── Save Types ────────────────────────────────────────────────────────────

export interface SavePayload {
  content: string;
  category: NoteCategory;
  tags?: string[];
  metadata?: Record<string, unknown>;
  importance?: number;
  project?: string;
}

// ── Session Types ─────────────────────────────────────────────────────────

export interface Session {
  id: SessionId;
  project_root: string;
  started_at: number;
  ended_at?: number;
  summary?: string;
  status: "active" | "compacted" | "ended";
  decision_threads: DecisionThread[];
  context_window?: ContextWindowState;
}

export interface SessionBriefing {
  session_id: SessionId;
  context: string;
  active_threads: DecisionThread[];
  recent_memories: SearchResult[];
  project_facts: KGFact[];
}

export interface DecisionThread {
  id: string;
  title: string;
  status: "open" | "resolved" | "deferred";
  events: ThreadEvent[];
  created_at: number;
  resolved_at?: number;
}

export type ThreadEventType =
  | "claim"
  | "evidence"
  | "decision"
  | "question"
  | "pivot";

export interface ThreadEvent {
  type: ThreadEventType;
  content: string;
  timestamp: number;
}

export interface ContextWindowState {
  token_count: number;
  message_count: number;
  last_compacted_at?: number;
}

// ── Knowledge Graph Types ─────────────────────────────────────────────────

export interface KGEntity {
  id: string;
  name: string;
  entity_type: string;
  properties: Record<string, unknown>;
  created_at: number;
}

export interface KGFact {
  id: string;
  subject: string;
  predicate: string;
  object: string;
  confidence: number;
  source_note_id?: NoteId;
  valid_from?: number;
  valid_to?: number;
  created_at: number;
}

export interface KGNode {
  id: string;
  name: string;
  type: string;
  facts: KGFact[];
}

export interface KGEdge {
  source: string;
  target: string;
  predicate: string;
  confidence: number;
}

// ── Belief Types ──────────────────────────────────────────────────────────

export interface BeliefAssertion {
  id: string;
  content: string;
  confidence: number;
  evidence_count: number;
  last_challenged_at?: number;
  superseded_by?: string;
  created_at: number;
}

// ── Memory Events ─────────────────────────────────────────────────────────

export type MemoryEvent =
  | { type: "memory.saved"; noteId: string; category: string }
  | { type: "memory.searched"; query: string; resultCount: number }
  | { type: "kg.entity.created"; entityId: string }
  | { type: "kg.fact.extracted"; factId: string }
  | { type: "contradiction.detected"; noteIds: string[] }
  | { type: "session.started"; sessionId: string }
  | { type: "session.compacting"; sessionId: string }
  | { type: "belief.updated"; beliefId: string; confidence: number };

export type KGEvent =
  | { type: "entity.created"; entity: KGEntity }
  | { type: "fact.extracted"; fact: KGFact }
  | { type: "contradiction.resolved"; factId: string };

// ── Health ────────────────────────────────────────────────────────────────

export interface HealthStatus {
  status: "healthy" | "degraded" | "unhealthy";
  python_process: boolean;
  db_accessible: boolean;
  embeddings_loaded: boolean;
  version: string;
}

// ── Unsubscribe ───────────────────────────────────────────────────────────

export type Unsubscribe = () => void;

// ── LLM Types ─────────────────────────────────────────────────────────────

export interface Message {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  tool_call_id?: string;
  tool_calls?: ToolCall[];
  name?: string;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: JSONSchema;
}

export interface JSONSchema {
  type: string;
  properties?: Record<string, unknown>;
  required?: string[];
  description?: string;
  items?: JSONSchema;
  [key: string]: unknown;
}

export interface ChatParams {
  model: string;
  messages: Message[];
  tools: ToolDefinition[];
  systemPrompt: string;
  temperature?: number;
  maxTokens?: number;
  stop?: string[];
}

export type ChatChunk =
  | { type: "text"; text: string }
  | { type: "tool_call"; id: string; name: string; arguments: string }
  | { type: "done"; reason: "stop" | "tool_calls" | "length" }
  | { type: "error"; error: string };

// ── Tool Types ────────────────────────────────────────────────────────────

export type ToolCategory =
  | "filesystem"
  | "terminal"
  | "search"
  | "memory"
  | "git"
  | "agent";

export interface Tool {
  name: string;
  description: string;
  category: ToolCategory;
  inputSchema: JSONSchema;
  execute: (args: Record<string, unknown>, ctx: ToolContext) => Promise<ToolResult>;
}

export interface ToolContext {
  projectRoot: string;
  sessionId: string;
  agentId: string;
  allowedPaths: string[];
}

export interface ToolResult {
  success: boolean;
  output: string;
  preview: string;
  error?: string;
  metadata?: Record<string, unknown>;
}

// ── Conversation Types ────────────────────────────────────────────────────

export type TurnEvent =
  | { type: "text"; text: string }
  | { type: "tool_call"; toolName: string; args: Record<string, unknown> }
  | { type: "tool_result"; toolName: string; result: ToolResult }
  | { type: "error"; error: string }
  | { type: "done" };

// ── Agent Types ───────────────────────────────────────────────────────────

export interface AgentConfig {
  id: string;
  model: string;
  systemPrompt: string;
  tools: string[];
  projectRoot: string;
}

export interface AgentInstance {
  id: string;
  config: AgentConfig;
  status: "running" | "idle" | "stopped" | "error";
}

// ── Workspace Types ───────────────────────────────────────────────────────

export interface Project {
  root: string;
  name: string;
  gitBranch?: string;
  files: FileEntry[];
}

export interface FileEntry {
  path: string;
  name: string;
  isDirectory: boolean;
  children?: FileEntry[];
  gitStatus?: string;
}

export interface WorkspaceContext {
  projectStructure: FileEntry[];
  activeFiles: string[];
  gitStatus: string;
  recentChanges: FileChange[];
}

export interface FileChange {
  path: string;
  changeType: "added" | "modified" | "deleted";
  timestamp: number;
}

// ── Context Builder Types ─────────────────────────────────────────────────

export interface ContextParams {
  sessionId: string;
  userMessage: string;
  activeFiles: string[];
  recentToolResults: Array<{ tool: string; result: string }>;
}

export interface BuildContext {
  systemPrompt: string;
  messages: Message[];
}

export interface ContextBudget {
  systemPrompt: number;
  sessionContext: number;
  proactiveMemory: number;
  kgFacts: number;
  conversationHistory: number;
  toolResults: number;
}

// ── App Store Types ───────────────────────────────────────────────────────

export type PanelLayout = "default" | "wide-editor" | "wide-chat" | "memory-focus";
export type Theme = string;

export interface OpenFile {
  path: string;
  name: string;
  content: string;
  language: string;
  isDirty: boolean;
  isAgentEdit: boolean;
}

// ── Change-Set Types ─────────────────────────────────────────────────────

export type FileEditKind = "modify" | "create" | "delete";

export interface FileEdit {
  path: string;
  kind: FileEditKind;
  /** Original content (for modify/delete — captured as pre-image). */
  oldText?: string;
  /** New content (for modify/create). */
  newText?: string;
}

export interface ChangeSet {
  id: string;
  summary: string;
  edits: FileEdit[];
  createdAt: number;
  applied: boolean;
  reverted: boolean;
}

// ── Git Panel Types ──────────────────────────────────────────────────────

export interface GitStatusFile {
  path: string;
  status: "added" | "modified" | "deleted" | "renamed" | "untracked";
  staged: boolean;
}

export interface GitBranchInfo {
  current: string;
  branches: string[];
}

// ── @-Mention Types ──────────────────────────────────────────────────────

export type MentionKind = "file" | "symbol" | "folder" | "memory" | "entity";

export interface MentionItem {
  kind: MentionKind;
  label: string;
  /** Path for file/folder, note_id for memory, entity name for entity. */
  value: string;
  description?: string;
  icon?: string;
}
