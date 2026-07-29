/**
 * useAgent Hook
 *
 * Manages the agent conversation loop: send messages, process tool calls,
 * stream responses to the chat panel. Wired to the real AgentService which
 * bootstraps the full stack (Memory + LLM + ContextBuilder + Tools).
 */

import { useCallback, useRef, useEffect } from "react";
import { useAppStore, type ChatMessage } from "../stores/appStore";
import { agentService } from "../services/agentService";
import { getApiKey } from "../services/secretStore";
import { nanoid } from "nanoid";
import type { ProviderConfig } from "@ami/llm";

/**
 * Build the effective provider config: the persisted store config plus the API
 * key loaded from the OS keychain (never persisted in localStorage).
 */
async function resolveProvider(): Promise<ProviderConfig> {
  const cfg = useAppStore.getState().providerConfig;
  const apiKey = (await getApiKey(cfg.type)) ?? cfg.apiKey;
  // If using a cloud provider with no API key, fall back to LM Studio
  const needsKey = cfg.type === "openai" || cfg.type === "anthropic" || cfg.type === "google";
  if (needsKey && !apiKey) {
    console.warn(`[useAgent] No API key for ${cfg.type}, falling back to LM Studio`);
    return { type: "lmstudio", model: "local-model", baseUrl: "http://127.0.0.1:1234/v1" };
  }
  return { ...cfg, apiKey: apiKey ?? undefined };
}

export function useAgent(sessionId = "default", agentId?: string) {
  const {
    chatMessages,
    chatSessions,
    addChatMessage,
    addChatMessageToSession,
    updateChatMessage,
    setStreaming,
    isStreaming,
    addRecentMemory,
  } = useAppStore();

  const abortRef = useRef(false);

  /**
   * Initialize the agent service on first use.
   */
  useEffect(() => {
    if (!agentService.isInitialized) {
      (async () => {
        try {
          await agentService.setProvider(await resolveProvider());
          await agentService.initialize();
        } catch (err) {
          console.error("Failed to initialize agent service:", err);
        }
      })();
    }
  }, []);

  /**
   * Send a user message and process the agent's response.
   * Triggers the full ConversationLoop: context build → LLM → tool calls → memory.
   */
  const sendMessage = useCallback(
    async (content: string) => {
      if (isStreaming) return;

      // ALWAYS add user message first — even if init fails
      const userMsg: ChatMessage = {
        id: nanoid(),
        role: "user",
        content,
        timestamp: Date.now(),
      };
      if (sessionId === "default") {
        addChatMessage(userMsg);
      } else {
        addChatMessageToSession(sessionId, userMsg);
      }

      // Try to initialize if not already done
      if (!agentService.isInitialized) {
        try {
          await agentService.setProvider(await resolveProvider());
          await agentService.initialize();
        } catch (err) {
          const assistantId = nanoid();
          const errorMsg: ChatMessage = {
            id: assistantId,
            role: "assistant",
            content: `Failed to initialize: ${err instanceof Error ? err.message : String(err)}\n\nMake sure LM Studio is running and the base URL is correct in Settings.`,
            timestamp: Date.now(),
          };
          if (sessionId === "default") {
            addChatMessage(errorMsg);
          } else {
            addChatMessageToSession(sessionId, errorMsg);
          }
          setStreaming(false);
          return;
        }
      }

      // Create assistant placeholder
      const assistantId = nanoid();
      const assistantMsg: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        timestamp: Date.now(),
      };
      if (sessionId === "default") {
        addChatMessage(assistantMsg);
      } else {
        addChatMessageToSession(sessionId, assistantMsg);
      }

      setStreaming(true);
      abortRef.current = false;

      // Safety timeout — if no text arrives within 120s, show an error
      let receivedText = false;
      const safetyTimeout = setTimeout(() => {
        if (!abortRef.current) {
          if (!receivedText) {
            updateChatMessage(assistantId, {
              content: "No response received. Check that your LLM provider is configured correctly in Settings (API key, base URL, model).\n\nIf using LM Studio, ensure it is running and a model is loaded.",
            });
          }
          setStreaming(false);
          abortRef.current = true;
        }
      }, 120_000);

      let accumulated = "";

      try {
        const activeSession = chatSessions.find((s) => s.id === sessionId);
        const effectiveAgentId = agentId || activeSession?.agentId || "default";

        for await (const event of agentService.sendMessage(content, sessionId, effectiveAgentId)) {
          if (abortRef.current) break;

          console.log("[useAgent] turn event:", event.type, event);

          switch (event.type) {
            case "text":
              receivedText = true;
              accumulated += event.text;
              updateChatMessage(assistantId, { content: accumulated });
              break;

            case "tool_call":
              accumulated += `\n\n**Calling tool:** \`${event.toolName}\``;
              updateChatMessage(assistantId, { content: accumulated });
              break;

            case "tool_result":
              accumulated += `\n> ${event.result.preview}`;
              updateChatMessage(assistantId, { content: accumulated });
              break;

            case "error":
              accumulated += `\n\n**Error:** ${event.error}`;
              updateChatMessage(assistantId, { content: accumulated });
              break;

            case "done":
              addRecentMemory({
                note_id: `chat-${nanoid()}`,
                content,
                category: "sessions",
                tags: ["chat", "user-message"],
                score: 1.0,
                source: "chat-stream",
                metadata: { sessionId, assistantId, timestamp: Date.now() },
                created_at: Date.now(),
              });
              break;
          }
        }
      } catch (err) {
        const errMsg = err instanceof Error ? err.message : "Unknown error";
        updateChatMessage(assistantId, {
          content: accumulated || `**Connection Error:** ${errMsg}\n\nCheck Settings → Provider configuration.`,
        });
      } finally {
        clearTimeout(safetyTimeout);
        setStreaming(false);
      }
    },
    [addChatMessage, addChatMessageToSession, updateChatMessage, setStreaming, isStreaming, addRecentMemory, sessionId, agentId, chatSessions],
  );

  /**
   * Abort the current response.
   */
  const abort = useCallback(() => {
    abortRef.current = true;
    setStreaming(false);
  }, [setStreaming]);

  // Fix: filter messages by session when not using the default session
  const sessionMessages = sessionId === "default"
    ? chatMessages
    : (chatSessions.find((s) => s.id === sessionId)?.messages ?? []);

  return {
    sendMessage: (content: string) => sendMessage(content),
    abort,
    clearChat: () => sessionId === "default" ? useAppStore.getState().clearChat() : useAppStore.getState().deleteChatSession(sessionId),
    isStreaming,
    isInitialized: agentService.isInitialized,
    messages: sessionMessages,
  };
}
