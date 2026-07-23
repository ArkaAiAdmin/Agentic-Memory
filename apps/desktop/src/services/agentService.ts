/**
 * Agent Service
 *
 * Bootstraps and manages the full agent stack:
 * - MemoryBridgeClient (Python subprocess)
 * - LiteLLMBridgeProvider (Python subprocess)
 * - ContextBuilder (memory-driven context assembly)
 * - ToolRegistry + ToolExecutor (with memory events)
 * - ConversationLoop (the heart)
 *
 * This is the main entry point for the agent system.
 */

import { memoryBridge } from "@ami/memory-bridge";
import { LiteLLMBridgeProvider, createProvider, type LLMProvider, type ProviderConfig } from "@ami/llm";
import {
  ConversationLoop,
  ContextBuilder,
  ToolRegistry,
  ToolExecutor,
} from "@ami/core";
import type { Tool, ToolResult, ToolContext, TurnEvent, JSONSchema } from "@ami/shared";
import { fs as fsIpc, git as gitIpc, process as processIpc } from "../ipc/client";
import { getWorkerManager } from "./workerManager";

// ── Built-in Tools ────────────────────────────────────────────────────────

function createBuiltinTools(): Tool[] {
  return [
    // Filesystem tools
    {
      name: "readFile",
      description: "Read the contents of a file at the given path.",
      category: "filesystem",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "File path to read" },
        },
        required: ["path"],
      } as JSONSchema,
      execute: async (args, ctx) => {
        const content = await fsIpc.readFile(args.path as string);
        return {
          success: true,
          output: content,
          preview: content.slice(0, 500),
        };
      },
    },
    {
      name: "writeFile",
      description: "Write content to a file, creating it if it doesn't exist.",
      category: "filesystem",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "File path to write" },
          content: { type: "string", description: "Content to write" },
        },
        required: ["path", "content"],
      } as JSONSchema,
      execute: async (args) => {
        await fsIpc.writeFile(args.path as string, args.content as string);
        return {
          success: true,
          output: `Wrote ${(args.content as string)?.length ?? 0} bytes to ${args.path}`,
          preview: `Wrote to ${args.path}`,
        };
      },
    },
    {
      name: "listDirectory",
      description: "List files and directories in a directory.",
      category: "filesystem",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "Directory path" },
        },
        required: ["path"],
      } as JSONSchema,
      execute: async (args) => {
        const entries = await fsIpc.listDir(args.path as string);
        const output = entries
          .map((e) => `${e.isDir ? "📁" : "📄"} ${e.name}`)
          .join("\n");
        return {
          success: true,
          output,
          preview: `${entries.length} entries`,
        };
      },
    },
    {
      name: "globFiles",
      description: "Find files matching a glob pattern.",
      category: "filesystem",
      inputSchema: {
        type: "object",
        properties: {
          pattern: { type: "string", description: "Glob pattern" },
          cwd: { type: "string", description: "Working directory" },
        },
        required: ["pattern"],
      } as JSONSchema,
      execute: async (args) => {
        const result = await processIpc.run(
          `find ${args.cwd ?? "."} -name "${args.pattern}" -type f 2>/dev/null | head -50`,
          (args.cwd as string) ?? "/",
        );
        return {
          success: result.exitCode === 0,
          output: result.stdout,
          preview: result.stdout.slice(0, 500),
        };
      },
    },
    {
      name: "grepSearch",
      description: "Search file contents with a regex pattern.",
      category: "filesystem",
      inputSchema: {
        type: "object",
        properties: {
          pattern: { type: "string", description: "Regex pattern" },
          path: { type: "string", description: "Directory to search in" },
        },
        required: ["pattern"],
      } as JSONSchema,
      execute: async (args) => {
        const result = await processIpc.run(
          `grep -rn "${args.pattern}" ${args.path ?? "."} 2>/dev/null | head -50`,
          (args.path as string) ?? "/",
        );
        return {
          success: result.exitCode === 0,
          output: result.stdout,
          preview: result.stdout.slice(0, 500),
        };
      },
    },

    // Terminal tools
    {
      name: "runCommand",
      description: "Execute a shell command and return its output.",
      category: "terminal",
      inputSchema: {
        type: "object",
        properties: {
          command: { type: "string", description: "Shell command to execute" },
          cwd: { type: "string", description: "Working directory" },
        },
        required: ["command"],
      } as JSONSchema,
      execute: async (args) => {
        const result = await processIpc.run(
          args.command as string,
          (args.cwd as string) ?? "/",
        );
        return {
          success: result.exitCode === 0,
          output: result.stdout + (result.stderr ? `\nSTDERR: ${result.stderr}` : ""),
          preview: result.stdout.slice(0, 500),
        };
      },
    },

    // Git tools
    {
      name: "gitStatus",
      description: "Show the current git status.",
      category: "git",
      inputSchema: {
        type: "object",
        properties: {
          repoPath: { type: "string", description: "Repository path" },
        },
        required: ["repoPath"],
      } as JSONSchema,
      execute: async (args) => {
        const status = await gitIpc.status(args.repoPath as string);
        return {
          success: true,
          output: status,
          preview: status.slice(0, 500),
        };
      },
    },
    {
      name: "gitDiff",
      description: "Show git diff for the repository.",
      category: "git",
      inputSchema: {
        type: "object",
        properties: {
          repoPath: { type: "string", description: "Repository path" },
          filePath: { type: "string", description: "Optional file path to diff" },
        },
        required: ["repoPath"],
      } as JSONSchema,
      execute: async (args) => {
        const diff = await gitIpc.diff(
          args.repoPath as string,
          args.filePath as string | undefined,
        );
        return {
          success: true,
          output: diff,
          preview: diff.slice(0, 500),
        };
      },
    },
    {
      name: "gitLog",
      description: "Show recent git log.",
      category: "git",
      inputSchema: {
        type: "object",
        properties: {
          repoPath: { type: "string", description: "Repository path" },
          limit: { type: "number", description: "Number of commits" },
        },
        required: ["repoPath"],
      } as JSONSchema,
      execute: async (args) => {
        const log = await gitIpc.log(
          args.repoPath as string,
          (args.limit as number) ?? 10,
        );
        return {
          success: true,
          output: log,
          preview: log.slice(0, 500),
        };
      },
    },

    // Memory tools
    {
      name: "memorySearch",
      description: "Search the memory system for relevant memories.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "Search query" },
          limit: { type: "number", description: "Max results" },
        },
        required: ["query"],
      } as JSONSchema,
      execute: async (args) => {
        const results = await memoryBridge.search({
          query: args.query as string,
          limit: (args.limit as number) ?? 10,
        });
        const output = results
          .map((r) => `[${r.category}] ${r.content} (score: ${r.score.toFixed(2)})`)
          .join("\n");
        return {
          success: true,
          output,
          preview: `${results.length} memories found`,
        };
      },
    },
    {
      name: "memorySave",
      description: "Save a memory for future retrieval.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          content: { type: "string", description: "Content to remember" },
          category: { type: "string", description: "Memory category" },
          tags: {
            type: "array",
            items: { type: "string" },
            description: "Tags",
          },
        },
        required: ["content", "category"],
      } as JSONSchema,
      execute: async (args) => {
        const id = await memoryBridge.save({
          content: args.content as string,
          category: (args.category as any) ?? "auto_save",
          tags: (args.tags as string[]) ?? [],
        });
        return {
          success: true,
          output: `Saved memory ${id}`,
          preview: `Memory saved: ${id}`,
        };
      },
    },
  ];
}

