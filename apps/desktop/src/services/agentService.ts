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
import { createProvider, type LLMProvider, type ProviderConfig } from "@ami/llm";
import {
  ConversationLoop,
  ContextBuilder,
  ToolRegistry,
  ToolExecutor,
} from "@ami/core";
import type { Tool, TurnEvent, JSONSchema, SearchResult } from "@ami/shared";
import { fs as fsIpc, git as gitIpc, process as processIpc } from "../ipc/client";
import { getWorkerManager } from "./workerManager";
import { addEditToChangeSet } from "./changeSet";
import { toolContributorRegistry } from "./toolContributors";
import { agentRegistry } from "./agentRegistry";


// ── Input Validation ───────────────────────────────────────────────────────

const MAX_COMMAND_LENGTH = 4096;

function validateCommandInput(command: unknown): string {
  if (typeof command !== "string") {
    throw new Error("Command must be a string");
  }
  const trimmed = command.trim();
  if (trimmed.length === 0) {
    throw new Error("Command cannot be empty");
  }
  if (trimmed.length > MAX_COMMAND_LENGTH) {
    throw new Error(`Command exceeds maximum length of ${MAX_COMMAND_LENGTH}`);
  }
  return trimmed;
}

function validatePathInput(path: unknown): string {
  if (typeof path !== "string") {
    throw new Error("Path must be a string");
  }
  const trimmed = path.trim();
  if (trimmed.length === 0) {
    throw new Error("Path cannot be empty");
  }
  return trimmed;
}

// ── Built-in Tools ────────────────────────────────────────────────────────

