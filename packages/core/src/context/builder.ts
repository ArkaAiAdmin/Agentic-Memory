/**
 * Context Builder — Memory-Driven Context Assembly
 *
 * This is the KEY DIFFERENTIATOR from other IDEs.
 * Instead of dumping raw conversation history, the context builder
 * queries the 14-phase search pipeline, decision threads, KG facts,
 * belief assertions, and project rules to construct the optimal
 * context window.
 *
 * Context budget allocation:
 *   - 15% System prompt + agent contract + rules
 *   - 10% Session briefing + decision threads
 *   - 15% Proactive context (search results)
 *   - 10% KG facts
 *   - 40% Conversation messages
 *   - 10% Tool results
 */

import type {
  ContextParams,
  BuildContext,
  Message,
  SearchResult,
  KGNode,
  KGFact,
  BeliefAssertion,
  ContextBudget,
} from "@ami/shared";
import type { MemoryBridgeClient } from "@ami/memory-bridge";

// ── Agent Contract (injected into every system prompt) ────────────────────

const AGENT_CONTRACT = `# Agent Contract

1. **Start every session** — call memory_session_start to load context from prior sessions.
2. **Search before acting** — call memory_search before any significant action.
3. **Save what you learn** — call memory_save for decisions, lessons, and discoveries.
4. **End every session** — call memory_session_end to save context for next time.
5. **Maintenance is automated** — background workers handle indexing, embeddings, contradictions.

You are a memory-first coding agent. Every action is a memory event.
You build context from memory, not from raw conversation history.
`;

// ── Rules Loader ─────────────────────────────────────────────────────────

/** Standard rule file names to search for in the project root. */
const RULES_FILE_NAMES = [
  "AGENTS.md",
  ".cursor/rules",
  "CLAUDE.md",
  ".github/copilot-instructions.md",
];

export type RulesLoader = (fileNames: string[]) => Promise<string>;

// ── Default Budget ────────────────────────────────────────────────────────

const DEFAULT_BUDGET: ContextBudget = {
  systemPrompt: 0.15,
  sessionContext: 0.1,
  proactiveMemory: 0.15,
  kgFacts: 0.1,
  conversationHistory: 0.4,
  toolResults: 0.1,
};

// ── Context Builder ───────────────────────────────────────────────────────

export class ContextBuilder {
  private budget: ContextBudget;
  private rulesCache: string | null = null;
  private rulesLoadTime = 0;
  private readonly MAX_USER_MESSAGE_LENGTH = 10000;

  constructor(
    private readonly memory: MemoryBridgeClient,
    private readonly maxTokens: number = 128000,
    budget?: Partial<ContextBudget>,
    private readonly rulesLoader?: RulesLoader,
  ) {
    this.budget = { ...DEFAULT_BUDGET, ...budget };
  }

  /**
   * Validate and sanitize user message input.
   * Returns sanitized string or throws on invalid input.
   */
  private validateUserMessage(message: string): string {
    if (typeof message !== "string") {
      throw new Error("User message must be a string");
    }

    // Trim whitespace
    let sanitized = message.trim();

    // Enforce length limit
    if (sanitized.length > this.MAX_USER_MESSAGE_LENGTH) {
      sanitized = sanitized.slice(0, this.MAX_USER_MESSAGE_LENGTH);
    }

    // Remove control characters except newlines and tabs
    // eslint-disable-next-line no-control-regex
    sanitized = sanitized.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");

    return sanitized;
  }

  /**
   * Build the full context for an LLM call.
   * This queries multiple memory subsystems in parallel.
   */
  async build(params: ContextParams): Promise<BuildContext> {
    // Validate user input before using it
    const userMessage = this.validateUserMessage(params.userMessage);

    // Query all memory subsystems in parallel
    const [
      sessionBriefing,
      relevantMemories,
      kgFacts,
      beliefs,
      projectRules,
    ] = await Promise.all([
      // Session briefing (prior context)
      this.memory
        .recall(userMessage, params.sessionId)
        .catch(() => ({ memories: [], context: "" })),

      // 14-phase hybrid search
      this.memory
        .search({
          query: userMessage,
          mode: "hybrid",
          limit: 15,
        })
        .catch(() => []),

      // Knowledge graph facts
      this.memory.graphExplore(userMessage).catch(() => []),

      // Belief assertions
      this.memory.reviewBeliefs({ minConfidence: 0.3 }).catch(() => []),

      // Project rules (AGENTS.md, .cursor/rules, CLAUDE.md)
      this.loadRules(),
    ]);

    // Assemble system prompt from structured sections
    let systemPrompt: string;
    try {
      systemPrompt = this.assembleSystemPrompt({
        sessionContext: sessionBriefing.context,
        relevantMemories,
        kgFacts,
        beliefs,
        projectRules,
      });
    } catch (err) {
      console.error("[ContextBuilder] Failed to assemble system prompt:", err);
      systemPrompt = AGENT_CONTRACT;
    }

    // Build message array
    let messages: Message[];
    try {
      messages = this.buildMessages(params, relevantMemories);
    } catch (err) {
      console.error("[ContextBuilder] Failed to build messages:", err);
      messages = [];
    }

    return { systemPrompt, messages };
  }

