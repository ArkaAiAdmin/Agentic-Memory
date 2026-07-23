/**
 * Context Builder — Memory-Driven Context Assembly
 *
 * This is the KEY DIFFERENTIATOR from other IDEs.
 * Instead of dumping raw conversation history, the context builder
 * queries the 14-phase search pipeline, decision threads, KG facts,
 * and belief assertions to construct the optimal context window.
 *
 * Context budget allocation:
 *   - 15% System prompt + agent contract
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
  DecisionThread,
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

  constructor(
    private readonly memory: MemoryBridgeClient,
    private readonly maxTokens: number = 128000,
    budget?: Partial<ContextBudget>,
  ) {
    this.budget = { ...DEFAULT_BUDGET, ...budget };
  }

  /**
   * Build the full context for an LLM call.
   * This queries multiple memory subsystems in parallel.
   */
  async build(params: ContextParams): Promise<BuildContext> {
    // Query all memory subsystems in parallel
    const [
      sessionBriefing,
      relevantMemories,
      kgFacts,
      beliefs,
      recentActivity,
    ] = await Promise.all([
      // Session briefing (prior context)
      this.memory
        .recall(params.userMessage, params.sessionId)
        .catch(() => ({ memories: [], context: "" })),

      // 14-phase hybrid search
      this.memory
        .search({
          query: params.userMessage,
          mode: "hybrid",
          limit: 15,
        })
        .catch(() => []),

      // Knowledge graph facts
      this.memory.graphExplore(params.userMessage).catch(() => []),

      // Belief assertions
      this.memory.reviewBeliefs({ minConfidence: 0.3 }).catch(() => []),

      // Recent audit activity
      this.memory.audit({ hours: 2 }).catch(() => []),
    ]);

    // Assemble system prompt from structured sections
    const systemPrompt = this.assembleSystemPrompt({
      sessionContext: sessionBriefing.context,
      relevantMemories,
      kgFacts,
      beliefs,
    });

    // Build message array
    const messages = this.buildMessages(params, relevantMemories);

    return { systemPrompt, messages };
  }

  /**
   * Assemble the system prompt from structured memory sections.
   */
  private assembleSystemPrompt(sections: {
    sessionContext: string;
    relevantMemories: SearchResult[];
    kgFacts: KGNode[];
    beliefs: BeliefAssertion[];
  }): string {
    const parts: string[] = [AGENT_CONTRACT];

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