function createBuiltinTools(): Tool[] {
  return [
    // Filesystem tools
    {
      name: "readFile",
      description: "Read the contents of a file at the given path. Returns numbered lines (e.g. '  1| code here') so you can use editLines to edit specific ranges.",
      category: "filesystem",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "File path to read" },
          startLine: { type: "number", description: "Start line (1-based, optional — reads from this line)" },
          endLine: { type: "number", description: "End line (1-based, optional — reads up to this line)" },
        },
        required: ["path"],
      } as JSONSchema,
      execute: async (args, _ctx) => {
        const raw = await fsIpc.readFile(args.path as string);
        const allLines = raw.split("\n");
        const start = Math.max(1, (args.startLine as number) || 1);
        const end = Math.min(allLines.length, (args.endLine as number) || allLines.length);
        const lines = allLines.slice(start - 1, end);
        const padWidth = String(end).length;
        const numbered = lines.map((line, i) =>
          `${String(start + i).padStart(padWidth)}| ${line}`
        ).join("\n");
        const header = `File: ${args.path} (${allLines.length} lines total, showing L${start}-L${end})`;
        const content = `${header}\n${numbered}`;
        return {
          success: true,
          output: content,
          preview: content.slice(0, 500),
        };
      },
    },
    {
      name: "writeFile",
      description: "Write content to a file, creating it if it doesn't exist. Changes are staged as a change-set for review.",
      category: "filesystem",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "File path to write" },
          content: { type: "string", description: "Content to write" },
          summary: { type: "string", description: "Brief summary of the change (for change-set)" },
        },
        required: ["path", "content"],
      } as JSONSchema,
      execute: async (args) => {
        const path = args.path as string;
        const content = args.content as string;
        const summary = (args.summary as string) ?? `Write to ${path}`;

        // Stage as a change-set instead of writing directly
        const cs = await addEditToChangeSet(null, summary, {
          path,
          kind: "modify",
          newText: content,
        });

        return {
          success: true,
          output: `Staged change-set ${cs.id}: ${summary}\nFile: ${path} (${content.length} bytes)\nReview in the Composer panel, or the write will be applied on next approval.`,
          preview: `Staged: ${path}`,
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
        const cwd = validatePathInput(args.cwd) || ".";
        const pattern = validateCommandInput(args.pattern).replace(/[^a-zA-Z0-9_\-.*?/\\[\]{}!@#%^&+=~`'"]/g, "");
        try {
          const result = await processIpc.run(
            `find ${JSON.stringify(cwd)} -name ${JSON.stringify(pattern)} -type f -maxdepth 10`,
            cwd,
          );
          const lines = result.stdout.split("\n").filter(Boolean).slice(0, 50);
          return {
            success: true,
            output: lines.join("\n"),
            preview: lines.slice(0, 10).join("\n"),
          };
        } catch {
          return {
            success: false,
            output: "",
            preview: "Glob search failed",
          };
        }
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
        const searchPath = (args.path as string) ?? ".";
        const pattern = (args.pattern as string).replace(/[`$()|;&<>{}!]/g, "");
        try {
          const result = await processIpc.run(
            `grep -rn -- "${pattern}" "${searchPath}" 2>/dev/null | head -50`,
            searchPath,
          );
          return {
            success: true,
            output: result.stdout,
            preview: result.stdout.slice(0, 500),
          };
        } catch {
          return {
            success: false,
            output: "",
            preview: "Grep search failed",
          };
        }
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
        const command = validateCommandInput(args.command);
        const cwd = validatePathInput(args.cwd) || "/";
        const result = await processIpc.run(command, cwd);
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

    // Editing tools
    {
      name: "editLines",
      description: "Edit a file by replacing a specific line range with new content. Use readFile first to see line numbers, then specify the range to replace. This is the preferred way to make surgical edits.",
      category: "filesystem",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "File path to edit" },
          startLine: { type: "number", description: "First line number to replace (1-based, inclusive)" },
          endLine: { type: "number", description: "Last line number to replace (1-based, inclusive)" },
          newContent: { type: "string", description: "Replacement content (replaces the entire range). Can be more or fewer lines than the original." },
          summary: { type: "string", description: "Brief description of the edit" },
        },
        required: ["path", "startLine", "endLine", "newContent"],
      } as JSONSchema,
      execute: async (args) => {
        const path = args.path as string;
        const startLine = args.startLine as number;
        const endLine = args.endLine as number;
        const newContent = args.newContent as string;
        const summary = (args.summary as string) ?? `Edit lines ${startLine}-${endLine} in ${path}`;

        // Read the file, splice lines, stage as change-set
        const original = await fsIpc.readFile(path);
        const lines = original.split("\n");
        const before = lines.slice(0, startLine - 1);
        const after = lines.slice(endLine);
        const replacement = newContent.split("\n");
        const result = [...before, ...replacement, ...after].join("\n");

        const cs = await addEditToChangeSet(null, summary, {
          path,
          kind: "modify",
          newText: result,
        });

        return {
          success: true,
          output: `Staged edit ${cs.id}: replaced lines ${startLine}-${endLine} (${endLine - startLine + 1} lines -> ${replacement.length} lines)\nFile: ${path}\n${summary}`,
          preview: `Edited L${startLine}-L${endLine} in ${path}`,
        };
      },
    },
    {
      name: "searchReplace",
      description: "Find and replace exact text in a file. The search text must match exactly (including whitespace). Use this for precise, targeted edits when you know the exact text to change.",
      category: "filesystem",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "File path to edit" },
          search: { type: "string", description: "Exact text to find (must match precisely, including whitespace)" },
          replace: { type: "string", description: "Text to replace it with" },
          summary: { type: "string", description: "Brief description of the change" },
        },
        required: ["path", "search", "replace"],
      } as JSONSchema,
      execute: async (args) => {
        const path = args.path as string;
        const search = args.search as string;
        const replace = args.replace as string;
        const summary = (args.summary as string) ?? `Replace text in ${path}`;

        const original = await fsIpc.readFile(path);
        if (!original.includes(search)) {
          return {
            success: false,
            output: `Search text not found in ${path}. Make sure it matches exactly including whitespace and newlines.`,
            preview: "No match found",
          };
        }

        const result = original.replace(search, replace);
        const cs = await addEditToChangeSet(null, summary, {
          path,
          kind: "modify",
          newText: result,
        });

        return {
          success: true,
          output: `Staged replacement ${cs.id}: ${summary}\nFile: ${path}`,
          preview: `Replaced in ${path}`,
        };
      },
    },

    // Memory tools
    {
      name: "memorySearch",
      description: "Search the memory system for relevant memories. Supports semantic, FTS, hybrid, fact, and graph modes. Can include shared memories from other agents.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "Search query (optional — omitting returns recent context)" },
          limit: { type: "number", description: "Max results (default 10)" },
          category: { type: "string", description: "Filter by category: lessons, decisions, projects, sessions, preferences" },
          mode: { type: "string", enum: ["hybrid", "semantic", "fts", "facts", "graph"], description: "Search mode (default hybrid)" },
          shared_with_me: { type: "boolean", description: "Include memories shared by other agents" },
          include_global: { type: "boolean", description: "Include global memories (default true)" },
          belief_status: { type: "string", enum: ["active", "retracted", "deprecated", "unconfirmed"], description: "Filter KG facts by belief status" },
          memory_source: { type: "string", enum: ["agent", "auto_save", "import"], description: "Filter by source type" },
        },
      } as JSONSchema,
      execute: async (args) => {
        const query = args.query as string | undefined;
        let results: SearchResult[];
        if (query) {
          results = await memoryBridge.search({
            query,
            limit: (args.limit as number) ?? 10,
            category: args.category as any,
            mode: (args.mode as any) ?? "hybrid",
          });
        } else {
          const ctx = await memoryBridge.recall();
          results = ctx.memories.slice(0, (args.limit as number) ?? 10);
        }
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
      description: "Save a memory for future retrieval. Supports categories, importance scoring, pinning, and multi-agent sharing.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          content: { type: "string", description: "Content to remember" },
          category: { type: "string", description: "Memory category: lessons, decisions, projects, sessions, preferences" },
          importance: { type: "number", description: "Importance 1-5 (default 3). High-importance memories survive compaction." },
          tags: {
            type: "array",
            items: { type: "string" },
            description: "Tags",
          },
          pinned: { type: "boolean", description: "Pin to hot tier (prevents archival)" },
          is_global: { type: "boolean", description: "Save to global memory store" },
          title_slug: { type: "string", description: "Custom URL-friendly slug (auto-generated if empty)" },
        },
        required: ["content", "category"],
      } as JSONSchema,
      execute: async (args) => {
        const id = await memoryBridge.save({
          content: args.content as string,
          category: (args.category as any) ?? "auto_save",
          tags: (args.tags as string[]) ?? [],
          importance: args.importance as number | undefined,
        });
        return {
          success: true,
          output: `Saved memory ${id}`,
          preview: `Memory saved: ${id}`,
        };
      },
    },
    {
      name: "memoryCoordinate",
      description: "Multi-agent coordination: task management, file locking, inter-agent messaging, and project state. Actions: create_task, claim_task, update_task_status, release_task, complete_task, list_tasks, lock_file, unlock_file, check_lock, send_message, read_messages, get_project_state, update_project_state.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          action: {
            type: "string",
            enum: ["create_task", "claim_task", "update_task_status", "release_task", "complete_task", "list_tasks", "lock_file", "unlock_file", "check_lock", "send_message", "read_messages", "get_project_state", "update_project_state"],
            description: "Coordination action to perform",
          },
          task_id: { type: "number", description: "Task ID (required for claim, update, release, complete)" },
          task_type: { type: "string", description: "Task type label (for create_task)" },
          description: { type: "string", description: "Task description (for create_task)" },
          assigned_to: { type: "string", description: "Agent ID to assign task to (for create_task)" },
          status: { type: "string", description: "New task status (for update_task_status)" },
          file_path: { type: "string", description: "File path to lock/unlock/check" },
          to_agent: { type: "string", description: "Recipient agent ID (for send_message)" },
          message_type: { type: "string", description: "Message type label (for send_message)" },
          payload: { type: "string", description: "Message payload content (for send_message)" },
          key: { type: "string", description: "Project state key (for update_project_state)" },
          value: { type: "string", description: "Project state value (for update_project_state)" },
          project_id: { type: "string", description: "Project scope identifier" },
        },
        required: ["action"],
      } as JSONSchema,
      execute: async (args) => {
        const action = args.action as string;
        const params: Record<string, unknown> = { ...args };
        delete params.action;
        const result = await memoryBridge.coordinate(action, params);
        return {
          success: true,
          output: JSON.stringify(result, null, 2),
          preview: `Coordinate ${action} completed`,
        };
      },
    },
    {
      name: "memoryShare",
      description: "Share memories with other agents or view/import from the shared pool. Actions: list (view shared pool), share (push one of your memories), import (pull into your DB), stats (sharing overview).",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          action: {
            type: "string",
            enum: ["list", "share", "import", "stats"],
            description: "Sharing action",
          },
          note_id: { type: "string", description: "Memory note ID to share or import (required for share/import)" },
          share_with: { type: "string", description: "Target agent ID (for share action)" },
        },
        required: ["action"],
      } as JSONSchema,
      execute: async (args) => {
        const action = args.action as string;
        if (action === "share") {
          const result = await memoryBridge.shareMemory(
            args.note_id as string,
            args.share_with as string | undefined,
          );
          return { success: true, output: JSON.stringify(result), preview: "Memory shared" };
        }
        if (action === "list") {
          const result = await memoryBridge.listSharedMemories();
          return { success: true, output: JSON.stringify(result, null, 2), preview: "Shared memories listed" };
        }
        if (action === "import") {
          const result = await memoryBridge.importSharedMemory(
            args.note_id as string,
            args.share_with as string,
          );
          return { success: true, output: JSON.stringify(result), preview: "Memory imported" };
        }
        if (action === "stats") {
          const result = await memoryBridge.maintenance("shared_stats");
          return { success: true, output: JSON.stringify(result, null, 2), preview: "Sharing stats" };
        }
        return { success: false, output: `Unknown share action: ${action}`, preview: "Unknown action" };
      },
    },
    {
      name: "memoryAgentList",
      description: "List all active agents in the multi-agent system, their roles, status, and last active time.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {},
      } as JSONSchema,
      execute: async () => {
        const result = await memoryBridge.listAgents();
        const agents = result?.agents ?? [];
        const output = agents.map((a: any) =>
          `[${a.agent_id}] ${a.metadata?.name ?? a.agent_id} — role: ${a.metadata?.role ?? "assistant"} — last: ${a.last_active ?? "unknown"}`
        ).join("\n");
        return {
          success: true,
          output: output || "No agents registered",
          preview: `${agents.length} agent(s) found`,
        };
      },
    },
    {
      name: "memoryAgentInit",
      description: "Register or update this agent's identity in the memory system. Call this at session start to announce your presence to other agents.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          agent_id: { type: "string", description: "Unique agent identifier" },
          display_name: { type: "string", description: "Human-readable name" },
          role: { type: "string", description: "Agent role description" },
        },
        required: ["agent_id", "display_name"],
      } as JSONSchema,
      execute: async (args) => {
        await memoryBridge.initAgent(
          args.agent_id as string,
          { displayName: args.display_name as string },
        );
        return {
          success: true,
          output: `Agent ${args.agent_id} initialized`,
          preview: `Agent ${args.agent_id} ready`,
        };
      },
    },
    {
      name: "memoryRecall",
      description: "Retrieve session context briefing or recent activity. Use this when starting a new task to pick up where you left off.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "Topic to recall context for (optional)" },
          session_id: { type: "string", description: "Specific session to recall" },
        },
      } as JSONSchema,
      execute: async (args) => {
        const result = await memoryBridge.recall(
          args.query as string | undefined,
          args.session_id as string | undefined,
        );
        return {
          success: true,
          output: result.context || JSON.stringify(result.memories?.slice(0, 10) ?? []),
          preview: `${result.memories?.length ?? 0} memories recalled`,
        };
      },
    },
    {
      name: "memoryNote",
      description: "Read, update, patch, or supersede a specific memory note by ID. Use read to get full content, update to modify, supersede to mark outdated.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          note_id: { type: "string", description: "Note ID (e.g. lessons/my-note)" },
          action: {
            type: "string",
            enum: ["read", "update", "delete", "supersede", "patch", "restore"],
            description: "Operation to perform (default read)",
          },
          content: { type: "string", description: "New content (required for update/supersede)" },
          rationale: { type: "string", description: "Reason for the action (recommended for supersede/patch/delete)" },
        },
        required: ["note_id"],
      } as JSONSchema,
      execute: async (args) => {
        const result = await memoryBridge.callTool("memory_note", {
          note_id: args.note_id as string,
          action: (args.action as string) ?? "read",
          content: args.content as string | undefined,
          rationale: args.rationale as string | undefined,
        });
        return {
          success: true,
          output: JSON.stringify(result, null, 2),
          preview: `Note ${args.note_id} ${args.action ?? "read"} completed`,
        };
      },
    },
    {
      name: "memoryAudit",
      description: "Review recent memory system activity, errors, and health. Use this to see what other agents have been doing or diagnose issues.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          hours: { type: "number", description: "Look back window in hours (default 24)" },
          limit: { type: "number", description: "Max results (default 20)" },
          include_errors: { type: "boolean", description: "Include error entries (default true)" },
        },
      } as JSONSchema,
      execute: async (args) => {
        const result = await memoryBridge.audit({
          hours: (args.hours as number) ?? 24,
        });
        const entries = (Array.isArray(result) ? result : (result as any)?.entries ?? []) as any[];
        const output = entries
          .slice(0, (args.limit as number) ?? 20)
          .map((e: any) => `[${e.timestamp ?? e.ts ?? "?"}] ${e.tool ?? e.event ?? e.type ?? "?"} — ${e.status ?? e.message ?? ""}`)
          .join("\n");
        return {
          success: true,
          output: output || "No recent activity",
          preview: `${entries.length} audit entries found`,
        };
      },
    },
    {
      name: "memoryLearn",
      description: "Save a lesson or compile a skill from content. Auto-categorizes and tags the memory. When as_skill=True, compiles a reusable skill rule file. Use this to capture hard-won knowledge as a repeatable skill.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          content: { type: "string", description: "The lesson/skill content (markdown)" },
          as_skill: { type: "boolean", description: "If true, compile as a reusable skill (default false)" },
          skill_name: { type: "string", description: "Skill directory name (required if as_skill=true)" },
          category: { type: "string", description: "Target category (default 'lessons')" },
          tags: { type: "string", description: "Comma-separated tags" },
        },
        required: ["content"],
      } as JSONSchema,
      execute: async (args) => {
        const result = await memoryBridge.callTool("memory_learn", {
          content: args.content as string,
          as_skill: args.as_skill as boolean | undefined,
          skill_name: args.skill_name as string | undefined,
          category: (args.category as string) ?? "lessons",
          tags: args.tags as string | undefined,
        });
        return {
          success: true,
          output: JSON.stringify(result, null, 2),
          preview: "Lesson/skill saved",
        };
      },
    },
    {
      name: "memoryListSkills",
      description: "List all compiled skills ordered by hit count. Shows topic, hit count, last-used timestamp, and description preview. Use this to discover what skills are available from previous sessions.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          limit: { type: "number", description: "Max results (default 50)" },
        },
      } as JSONSchema,
      execute: async (args) => {
        const result = await memoryBridge.callTool("memory_list_skills", {
          limit: (args.limit as number) ?? 50,
        });
        return {
          success: true,
          output: JSON.stringify(result, null, 2),
          preview: "Skills listed",
        };
      },
    },
    {
      name: "memoryExtractSkills",
      description: "Manually trigger skill extraction on a specific memory or all memories. Use when auto-extraction didn't fire or you want to compile a skill from an existing note.",
      category: "memory",
      inputSchema: {
        type: "object",
        properties: {
          memory_id: { type: "string", description: "Specific memory ID to extract from (e.g. 'lessons/foo'). Empty = extract from all memories." },
          dry_run: { type: "boolean", description: "Preview what would be extracted without writing" },
        },
      } as JSONSchema,
      execute: async (args) => {
        const result = await memoryBridge.callTool("memory_extract_skills", {
          memory_id: (args.memory_id as string) ?? "",
          dry_run: args.dry_run as boolean | undefined,
        });
        return {
          success: true,
          output: JSON.stringify(result, null, 2),
          preview: "Skill extraction done",
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
  maxTokens: number;
  memoryDir: string;
  provider: ProviderConfig;
}

const DEFAULT_CONFIG: AgentServiceConfig = {
  model: "gpt-4o",
  maxTurns: 25,
  temperature: 0.7,
  maxTokens: 16384,
  memoryDir: "", // Will be set during initialization
  provider: { type: "openai" },
};

class AgentService {
  private llm: LLMProvider | null = null;
  private contextBuilder: ContextBuilder | null = null;
  private toolRegistry: ToolRegistry | null = null;
  private toolExecutor: ToolExecutor | null = null;
  private conversationLoops = new Map<string, ConversationLoop>();
  private _initialized = false;
  private _providerLock = false;
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

  getOrCreateLoop(sessionId: string, agentId?: string): ConversationLoop {
    const activeAgentId = agentId || agentRegistry.getActiveAgentId() || "default";
    const loopKey = `${sessionId}:${activeAgentId}`;

    let loop = this.conversationLoops.get(loopKey);
    if (!loop) {
      if (!this._initialized) {
        throw new Error("Agent service not initialized. Call initialize() first.");
      }

      const agentIdentity = agentRegistry.get(activeAgentId);
      const systemPrompt = agentIdentity
        ? `You are ${agentIdentity.name} (Role: ${agentIdentity.role}, ID: ${activeAgentId}). Assist the user in their codebase.`
        : undefined;

      loop = new ConversationLoop(
        {
          model: this.config.model,
          maxTurns: this.config.maxTurns,
          temperature: this.config.temperature,
          maxTokens: this.config.maxTokens,
          systemPrompt,
        },
        this.llm!,
        this.contextBuilder!,
        this.toolRegistry!,
        this.toolExecutor!,
        sessionId,
      );
      this.conversationLoops.set(loopKey, loop);
    }
    return loop;
  }

  /**
   * Initialize the full agent stack.
   */
  async initialize(): Promise<void> {
    if (this._initialized) return;

    // Get home directory for memory path
    try {
      const { homeDir } = await import("@tauri-apps/api/path");
      this.config.memoryDir = `${await homeDir()}/.config/agentic-memory`;
      console.log("[AgentService] Memory dir:", this.config.memoryDir);
    } catch {
      const home = (() => {
        if (typeof process !== "undefined" && process.env?.HOME) return process.env.HOME;
        if (typeof process !== "undefined" && process.env?.USERPROFILE) return process.env.USERPROFILE;
        return "/tmp";
      })();
      this.config.memoryDir = `${home}/.config/agentic-memory`;
      console.warn("[AgentService] Using fallback memory dir:", this.config.memoryDir);
    }

    // 1. Start memory bridge in the BACKGROUND (don't block LLM init)
    // The bridge handles Tauri IPC, MCP handshake, and polling internally.
    // Skip in browser-only mode (no Rust backend)
    const hasTauri = typeof window !== "undefined" &&
      Boolean((window as any).__TAURI_INTERNALS__ || (window as any).__TAURI__);

    if (hasTauri) {
      try {
        console.log("[AgentService] Starting memory bridge (background)...");
        await memoryBridge.start(this.config.memoryDir);
        console.log("[AgentService] Memory bridge ready");
      } catch (err) {
        console.error("[AgentService] Failed to start memory bridge:", err);
      }
    } else {
      console.warn("[AgentService] Skipping memory bridge (browser-only mode)");
    }

    // 2. Start LLM provider
    this.llm = createProvider(this.config.provider);
    await this.llm.start();

    // 3. Create context builder with rules loader
    this.contextBuilder = new ContextBuilder(
      memoryBridge,
      this.llm.maxContextTokens(),
      undefined,
      this.createRulesLoader(),
    );

    // 4. Create tool registry with built-in tools
    this.toolRegistry = new ToolRegistry();

    // Register built-in tools as a contributor (so they can be extended/overridden)
    toolContributorRegistry.register({
      id: "builtin",
      source: "builtin",
      getTools: () => createBuiltinTools(),
    });

    // Load all tools from contributors
    for (const tool of toolContributorRegistry.getAllTools()) {
      this.toolRegistry.register(tool);
    }

    // 5. Create tool executor
    this.toolExecutor = new ToolExecutor(this.toolRegistry, memoryBridge);

    // 6. Mark as initialized BEFORE creating the loop
    this._initialized = true;

    // 7. Initialize agent registry sync
    agentRegistry.init().catch(err => console.warn("[AgentService] AgentRegistry init error:", err));

    // 8. Create default conversation loop
    this.getOrCreateLoop("default");

    // Start background workers
    getWorkerManager(this.config.memoryDir).start();
  }

  /**
   * Apply a new LLM provider configuration (from Settings). If the service is
   * already initialized, the underlying provider is rebuilt and cached
   * conversation loops are dropped so subsequent turns use the new provider.
   * Conversation history is preserved in the app store, not in the loops.
   */
  async setProvider(provider: ProviderConfig): Promise<void> {
    if (this._providerLock) {
      await new Promise((r) => setTimeout(r, 50));
      if (this._providerLock) return;
    }
    this._providerLock = true;

    try {
      this.config.provider = provider;
      if (provider.model) this.config.model = provider.model;

      if (!this._initialized) return;

      const next = createProvider(provider);
      await next.start();
      const prev = this.llm;
      this.llm = next;
      this.contextBuilder = new ContextBuilder(
        memoryBridge,
        this.llm.maxContextTokens(),
      );
      this.conversationLoops.clear();
      try {
        await prev?.stop();
      } catch {
        // best-effort shutdown of the previous provider
      }
    } finally {
      this._providerLock = false;
    }
  }

  /**
   * Send a message and get an async iterable of turn events.
   * @param sessionId - The conversation session to use. Defaults to "default".
   * @param agentId - Optional agent identity ID.
   */
  async *sendMessage(
    message: string,
    sessionId = "default",
    agentId?: string,
  ): AsyncIterable<TurnEvent> {
    if (!this._initialized) {
      yield { type: "error", error: "Agent not initialized. Call initialize() first." };
      return;
    }

    if (!this.llm) {
      yield { type: "error", error: "LLM provider not available. Provider may be transitioning." };
      return;
    }

    const activeAgentId = agentId || agentRegistry.getActiveAgentId() || "default";
    agentRegistry.setStatus(activeAgentId, "busy");

    try {
      const loop = this.getOrCreateLoop(sessionId, activeAgentId);
      yield* loop.turn(message);
    } finally {
      agentRegistry.setStatus(activeAgentId, "idle");
    }
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

  /**
   * Create a rules loader that reads AGENTS.md / .cursor/rules / CLAUDE.md
   * from the active project root.
   */
  private createRulesLoader(): (fileNames: string[]) => Promise<string> {
    return async (fileNames: string[]) => {
      const { useAppStore } = await import("../stores/appStore");
      const projectRoot = useAppStore.getState().activeProject;
      if (!projectRoot) return "";

      const parts: string[] = [];
      for (const name of fileNames) {
        try {
          const content = await fsIpc.readFile(`${projectRoot}/${name}`);
          if (content.trim()) {
            parts.push(`### ${name}\n${content.trim()}`);
          }
        } catch {
          // File doesn't exist — skip
        }
      }
      return parts.join("\n\n");
    };
  }
}

// Singleton
export const agentService = new AgentService();