// ── Agent Service ─────────────────────────────────────────────────────────

export interface AgentServiceConfig {
  model: string;
  maxTurns: number;
  temperature: number;
  memoryDir: string;
  provider: ProviderConfig;
}

const DEFAULT_CONFIG: AgentServiceConfig = {
  model: "gpt-4o",
  maxTurns: 25,
  temperature: 0.7,
  memoryDir: `${(globalThis as any).process?.env?.HOME ?? (globalThis as any).process?.env?.USERPROFILE ?? "/"}/.config/agentic-memory`,
  provider: { type: "openai" },
};

class AgentService {
  private llm: LLMProvider | null = null;
  private contextBuilder: ContextBuilder | null = null;
  private toolRegistry: ToolRegistry | null = null;
  private toolExecutor: ToolExecutor | null = null;
  private conversationLoops = new Map<string, ConversationLoop>();
  private _initialized = false;
  private config: AgentServiceConfig;

  constructor(config?: Partial<AgentServiceConfig>) {
    this.config = { ...DEFAULT_CONFIG, ...config };
  }

  get isInitialized(): boolean {
    return this._initialized;
  }

  get memoryRunning(): boolean {
    return memoryBridge.isRunning;
  }

  getOrCreateLoop(sessionId: string): ConversationLoop {
    let loop = this.conversationLoops.get(sessionId);
    if (!loop) {
      if (!this._initialized) {
        throw new Error("Agent service not initialized. Call initialize() first.");
      }
      loop = new ConversationLoop(
        {
          model: this.config.model,
          maxTurns: this.config.maxTurns,
          temperature: this.config.temperature,
        },
        this.llm!,
        this.contextBuilder!,
        this.toolRegistry!,
        this.toolExecutor!,
        memoryBridge,
        sessionId,
      );
      this.conversationLoops.set(sessionId, loop);
    }
    return loop;
  }

  /**
   * Initialize the full agent stack.
   */
  async initialize(): Promise<void> {
    if (this._initialized) return;

    // 1. Start memory bridge
    await memoryBridge.start(this.config.memoryDir);

    // 2. Start LLM provider
    this.llm = createProvider(this.config.provider);
    await this.llm.start();

    // 3. Create context builder
    this.contextBuilder = new ContextBuilder(
      memoryBridge,
      this.llm.maxContextTokens(),
    );

    // 4. Create tool registry with built-in tools
    this.toolRegistry = new ToolRegistry();
    this.toolRegistry.registerAll(createBuiltinTools());

    // 5. Create tool executor
    this.toolExecutor = new ToolExecutor(this.toolRegistry, memoryBridge);

    // 6. Create default conversation loop
    this.getOrCreateLoop("default");

    this._initialized = true;

    // Start background workers
    getWorkerManager(this.config.memoryDir).start();
  }

  /**
   * Send a message and get an async iterable of turn events.
   * @param sessionId - The conversation session to use. Defaults to "default".
   */
  async *sendMessage(message: string, sessionId = "default"): AsyncIterable<TurnEvent> {
    if (!this._initialized) {
      yield { type: "error", error: "Agent not initialized. Call initialize() first." };
      return;
    }

    const loop = this.getOrCreateLoop(sessionId);
    yield* loop.turn(message);
  }

  /**
   * Get the tool registry (for UI to show available tools).
   */
  getToolRegistry(): ToolRegistry | null {
    return this.toolRegistry;
  }

  /**
   * Stop all services.
   */
  async shutdown(): Promise<void> {
    // Stop background workers
    getWorkerManager().stop();

    await memoryBridge.stop();
    if (this.llm) {
      await this.llm.stop();
    }
    this._initialized = false;
  }
}

// Singleton
export const agentService = new AgentService();
