/**
 * Agent Modes
 *
 * Different modes change how the agent behaves:
 * - Chat: normal conversational mode (default)
 * - Plan: agent reads and analyzes before acting, produces a plan
 * - Build: agent focuses on running build commands, fixing errors
 * - Spec: agent helps create specifications/PRDs through guided questions
 */

export type AgentMode = "chat" | "plan" | "build" | "spec";

export interface ModeConfig {
  id: AgentMode;
  name: string;
  description: string;
  icon: string;
  /** System prompt additions for this mode */
  systemPrompt: string;
  /** Tools available in this mode */
  tools: string[];
  /** Whether the agent should auto-approve tool calls */
  autoApprove: boolean;
}

export const AGENT_MODES: Record<AgentMode, ModeConfig> = {
  chat: {
    id: "chat",
    name: "Chat",
    description: "Normal conversational mode",
    icon: "💬",
    systemPrompt: "",
    tools: ["readFile", "writeFile", "listDirectory", "globFiles", "grepSearch", "runCommand", "gitStatus", "gitDiff", "gitLog", "memorySearch", "memorySave"],
    autoApprove: false,
  },
  plan: {
    id: "plan",
    name: "Plan",
    description: "Read and analyze before acting",
    icon: "📋",
    systemPrompt: `## Plan Mode
You are in PLAN MODE. Your job is to ANALYZE and PLAN, not to implement.

Rules:
1. Read all relevant files before making any changes
2. Search memory for prior decisions and lessons
3. Create a structured plan with steps
4. Ask the user to approve before implementing
5. Never write files unless explicitly told to proceed

Output format:
### Analysis
<what you found>

### Plan
1. <step 1>
2. <step 2>
...

### Questions
<any clarifications needed>

Say "READY TO IMPLEMENT" only after user approval.`,
    tools: ["readFile", "listDirectory", "globFiles", "grepSearch", "gitStatus", "gitDiff", "gitLog", "memorySearch"],
    autoApprove: true,
  },
  build: {
    id: "build",
    name: "Build",
    description: "Focus on build commands and errors",
    icon: "🔨",
    systemPrompt: `## Build Mode
You are in BUILD MODE. Your job is to run builds, fix errors, and ensure code compiles.

Rules:
1. Run the build command first to see current errors
2. Fix errors one at a time, starting with the first
3. Re-run the build after each fix to verify
4. Report progress after each fix
5. Stop when the build passes or you hit 10 fixes

Focus on: type errors, compilation errors, lint errors.
Do not: refactor, add features, or change behavior.`,
    tools: ["readFile", "writeFile", "listDirectory", "globFiles", "grepSearch", "runCommand", "gitStatus", "memorySearch"],
    autoApprove: false,
  },
  spec: {
    id: "spec",
    name: "Spec",
    description: "Create specifications and PRDs",
    icon: "📝",
    systemPrompt: `## Spec Mode
You are in SPEC CREATION MODE. Your job is to help the user create a clear specification.

Rules:
1. Ask clarifying questions about the feature
2. Identify requirements, constraints, and edge cases
3. Structure the spec with: Goal, Requirements, Design, Edge Cases, Success Criteria
4. Write the spec as a markdown document
5. Offer to save it to the project

Output format:
# Feature: [name]

## Goal
<what we're building and why>

## Requirements
- <requirement 1>
- <requirement 2>

## Design
<how it works>

## Edge Cases
- <edge case 1>

## Success Criteria
- <how we know it's done>`,
    tools: ["readFile", "listDirectory", "globFiles", "grepSearch", "memorySearch", "memorySave"],
    autoApprove: true,
  },
};

let currentMode: AgentMode = "chat";
let listeners = new Set<(mode: AgentMode) => void>();

export function getCurrentMode(): AgentMode {
  return currentMode;
}

export function getModeConfig(mode: AgentMode): ModeConfig {
  return AGENT_MODES[mode];
}

export function setMode(mode: AgentMode): void {
  currentMode = mode;
  for (const fn of listeners) fn(mode);
}

export function onModeChange(fn: (mode: AgentMode) => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}