  /**
   * Load project rules from standard file locations.
   * Cached for 5 minutes to avoid re-reading on every turn.
   */
  private async loadRules(): Promise<string> {
    // Return cached rules if fresh enough
    if (this.rulesCache !== null && Date.now() - this.rulesLoadTime < 300_000) {
      return this.rulesCache;
    }

    if (!this.rulesLoader) {
      this.rulesCache = "";
      this.rulesLoadTime = Date.now();
      return "";
    }

    try {
      const rules = await this.rulesLoader(RULES_FILE_NAMES);
      this.rulesCache = rules;
      this.rulesLoadTime = Date.now();
      return rules;
    } catch {
      this.rulesCache = "";
      this.rulesLoadTime = Date.now();
      return "";
    }
  }

  /**
   * Assemble the system prompt from structured memory sections.
   */
  private assembleSystemPrompt(sections: {
    sessionContext: string;
    relevantMemories: SearchResult[];
    kgFacts: KGNode[];
    beliefs: BeliefAssertion[];
    projectRules: string;
  }): string {
    const parts: string[] = [AGENT_CONTRACT];

    // Project rules (AGENTS.md / .cursor/rules / CLAUDE.md)
    if (sections.projectRules) {
      parts.push(`## Project Rules\n${sections.projectRules}`);
    }

    // Session context
    if (sections.sessionContext) {
      parts.push(`## Prior Session Context\n${sections.sessionContext}`);
    }

    // Relevant memories
    if (sections.relevantMemories.length > 0) {
      const memoryText = sections.relevantMemories
        .map(
          (m) =>
            `- [${m.category}] ${m.content.slice(0, 200)} (score: ${m.score.toFixed(2)})`,
        )
        .join("\n");
      parts.push(`## Relevant Memories\n${memoryText}`);
    }

    // KG facts
    if (sections.kgFacts.length > 0) {
      const factText = sections.kgFacts
        .flatMap((n) =>
          n.facts.map(
            (f: KGFact) =>
              `- ${f.subject} ${f.predicate} ${f.object} (confidence: ${f.confidence.toFixed(2)})`,
          ),
        )
        .join("\n");
      parts.push(`## Knowledge Graph Facts\n${factText}`);
    }

    // Low-confidence beliefs needing review
    const lowConfidence = sections.beliefs.filter(
      (b) => b.confidence < 0.6,
    );
    if (lowConfidence.length > 0) {
      const beliefText = lowConfidence
        .map(
          (b) =>
            `- ${b.content} (confidence: ${b.confidence.toFixed(2)}, evidence: ${b.evidence_count})`,
        )
        .join("\n");
      parts.push(
        `## Beliefs Needing Review\n${beliefText}`,
      );
    }

    return parts.join("\n\n");
  }

  /**
   * Build the message array for the LLM call.
   */
  private buildMessages(
    params: ContextParams,
    memories: SearchResult[],
  ): Message[] {
    const messages: Message[] = [];

    // Inject memory context as a system-level user message
    if (memories.length > 0) {
      const memoryContext = memories
        .slice(0, 10)
        .map((m) => `[${m.category}] ${m.content.slice(0, 300)}`)
        .join("\n");

      messages.push({
        role: "user",
        content: `# Memory Context\n${memoryContext}`,
      });
      messages.push({
        role: "assistant",
        content: "I've reviewed the relevant memories and context.",
      });
    }

    return messages;
  }

  /**
   * Estimate token count for a string (rough approximation).
   */
  private estimateTokens(text: string): number {
    return Math.ceil(text.length / 4); // ~4 chars per token
  }
}
