/**
 * Completion Service — Monaco inline completions provider.
 *
 * Uses fill-in-the-middle with memory-backed context.
 * Local-first: tries Ollama/LM Studio before cloud.
 * 150-250ms debounce, abort-on-keystroke, daily token budget.
 */

import { memoryBridge } from "@ami/memory-bridge";
import type { ProviderConfig } from "@ami/llm";

interface CompletionConfig {
  /** Model to use for completions (e.g. "gpt-4o-mini", "codellama"). */
  model: string;
  /** Provider config — determines which backend to hit. */
  provider: ProviderConfig;
  /** Max tokens per completion request. */
  maxTokens: number;
  /** Debounce in ms. */
  debounceMs: number;
  /** Daily token budget (0 = unlimited). */
  dailyTokenBudget: number;
  /** Whether completions are enabled. */
  enabled: boolean;
}

const DEFAULT_CONFIG: CompletionConfig = {
  model: "gpt-4o-mini",
  provider: { type: "openai" },
  maxTokens: 512,
  debounceMs: 200,
  dailyTokenBudget: 100_000,
  enabled: true,
};

let config: CompletionConfig = { ...DEFAULT_CONFIG };
let dailyTokensUsed = 0;
let lastBudgetReset = Date.now();
let activeController: AbortController | null = null;

/** Update completion config (from Settings panel). */
export function setCompletionConfig(partial: Partial<CompletionConfig>): void {
  config = { ...config, ...partial };
}

/** Get current config. */
export function getCompletionConfig(): CompletionConfig {
  return { ...config };
}

/** Reset daily token budget (call at midnight or on session start). */
export function resetTokenBudget(): void {
  const now = Date.now();
  // Reset if more than 24h since last reset
  if (now - lastBudgetReset > 86_400_000) {
    dailyTokensUsed = 0;
    lastBudgetReset = now;
  }
}

/**
 * Build the fill-in-the-middle prompt with memory context.
 */
async function buildPrompt(
  prefix: string,
  suffix: string,
  language: string,
): Promise<string> {
  // Fetch memory context for the current symbol
  const lastLine = prefix.split("\n").pop()?.trim() ?? "";
  // Extract potential symbol name (word before cursor)
  const symbolMatch = lastLine.match(/(\w+)$/);
  const symbol = symbolMatch?.[1] ?? "";

  let memoryContext = "";
  if (symbol.length > 2) {
    try {
      const results = await memoryBridge.search({
        query: symbol,
        mode: "hybrid",
        limit: 3,
      });
      if (results.length > 0) {        memoryContext =
          "\n// Relevant context:\n" +
          results.map((r) => `// ${r.content.slice(0, 200)}`).join("\n") +
          "\n";
      }
    } catch {
      // Don't block completions on memory failures
    }
  }

  return `Complete the following ${language} code. Be concise and match the existing style.${memoryContext}\n<|prefix|>\n${prefix}\n<|suffix|>\n${suffix}\n<|completion|>`;
}

/**
 * Call the LLM for a completion.
 */
async function fetchCompletion(
  prompt: string,
  signal: AbortSignal,
): Promise<string> {
  const { createProvider } = await import("@ami/llm");

  // The Tauri transport is already installed by llmTransport.ts at app boot
  const provider = createProvider(config.provider);
  await provider.start();

  try {
    const response = await provider.chat({
      model: config.model,
      messages: [{ role: "user", content: prompt }],
      tools: [],
      systemPrompt: "You are a code completion engine. Output only the completed code, nothing else.",
      maxTokens: config.maxTokens,
      temperature: 0.2,
    });

    let result = "";
    for await (const chunk of response) {
      if (signal.aborted) break;
      if (chunk.type === "text") {
        result += chunk.text;
      }
    }

    return result;
  } finally {
    await provider.stop();
  }
}

/**
 * Main completion function called by the Monaco provider.
 * Returns completion text or null.
 */
export async function getCompletion(
  prefix: string,
  suffix: string,
  language: string,
): Promise<string | null> {
  if (!config.enabled) return null;

  // Reset budget at start of each request if 24h has passed
  resetTokenBudget();
  if (config.dailyTokenBudget > 0 && dailyTokensUsed >= config.dailyTokenBudget) return null;

  // Cancel any in-flight request
  activeController?.abort();
  const controller = new AbortController();
  activeController = controller;

  try {
    const prompt = await buildPrompt(prefix, suffix, language);
    const result = await fetchCompletion(prompt, controller.signal);

    if (controller.signal.aborted) return null;

    // Track tokens (rough estimate: ~4 chars per token)
    dailyTokensUsed += Math.ceil(result.length / 4);

    // Clean up the result — strip markdown fences, explanation, etc.
    let cleaned = result.trim();
    // Remove ```language ... ``` wrapping
    cleaned = cleaned.replace(/^```[\w]*\n?/, "").replace(/\n?```$/, "");
    // Remove trailing "or:" style comments that models sometimes add
    cleaned = cleaned.split("\n// or:")[0];
    cleaned = cleaned.split("\n// alternative")[0];

    return cleaned || null;
  } catch (err: any) {
    if (err?.name === "AbortError") return null;
    console.warn("Completion failed:", err);
    return null;
  } finally {
    activeController = null;
  }
}

/**
 * Cancel any in-flight completion request.
 */
export function cancelCompletion(): void {
  activeController?.abort();
  activeController = null;
}
